from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import openpyxl

from shoebox.clients.ebay_rest.client import EbayClient
from shoebox.clients.image_log import ImageLogClient
from shoebox.models.ebay_listing import EbayListingResult
from shoebox.settings import ensure_runtime_dirs, get_settings
from shoebox.transforms.listing_builder import sport as get_sport
from shoebox.transforms.variation_listing_builder import (
    ChecklistVariationRow,
    build_group_key,
    build_individual_inventory_item,
    build_item_group_payload,
    build_sku_offer_payload,
    build_variation_sku,
    variation_store_category,
)
from shoebox.utils.ad_campaign import get_ad_campaign
from shoebox.utils.jsonl import append_jsonl

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_GROUP_IMAGE_LIMIT = 12


def _discover_card_images(images_dir: Path, card_number: str) -> dict[str, Path]:
    """
    Scan images_dir for front/back image files belonging to card_number.
    Matches any filename whose stem contains the card number (case-insensitive).
    Returns {"front": Path, "back": Path} for whichever sides are found.
    """
    cn_lower = card_number.lower()
    found: dict[str, Path] = {}
    for f in images_dir.iterdir():
        if f.suffix.lower() not in _IMAGE_EXTENSIONS:
            continue
        stem = f.stem.lower()
        if cn_lower not in stem:
            continue
        if "back" in stem:
            found.setdefault("back", f)
        else:
            found.setdefault("front", f)
    return found


def _upload_card_images(
    img_client: ImageLogClient,
    row: ChecklistVariationRow,
    images_dir: Path,
) -> list[str]:
    """
    Discover and upload front/back images for one card row.
    Returns a list of public URLs (front first, back second if present).
    """
    paths = _discover_card_images(images_dir, row.card_number)
    urls: list[str] = []
    for side in ("front", "back"):
        file_path = paths.get(side)
        if not file_path:
            continue
        try:
            entry = img_client.upload_image(
                file_path=file_path,
                set_name=row.set_name,
                subset_name=row.subset_name,
                card_number=row.card_number,
                side=side,
                delete_original=False,
            )
            if entry.public_url:
                urls.append(entry.public_url)
        except Exception:
            logger.warning("Failed to upload %s image for %s", side, row.card_number, exc_info=True)
    return urls


def load_checklist_from_excel(
    excel_path: Path,
    sheet_name: str = "Checklist",
    in_stock_only: bool = True,
) -> dict[str, list[ChecklistVariationRow]]:
    """
    Read the Checklist sheet and group rows by Subset ID.
    When in_stock_only=True, rows with no Qty are excluded from the listing
    but the variation slot is still reserved in the group for future restocks
    by passing include_all_in_group=True to run_variation_listing.
    """
    wb = openpyxl.load_workbook(str(excel_path), data_only=True)
    ws = wb[sheet_name]
    headers = [cell.value for cell in ws[1]]

    groups: dict[str, list[ChecklistVariationRow]] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        d = dict(zip(headers, row, strict=False))
        if not any(v is not None for v in d.values()):
            continue
        if not d.get("Subset ID"):
            continue

        qty = int(d["Qty"]) if d.get("Qty") else 0
        if in_stock_only and qty <= 0:
            continue

        cr = ChecklistVariationRow(
            record_id=str(d["Record ID"]),
            set_year=str(d["Set Year"]),
            subset_id=str(d["Subset ID"]),
            set_name=str(d["Set Name"]),
            subset_name=str(d["Subset Name"]),
            subset_type=str(d.get("Subset Type") or ""),
            derived_card_number=(
                str(d["Derived Card Number"]) if d.get("Derived Card Number") else None
            ),
            card_number=str(d["Card Number"]),
            qty=qty,
            price=float(d["Price"]) if d.get("Price") else 0.0,
            player=str(d["Player"]) if d.get("Player") else "",
            team=str(d["Team"]) if d.get("Team") else None,
            display_name=(
                str(d["Display Name"]) if d.get("Display Name") else str(d["Card Number"])
            ),
            note=str(d["Note"]) if d.get("Note") else None,
        )
        groups.setdefault(cr.subset_id, []).append(cr)

    return groups


