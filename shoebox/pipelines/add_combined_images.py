"""Add a combined front+back picture to listings already live on eBay.

New listings get this picture from ``create-listings``. This backfills it on
the existing ones: for every single-card listing with exactly two pictures
(front, back), the two are downloaded, combined side by side, uploaded to GCS,
and appended as picture 3. The front stays picture 1, so search thumbnails and
the send-offers Slack prompts don't change.

    shoebox add-combined-images                       # preview from BigQuery
    shoebox add-combined-images --input pull.csv      # preview a local export
    shoebox add-combined-images --apply --limit 5     # update the first 5

Nothing is sent to eBay without ``--apply``. Re-running is safe: every
listing's live picture count is checked right before writing, and anything
without exactly two pictures is skipped, so none gets a second composite.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import requests

from shoebox.clients.ebay.client import EbayClient
from shoebox.clients.gcs import GCSClient
from shoebox.pipelines.enhance_listing_titles import (
    _clean_str,
    load_listings,
    load_listings_from_bigquery,
)
from shoebox.settings import get_settings
from shoebox.transforms.listing_builder import inventory_item_body_with_images
from shoebox.utils.images import combine_front_back
from shoebox.utils.logging_setup import setup_logging
from shoebox.utils.slack import notify_best_effort

logger = logging.getLogger(__name__)

# Multi-variation "You Pick" listings carry per-variation pictures; leave them be.
_VARIATION_TITLE_MARKER = "complete your set"

_DOWNLOAD_TIMEOUT_S = 30


def _picture_urls(value: object) -> list[str]:
    """picture_urls arrives as a list, a numpy array (BigQuery), or a JSON string (CSV)."""
    if value is None:
        return []
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        if not isinstance(value, list):
            return []
    try:
        return [str(v) for v in value if _clean_str(v)]
    except TypeError:
        return []  # NaN and other scalars


def build_image_plan(df: pd.DataFrame) -> pd.DataFrame:
    """One report row per listing, saying whether it gets a combined picture.

    Without a ``picture_urls`` column (v_active_listing_details doesn't carry
    one) the count is left blank and checked against the live item at apply
    time instead.
    """
    has_pictures = "picture_urls" in df.columns
    records = []
    for _, row in df.iterrows():
        title = _clean_str(row.get("title"))
        sku = _clean_str(row.get("sku"))
        item_id = _clean_str(row.get("item_id")).removesuffix(".0")
        pictures = _picture_urls(row.get("picture_urls")) if has_pictures else None

        skip_reason = ""
        if _VARIATION_TITLE_MARKER in title.lower():
            skip_reason = "variation_listing"
        elif not sku:
            # The inventory item is what gets updated; SKU-less listings
            # (created outside the Inventory API) have none.
            skip_reason = "no_sku"
        elif pictures is not None and len(pictures) != 2:
            skip_reason = f"has_{len(pictures)}_pictures"

        records.append(
            {
                "item_id": item_id,
                "sku": sku,
                "title": title,
                "picture_count": len(pictures) if pictures is not None else None,
                "eligible": not skip_reason,
                "skip_reason": skip_reason,
            }
        )
    return pd.DataFrame.from_records(records)


def _full_size(url: str) -> str:
    # eBay-hosted picture URLs ending in $_1 are thumbnails; $_57 is full size.
    return url.replace("$_1", "$_57")


def _download(url: str) -> bytes:
    resp = requests.get(_full_size(url), timeout=_DOWNLOAD_TIMEOUT_S)
    resp.raise_for_status()
    return resp.content


def add_combined_image(
    sku: str,
    *,
    ebay: EbayClient,
    gcs: GCSClient,
    bucket: str,
) -> str:
    """Append a front+back composite to one listing's pictures.

    Returns "updated", or "skipped" when the live inventory item no longer has
    exactly two pictures (the report source can be a day stale).
    """
    item = ebay.inventory.get_inventory_item(sku)
    current = list(item.product.image_urls) if item.product else []
    if len(current) != 2:
        logger.info("sku=%s has %d picture(s) on eBay; skipping", sku, len(current))
        return "skipped"

    combined = combine_front_back(_download(current[0]), _download(current[1]))
    blob = gcs.client.bucket(bucket).blob(f"other/{sku}_combined.jpg")
    blob.upload_from_string(combined, content_type="image/jpeg")

    ebay.inventory.upsert_inventory_item(
        sku, inventory_item_body_with_images(item, [*current, blob.public_url])
    )
    logger.info("Added combined picture to sku=%s", sku)
    return "updated"


def apply_combined_images(
    plan: pd.DataFrame,
    *,
    ebay: EbayClient | None = None,
    gcs: GCSClient | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """Update the eligible listings one at a time; a failure doesn't stop the run.

    ``limit`` caps listings actually updated, not listings looked at: when the
    plan couldn't count pictures, many of the first candidates may turn out
    to have more or fewer than two and be skipped.
    """
    ebay = ebay or EbayClient()
    gcs = gcs or GCSClient()
    bucket = get_settings().gcs.image_bucket

    targets = plan[plan["eligible"]]

    statuses: list[str] = []
    errors: list[str] = []
    total = len(targets)
    for idx, (_, row) in enumerate(targets.iterrows(), 1):
        if limit is not None and statuses.count("updated") >= limit:
            break
        sku = row["sku"]
        try:
            statuses.append(add_combined_image(sku, ebay=ebay, gcs=gcs, bucket=bucket))
            errors.append("")
        except Exception as e:
            logger.warning("Failed to add combined picture to sku=%s: %s", sku, e)
            statuses.append("failed")
            errors.append(str(e))
        if idx % 25 == 0 or idx == total:
            logger.info("Processed %d of %d listing(s)", idx, total)

    return targets.head(len(statuses)).assign(status=statuses, error=errors)


def write_report(plan: pd.DataFrame, exports_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y_%m_%d_%H_%M_%S")
    out_path = exports_dir / f"csv/combined_images_{stamp}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plan.to_csv(out_path, index=False)
    return out_path


def add_combined_images(
    *,
    input_path: Path | None = None,
    apply: bool = False,
    limit: int | None = None,
) -> pd.DataFrame:
    settings = get_settings()

    if input_path:
        df = load_listings(input_path)
        source = str(input_path)
    else:
        df = load_listings_from_bigquery()
        source = f"BigQuery {settings.bigquery.ebay_dataset}.v_active_listing_details"
    logger.info("Loaded %d listing(s) from %s", len(df), source)

    plan = build_image_plan(df)
    report_path = write_report(plan, Path(settings.paths.exports_dir))
    eligible = int(plan["eligible"].sum()) if not plan.empty else 0
    skipped = plan.loc[~plan["eligible"], "skip_reason"].value_counts().to_dict()
    logger.info("%d listing(s) eligible for a combined picture; skipped: %s", eligible, skipped)
    if not plan.empty and plan["picture_count"].isna().any():
        logger.info("No picture_urls in the source; picture counts are checked on eBay per listing")
    logger.info("Report written to %s", report_path)

    if not apply:
        logger.info("Preview only — re-run with --apply to update eBay.")
        return plan

    applied = apply_combined_images(plan, limit=limit)
    counts = applied["status"].value_counts().to_dict() if not applied.empty else {}
    updated, failed = counts.get("updated", 0), counts.get("failed", 0)

    applied.to_csv(report_path.with_name(report_path.stem + "_applied.csv"), index=False)
    notify_best_effort(
        settings.slack.notify_channel,
        f"Added a combined picture to {updated} listing(s){f', {failed} failed' if failed else ''}.",
    )
    logger.info("Updated %d listing(s); %s", updated, counts)
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="Listings export (.jsonl or .csv)")
    parser.add_argument("--apply", action="store_true", help="Push the changes to eBay")
    parser.add_argument("--limit", type=int, help="Cap how many listings are updated")
    args = parser.parse_args()

    setup_logging()

    add_combined_images(
        input_path=Path(args.input) if args.input else None,
        apply=args.apply,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
