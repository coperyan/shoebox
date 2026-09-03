import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from shoebox.clients.bigquery import BigQueryClient
from shoebox.clients.ebay.client import EbayClient
from shoebox.clients.ebay.trading import TradingQuotaExceeded
from shoebox.clients.gcs import GCSClient
from shoebox.settings import get_settings
from shoebox.utils.jsonl import write_jsonl
from shoebox.utils.slack import notify_best_effort

logger = logging.getLogger(__name__)

# GetItem has no batch equivalent that carries item specifics, so the details
# sweep is one call per listing and its wall clock is pure network wait.
# Overlapping the calls is the whole speedup: measured against the live API,
# 1,646 listings take ~22 min serially, ~2.2 min at 8 workers, ~1.4 at 16.
# 12 leaves margin on eBay's side while capturing most of the gain.
_DEFAULT_WORKERS = 12

# Above this share of failures the snapshot is too incomplete to publish.
_MAX_FAILURE_SHARE = 0.10


def sync_active_listing_details(*, max_workers: int = _DEFAULT_WORKERS):
    ebay_api = EbayClient()
    gcs_client = GCSClient()
    bq_client = BigQueryClient()
    settings = get_settings()

    notify_best_effort(settings.slack.notify_channel, "Starting sync_active_listing_details..")

    active_listings = ebay_api.legacy_api.get_active_listings()

    now = datetime.now(UTC)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_file = now.strftime("%Y_%m_%d_%H_%M_S")

    # Variation ("Complete Your Set") listings have no single card to describe.
    item_ids = [
        listing["item_id"]
        for listing in active_listings
        if "complete your set" not in (listing["title"] or "").lower()
    ]

    def log_progress(done: int, total: int) -> None:
        if done % 100 == 0 or done == total:
            logger.info("Fetched %d of %d listing detail(s)", done, total)

    started = time.monotonic()
    try:
        details, failures = ebay_api.legacy_api.get_item_details_bulk(
            item_ids,
            max_workers=max_workers,
            on_progress=log_progress,
        )
    except TradingQuotaExceeded as e:
        # Concurrency buys wall-clock, not extra calls: GetItem is metered by a
        # daily allowance, and one full sweep costs one call per listing.
        notify_best_effort(
            settings.slack.notify_channel,
            f"sync_active_listing_details stopped: {e}",
        )
        raise
    logger.info(
        "Fetched %d listing detail(s) in %.1fs (%d worker(s))",
        len(details),
        time.monotonic() - started,
        max_workers,
    )

    if failures:
        # A handful of bad items shouldn't throw away the sweep, but a snapshot
        # missing a real share of the store would quietly corrupt anything
        # reading the table -- that is worth failing loudly for.
        share = len(failures) / len(item_ids)
        logger.warning(
            "%d of %d listing(s) failed: %s",
            len(failures),
            len(item_ids),
            ", ".join(sorted(failures)[:10]),
        )
        if share > _MAX_FAILURE_SHARE:
            notify_best_effort(
                settings.slack.notify_channel,
                f"sync_active_listing_details aborted: {len(failures)} of {len(item_ids)} "
                "listings failed to fetch; snapshot not written.",
            )
            raise RuntimeError(
                f"{len(failures)} of {len(item_ids)} listing detail fetches failed "
                f"({share:.0%} > {_MAX_FAILURE_SHARE:.0%}); refusing to write a partial snapshot"
            )

    for d in details:
        d["start_time"] = datetime.fromisoformat(d["start_time"]).strftime("%Y-%m-%dT%H:%M:%SZ")
        d["file_date"] = now_str

    write_jsonl(Path(settings.paths.exports_dir) / "jsonl/active_listing_details.jsonl", details)

    gcs_client.upload_text(
        bucket=settings.gcs.ebay_bucket,
        object_name=f"logs/active_listing_details/active_listing_details_{now_file}.jsonl",
        text=(Path(settings.paths.exports_dir) / "jsonl/active_listing_details.jsonl").read_text(
            "utf-8"
        ),
        content_type="application/json",
    )
    bq_client.load_jsonl_from_gcs(
        bucket=settings.gcs.ebay_bucket,
        object_name=f"logs/active_listing_details/active_listing_details_{now_file}.jsonl",
        dataset=settings.bigquery.ebay_dataset,
        table="active_listing_details",
        schema_path=Path("configs/bigquery/schemas/active_listing_details.json"),
        write_disposition="WRITE_APPEND",
    )

    summary = f"Completed sync_active_listing_details.. {len(details)} listing(s)"
    if failures:
        summary += f", {len(failures)} failed"
    notify_best_effort(settings.slack.notify_channel, summary)


def main(*, max_workers: int = _DEFAULT_WORKERS):
    sync_active_listing_details(max_workers=max_workers)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Failed to update listing_details")
        raise
