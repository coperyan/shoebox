import logging
from datetime import UTC, datetime
from pathlib import Path

from shoebox.clients.bigquery import BigQueryClient
from shoebox.clients.ebay_rest.client import EbayClient
from shoebox.clients.gcs import GCSClient
from shoebox.settings import get_settings
from shoebox.utils.jsonl import write_jsonl

logger = logging.getLogger(__name__)


def sync_active_listing_details():
    ebay_api = EbayClient()
    gcs_client = GCSClient()
    bq_client = BigQueryClient()
    settings = get_settings()

    active_listings = ebay_api.legacy_api.get_active_listings()

    now = datetime.now(UTC)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_file = now.strftime("%Y_%m_%d_%H_%M_S")

    details = []
    ctr = 0
    for listing in active_listings:
        ctr += 1
        if "complete your set" in listing["title"].lower():
            continue
        d = ebay_api.legacy_api.get_item_details(item_id=listing["item_id"])
        details.append(d)
        if ctr % 10 == 0:
            logger.info("Completed listing %d of %d", ctr, len(active_listings))

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


def main():
    sync_active_listing_details()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Failed to update listing_details")
        raise