def run_variation_listing(
    *,
    excel_path: Path,
    sheet_name: str = "Checklist",
    publish: bool = False,
    dry_run: bool = False,
    schedule: bool = False,
    in_stock_only: bool = False,
    images_dir: Path | None = None,
    default_image_path: Path | None = None,
    promote_rate: int = 20,
    volume_discount_tiers: list[dict[int, int]] | None = None,
) -> None:
    """
    For each Subset ID group in the Excel sheet:
      1. Optionally upload a default listing image from default_image_path.
      2. Optionally discover and upload per-card images from images_dir.
      3. Create one inventory item per variation (card).
      4. Create the inventory item group linking all SKUs.
      5. Create a single offer referencing the group.
      6. Publish and promote the listing.

    default_image_path: local image file used as the item group hero image
    (shown before a buyer selects a specific variation). Uploaded once and
    prepended to the group imageUrls.

    images_dir: directory of card scans named with the card number
    (e.g. "BCP-1_front.jpg"). Each card's front image is set on its own
    inventory item so eBay shows it when that variation is selected. The first
    _GROUP_IMAGE_LIMIT such front images are also appended to the group imageUrls.
    """
    settings = get_settings()

    ebay = EbayClient(settings=settings)
    img_client = ImageLogClient(settings=settings) if (images_dir or default_image_path) else None
    results_path = Path(settings.paths.exports_dir) / "jsonl" / "ebay_listings.jsonl"

    if schedule:
        listing_start_date = (datetime.now(UTC) + timedelta(days=19)).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        listing_start_date = None

    groups = load_checklist_from_excel(
        excel_path, sheet_name=sheet_name, in_stock_only=in_stock_only
    )
    logger.info("Loaded %d subset group(s) from %s", len(groups), excel_path)

    total_groups = len(groups)
    for group_idx, (subset_id, rows) in enumerate(groups.items(), 1):
        group_key = build_group_key(subset_id)
        logger.info(
            "[%d/%d] Processing '%s' — %d variation(s), group_key=%s",
            group_idx,
            total_groups,
            subset_id,
            len(rows),
            group_key,
        )

        # Upload the default listing image once
        default_image_url: str | None = None
        if img_client and default_image_path:
            if not default_image_path.exists():
                raise FileNotFoundError(f"default_image_path not found: {default_image_path}")
            logger.info("[%s] Uploading default listing image...", group_key)
            try:
                entry = img_client.upload_image(
                    file_path=default_image_path,
                    set_name="variation_listing",
                    subset_name="default",
                    card_number="default",
                    side="front",
                    delete_original=False,
                )
                default_image_url = entry.public_url
                logger.info("[%s] Default listing image uploaded: %s", group_key, default_image_url)
            except Exception:
                logger.warning("[%s] Failed to upload default_image_path", group_key, exc_info=True)

        # Upload images and build per-SKU image URL map
        sku_images: dict[str, list[str]] = {}
        if img_client and images_dir:
            logger.info("[%s] Uploading per-card images for %d variations...", group_key, len(rows))
            for card_idx, row in enumerate(rows, 1):
                sku = build_variation_sku(row.subset_id, row.card_number)
                logger.info(
                    "[%s] [%d/%d] Uploading images for card %s...",
                    group_key,
                    card_idx,
                    len(rows),
                    row.card_number,
                )
                urls = _upload_card_images(img_client, row, images_dir)
                if urls:
                    sku_images[sku] = urls
            logger.info(
                "[%s] Per-card image upload done — %d/%d cards had images",
                group_key,
                len(sku_images),
                len(rows),
            )

        has_per_variation_images = bool(sku_images)

        # Group-level images: default hero first, then per-card front images
        group_image_urls: list[str] = [default_image_url] if default_image_url else []
        for sku_urls in sku_images.values():
            if sku_urls:
                group_image_urls.append(sku_urls[0])  # front image only
            if len(group_image_urls) >= _GROUP_IMAGE_LIMIT:
                break

        logger.info(
            "[%s] Building payloads — %d inventory items, %d group images",
            group_key,
            len(rows),
            len(group_image_urls),
        )
        sku_row_pairs = [(build_variation_sku(r.subset_id, r.card_number), r) for r in rows]
        sku_item_map: dict[str, dict] = {
            sku: build_individual_inventory_item(row, image_urls=sku_images.get(sku))
            for sku, row in sku_row_pairs
        }
        skus = list(sku_item_map.keys())

        store_cat = variation_store_category(rows[0])
        sku_offer_map: dict[str, dict] = {
            sku: build_sku_offer_payload(sku, row, store_cat, listing_start_date=listing_start_date)
            for sku, row in sku_row_pairs
        }

        item_group = build_item_group_payload(
            rows, skus, group_image_urls, has_per_variation_images=has_per_variation_images
        )

        if dry_run:
            result = EbayListingResult(
                sku=group_key,
                inventory_item_group_key=group_key,
                success=True,
                request={
                    "data": {
                        "inventory_items": sku_item_map,
                        "sku_offers": sku_offer_map,
                        "item_group": item_group,
                    }
                },
                response={"data": {"dry_run": True}},
            )
            logger.info("[dry_run] group=%s title=%s", group_key, item_group["title"])
        else:
            logger.info(
                "[%s] Submitting to eBay (publish=%s, %d variations)...",
                group_key,
                publish,
                len(rows),
            )
            resp = ebay.create_variation_listing_flow(
                group_key=group_key,
                sku_item_map=sku_item_map,
                sku_offer_map=sku_offer_map,
                item_group=item_group,
                publish=publish,
                promote_listing=True,
                campaign_id=get_ad_campaign(
                    title=item_group["title"],
                    set_name=rows[0].set_name,
                    sport=get_sport(rows[0].set_name),
                ),
                promote_rate=promote_rate,
                volume_discount_tiers=volume_discount_tiers,
            )

            listing_id = None
            if isinstance(resp.get("publish"), dict):
                listing_id = resp["publish"].get("listingId") or resp["publish"].get("listing_id")

            result = EbayListingResult(
                sku=group_key,
                inventory_item_group_key=group_key,
                listing_id=listing_id,
                success=True,
                request={"data": {"item_group": item_group}},
                response={"data": resp},
            )
            logger.info(
                "[%d/%d] Done — group=%s listing_id=%s",
                group_idx,
                total_groups,
                subset_id,
                result.listing_id,
            )

        append_jsonl(results_path, result.model_dump())

    if img_client:
        img_client.flush_append_log()


def _derive_default_image_path(set_name: str, set_images_dir: str) -> Path | None:
    filename = set_name.lower().replace(" ", "_") + ".png"
    path = Path(set_images_dir) / filename
    return path if path.exists() else None


def main(excel_path: Path, **kwargs) -> None:
    """Direct-run entrypoint; prefer `shoebox create-variation-listings` for flag handling.

    If no default_image_path is given, tries to derive a hero image from the
    set name under settings.paths.set_images_dir.
    """
    ensure_runtime_dirs()
    settings = get_settings()

    if "default_image_path" not in kwargs:
        groups = load_checklist_from_excel(excel_path, in_stock_only=True)
        first_rows = next(iter(groups.values())) if groups else []
        set_name = first_rows[0].set_name if first_rows else ""
        kwargs["default_image_path"] = _derive_default_image_path(
            set_name, settings.paths.set_images_dir
        )

    run_variation_listing(excel_path=excel_path, **kwargs)
