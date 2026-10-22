"""Refresh live listings as brand-new ones: delete the inventory item, recreate it.

``relist-listings`` keeps each SKU's inventory item and swaps only its offer.
This pipeline goes one step further, the API equivalent of "end listing, then
sell a similar item": ``deleteInventoryItem`` ends the live listing and deletes
its offers, and the item and a fresh offer are rebuilt from what eBay held, so
the new listing has a new item ID and no ties to the old one beyond the SKU.

Nothing is re-derived: title, description, aspects, condition, packaging,
price, policies (best-offer thresholds included) and store categories are
carried across exactly. Pictures come from the live listing and are re-hosted
in GCS first, because the old listing's eBay picture URLs are not something to
depend on once it has ended.

Because a delete can't be undone, every listing is snapshotted to
``<exports_dir>/refresh_snapshots/<run>/<sku>.json`` *before* it is touched.
If a recreate fails after the delete, ``refresh-listings --restore <file|dir>``
rebuilds the listing from that snapshot.

What a refresh loses, by design: watchers, view and sales history, open best
offers, and the listing's search standing. Watched listings are skipped unless
``include_watched`` is set.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from shoebox.clients.ebay.client import EbayClient
from shoebox.clients.ebay.errors import EbayClientError
from shoebox.clients.gcs import GCSClient
from shoebox.pipelines.relist_listings import get_active_listings, get_listing_df
from shoebox.services.listing_pictures import live_picture_urls, rehost_live_pictures
from shoebox.services.listings import ListingService
from shoebox.settings import get_settings
from shoebox.transforms.listing_builder import refresh_inventory_item_body, round_trip_offer_body

logger = logging.getLogger(__name__)

# Ad rate (percent) for a promoted listing whose old ad didn't report one.
_DEFAULT_PROMOTE_RATE = 7


def _campaign_ads(ebay_api: EbayClient) -> pd.DataFrame:
    ads = pd.json_normalize(ebay_api.marketing.get_campaign_ads())
    cols = ["campaign_id", "ad_id", "listing_id", "bid_percentage"]
    for c in cols:
        if c not in ads.columns:
            ads[c] = None
    return ads[cols]


def select_listings(
    ebay_api: EbayClient,
    *,
    skus: list[str] | None = None,
    item_ids: list[str] | None = None,
    aged: bool = False,
    min_age: int = 90,
    min_price: float = 2,
    min_impressions: int = 100,
    include_watched: bool = False,
    limit: int | None = None,
) -> pd.DataFrame:
    """Live single-SKU listings to refresh, with their ad (if any) joined on."""
    if not (skus or item_ids or aged):
        raise ValueError("Name listings with --sku / --item-id, or pass --aged")

    if aged:
        df = get_listing_df(ebay_api)
        df = df[
            (df["listing_age"] >= min_age)
            & (df["price"] > min_price)
            & (df["LISTING_IMPRESSION_TOTAL"] >= min_impressions)
        ]
    else:
        df = get_active_listings(ebay_api)
        wanted_skus, wanted_ids = set(skus or []), {str(i) for i in item_ids or []}
        df = df[df["sku"].isin(wanted_skus) | df["item_id"].astype(str).isin(wanted_ids)]
        found = set(df["sku"]) | set(df["item_id"].astype(str))
        for missing in sorted((wanted_skus | wanted_ids) - found):
            logger.warning("%s is not an active single-SKU listing; skipping", missing)

    watchers = df["watchers"] if "watchers" in df.columns else pd.Series(0, index=df.index)
    watched = pd.to_numeric(watchers, errors="coerce").fillna(0) > 0
    if not include_watched and watched.any():
        for title in df.loc[watched, "title"]:
            logger.info("Skipping watched listing (would lose its watchers): %s", title)
        df = df[~watched]

    df = df.merge(_campaign_ads(ebay_api), how="left", left_on="item_id", right_on="listing_id")
    df = df.drop_duplicates(subset="sku")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.sort_values(by="price", ascending=False).reset_index(drop=True)
    if limit is not None:
        df = df.head(limit)
    return df


def _none_if_nan(value: Any) -> Any:
    return None if value is None or (isinstance(value, float) and pd.isna(value)) else value


def build_snapshot(
    row: pd.Series,
    ebay_api: EbayClient,
    gcs_client: GCSClient | None,
    settings,
    *,
    rehost: bool,
) -> dict[str, Any]:
    """Everything needed to recreate a listing, gathered while it still exists.

    ``rehost`` copies the live pictures to GCS (needed to apply); a preview
    leaves GCS alone and records the live picture URLs instead.
    """
    sku, item_id = row["sku"], str(row["item_id"])

    item = ebay_api.inventory.get_inventory_item(sku)
    if item.inventory_item_group_keys:
        raise EbayClientError(
            f"sku={sku} is part of a variation listing; deleting it would pull it from the group"
        )
    offer = ebay_api.inventory.find_offer(sku)
    if offer is None:
        raise EbayClientError(f"sku={sku} has no offer; not an Inventory API listing")
    if offer.status != "PUBLISHED" or str(offer.listing_id) != item_id:
        raise EbayClientError(
            f"sku={sku}: offer {offer.offer_id} is {offer.status} on listing "
            f"{offer.listing_id}, not live on {item_id}"
        )

    if rehost:
        image_urls = rehost_live_pictures(
            ebay_api, item_id=item_id, sku=sku, gcs=gcs_client, settings=settings
        )
    else:
        image_urls = live_picture_urls(ebay_api, item_id)

    rate = _none_if_nan(row.get("bid_percentage"))
    return {
        "sku": sku,
        "item_id": item_id,
        "title": row.get("title"),
        "price": _none_if_nan(row.get("price")),
        "ad_id": _none_if_nan(row.get("ad_id")),
        "campaign_id": _none_if_nan(row.get("campaign_id")),
        "promote_rate": round(float(rate)) if rate else _DEFAULT_PROMOTE_RATE,
        "inventory_item_body": refresh_inventory_item_body(item, image_urls),
        "offer_body": round_trip_offer_body(offer.raw),
        "original": {"inventory_item": item.raw, "offer": offer.raw},
        "taken_at": datetime.now(UTC).isoformat(),
    }


def write_snapshot(snapshot: dict[str, Any], run_dir: Path) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{snapshot['sku']}.json"
    path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    return path


def refresh_one(snapshot: dict[str, Any], service: ListingService) -> str | None:
    """Delete and recreate one listing from its snapshot; returns the new listing ID."""
    out = service.refresh_listing(
        sku=snapshot["sku"],
        inventory_item_body=snapshot["inventory_item_body"],
        offer_body=snapshot["offer_body"],
        existing_ad_id=snapshot.get("ad_id"),
        campaign_id=snapshot.get("campaign_id"),
        promote_rate=snapshot.get("promote_rate") or _DEFAULT_PROMOTE_RATE,
    )
    publish = out.get("publish") or {}
    return publish.get("listing_id") or publish.get("listingId")


def restore(path: Path) -> list[dict[str, Any]]:
    """Recreate listings from snapshot files (one file, or every *.json in a directory)."""
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    service = ListingService(EbayClient())
    results = []
    for f in files:
        snap = json.loads(f.read_text(encoding="utf-8"))
        try:
            out = service.recreate_listing(
                sku=snap["sku"],
                inventory_item_body=snap["inventory_item_body"],
                offer_body=snap["offer_body"],
                campaign_id=snap.get("campaign_id"),
                promote_rate=snap.get("promote_rate") or _DEFAULT_PROMOTE_RATE,
            )
            publish = out.get("publish") or {}
            new_id = publish.get("listing_id") or publish.get("listingId")
            logger.info("Restored sku=%s -> listing %s", snap["sku"], new_id)
            results.append({"sku": snap["sku"], "new_item_id": new_id, "error": None})
        except Exception as e:
            logger.error("Restore failed for sku=%s (%s): %s", snap["sku"], f, e)
            results.append({"sku": snap["sku"], "new_item_id": None, "error": str(e)})
    return results


def main(
    *,
    skus: list[str] | None = None,
    item_ids: list[str] | None = None,
    aged: bool = False,
    min_age: int = 90,
    min_price: float = 2,
    min_impressions: int = 100,
    include_watched: bool = False,
    limit: int | None = None,
    apply: bool = False,
) -> pd.DataFrame:
    ebay_api = EbayClient()
    settings = get_settings()
    gcs_client = GCSClient() if apply else None
    service = ListingService(ebay_api, gcs_client)

    df = select_listings(
        ebay_api,
        skus=skus,
        item_ids=item_ids,
        aged=aged,
        min_age=min_age,
        min_price=min_price,
        min_impressions=min_impressions,
        include_watched=include_watched,
        limit=limit,
    )
    logger.info("Found %d listing(s) to refresh", len(df))

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(settings.paths.exports_dir) / "refresh_snapshots" / stamp

    report = []
    for index, row in df.iterrows():
        logger.info("[%d/%d] %s", index + 1, len(df), row["title"])
        entry = {"sku": row["sku"], "old_item_id": str(row["item_id"]), "title": row["title"]}
        try:
            snapshot = build_snapshot(row, ebay_api, gcs_client, settings, rehost=apply)
            snap_path = write_snapshot(snapshot, run_dir)
            entry["snapshot"] = str(snap_path)
            if not apply:
                logger.info("[preview] Would delete + recreate sku=%s (%s)", row["sku"], snap_path)
                entry["status"] = "preview"
            else:
                entry["new_item_id"] = refresh_one(snapshot, service)
                entry["status"] = "refreshed"
                logger.info(
                    "Refreshed sku=%s: %s -> %s",
                    row["sku"],
                    entry["old_item_id"],
                    entry["new_item_id"],
                )
        except Exception as e:
            entry["status"] = "failed"
            entry["error"] = str(e)
            logger.warning("Failed: %s, error: %s", row["title"], e)
            if entry.get("snapshot") and apply:
                logger.warning(
                    "If the old listing is gone, rebuild it with: "
                    "shoebox refresh-listings --restore %s",
                    entry["snapshot"],
                )
        report.append(entry)

    report_df = pd.DataFrame(report)
    if not report_df.empty:
        run_dir.mkdir(parents=True, exist_ok=True)
        report_df.to_csv(run_dir / "report.csv", index=False)
        logger.info("Report: %s", run_dir / "report.csv")
        failed = (report_df["status"] == "failed").sum()
        if failed:
            logger.warning("%d listing(s) failed to refresh", failed)
    return report_df
