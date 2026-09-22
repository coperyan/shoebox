import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shoebox.clients.bigquery import BigQueryClient
from shoebox.clients.ebay.client import EbayClient
from shoebox.clients.ebay.trading import TradingQuotaExceeded
from shoebox.clients.gcs import GCSClient
from shoebox.settings import get_settings
from shoebox.utils.jsonl import write_jsonl
from shoebox.utils.slack import notify_best_effort

logger = logging.getLogger(__name__)

# Same reasoning as sync_active_listing_details: GetItem is one call per
# listing, so overlapping them is the only speedup.
_DEFAULT_WORKERS = 12

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _timestamp(value: Any) -> str | None:
    """eBay ISO time ("2026-09-01T12:00:00.000Z") -> BigQuery TIMESTAMP string."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).astimezone(UTC).strftime(_TS_FORMAT)
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def build_watch_list_rows(
    watched: list[dict[str, Any]],
    details: list[dict[str, Any]],
    failures: dict[str, str],
    *,
    file_date: str,
) -> list[dict[str, Any]]:
    """
    One row per watched listing, shaped to ``configs/bigquery/schemas/watch_list.json``.

    Buyer-side fields (seller, bids, time left, BIN price) come from the watch
    list; item specifics, category, condition, and pictures from GetItem. A
    listing whose GetItem call failed keeps its watch-list fields and carries
    the error in ``detail_error`` -- watched listings end all the time, so a
    missing detail is normal here rather than a reason to drop the row.
    """
    by_id = {str(d.get("item_id")): d for d in details}

    rows: list[dict[str, Any]] = []
    for w in watched:
        item_id = str(w.get("item_id") or "")
        d = by_id.get(item_id, {})
        rows.append(
            {
                "item_id": item_id,
                "title": w.get("title") or d.get("title"),
                "seller": w.get("seller"),
                "listing_type": w.get("listing_type"),
                "listing_status": w.get("listing_status") or d.get("listing_status"),
                "price": _float(w.get("price") or d.get("price")),
                "currency": w.get("currency") or d.get("currency"),
                "buy_it_now_price": _float(w.get("buy_it_now_price")),
                "bid_count": _int(w.get("bid_count")),
                "quantity": _int(w.get("quantity") or d.get("quantity")),
                "quantity_sold": _int(d.get("quantity_sold")),
                "time_left": w.get("time_left"),
                "start_time": _timestamp(w.get("start_time") or d.get("start_time")),
                "end_time": _timestamp(w.get("end_time") or d.get("end_time")),
                "view_item_url": w.get("view_item_url") or d.get("view_item_url"),
                "gallery_url": w.get("gallery_url"),
                "category_id": d.get("category_id"),
                "category_name": d.get("category_name"),
                "condition_id": d.get("condition_id"),
                "condition_display_name": d.get("condition_display_name"),
                "item_specifics": d.get("item_specifics"),
                "picture_urls": d.get("picture_urls"),
                "detail_error": None if d else failures.get(item_id, "no details returned"),
                "file_date": file_date,
            }
        )
    return rows


def sync_watch_list(*, max_workers: int = _DEFAULT_WORKERS):
    ebay_api = EbayClient()
    gcs_client = GCSClient()
    bq_client = BigQueryClient()
    settings = get_settings()

    notify_best_effort(settings.slack.notify_channel, "Starting sync_watch_list..")

    watched = ebay_api.trading.get_watch_list()
    logger.info("Pulled %d watched listing(s)", len(watched))

    now = datetime.now(UTC)
    now_str = now.strftime(_TS_FORMAT)
    now_file = now.strftime("%Y_%m_%d_%H_%M_S")

    item_ids = [str(w["item_id"]) for w in watched if w.get("item_id")]

    def log_progress(done: int, total: int) -> None:
        if done % 100 == 0 or done == total:
            logger.info("Fetched %d of %d watched listing detail(s)", done, total)

    started = time.monotonic()
    try:
        details, failures = ebay_api.trading.get_item_details_bulk(
            item_ids,
            max_workers=max_workers,
            on_progress=log_progress,
        )
    except TradingQuotaExceeded as e:
        notify_best_effort(settings.slack.notify_channel, f"sync_watch_list stopped: {e}")
        raise
    logger.info(
        "Fetched %d watched listing detail(s) in %.1fs (%d worker(s))",
        len(details),
        time.monotonic() - started,
        max_workers,
    )
    if failures:
        logger.warning(
            "%d of %d watched listing(s) returned no details: %s",
            len(failures),
            len(item_ids),
            ", ".join(sorted(failures)[:10]),
        )

    rows = build_watch_list_rows(watched, details, failures, file_date=now_str)

    local_path = Path(settings.paths.exports_dir) / "jsonl/watch_list.jsonl"
    object_name = f"logs/watch_list/watch_list_{now_file}.jsonl"
    write_jsonl(local_path, rows)

    gcs_client.upload_text(
        bucket=settings.gcs.ebay_bucket,
        object_name=object_name,
        text=local_path.read_text("utf-8"),
        content_type="application/json",
    )
    bq_client.load_jsonl_from_gcs(
        bucket=settings.gcs.ebay_bucket,
        object_name=object_name,
        dataset=settings.bigquery.ebay_dataset,
        table="watch_list",
        schema_path=Path("configs/bigquery/schemas/watch_list.json"),
        write_disposition="WRITE_APPEND",
    )

    summary = f"Completed sync_watch_list.. {len(rows)} listing(s)"
    if failures:
        summary += f", {len(failures)} without details"
    notify_best_effort(settings.slack.notify_channel, summary)


def main(*, max_workers: int = _DEFAULT_WORKERS):
    sync_watch_list(max_workers=max_workers)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Failed to sync watch list")
        raise
