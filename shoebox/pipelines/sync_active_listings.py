import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from shoebox.clients.bigquery import BigQueryClient
from shoebox.clients.ebay_rest.client import EbayClient
from shoebox.clients.gcs import GCSClient
from shoebox.settings import get_settings
from shoebox.utils.slack import notify

logger = logging.getLogger(__name__)


def sync_active_listings():
    ebay_api = EbayClient()
    gcs_client = GCSClient()
    bq_client = BigQueryClient()
    settings = get_settings()

    logger.info("Starting sync_active_listings..")
    notify(settings.slack.notify_channel, "Starting sync_active_listings..")

    active_listings = ebay_api.legacy_api.get_active_listings()

    now = datetime.now(UTC)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_file = now.strftime("%Y_%m_%d_%H_%M_S")

    ## Get traffic report
    max_date = datetime.now().strftime("%Y%m%d")
    min_date = (datetime.now() - timedelta(days=89)).strftime("%Y%m%d")
    logger.info(f"Min Date:{min_date}, Max Date: {max_date}")

    traffic_report = ebay_api.analytics.get_traffic_report(
        date_from=min_date,
        date_to=max_date,
        listing_ids=[x["item_id"] for x in active_listings],
    )
    traffic_df = pd.json_normalize(traffic_report)
    listing_df = pd.json_normalize(active_listings)
    listing_df = listing_df.merge(traffic_df, how="left", left_on="item_id", right_on="LISTING_ID")
    logger.info(f"Pulled {len(listing_df)} listings..")
    listing_df.rename(
        columns={
            "LISTING_IMPRESSION_TOTAL": "impression_count",
            "LISTING_VIEWS_TOTAL": "view_count",
        },
        inplace=True,
    )
    listing_df["impression_count"] = listing_df["impression_count"].fillna(0)
    listing_df["view_count"] = listing_df["view_count"].fillna(0)
    listing_df.drop(columns=["LISTING_ID"], inplace=True)
    listing_df["price"] = listing_df["price"].astype(float)
    listing_df["quantity"] = listing_df["quantity"].astype(int)
    listing_df["impression_count"] = listing_df["impression_count"].astype(int)
    listing_df["view_count"] = listing_df["view_count"].astype(int)
    listing_df["start_time"] = pd.to_datetime(listing_df["start_time"])
    listing_df["start_time"] = listing_df["start_time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    listing_df["file_date"] = now_str

    listing_df.to_json(
        Path(settings.paths.exports_dir) / "jsonl/active_listings.jsonl",
        orient="records",
        lines=True,
    )

    gcs_client.upload_text(
        bucket=settings.gcs.ebay_bucket,
        object_name=f"logs/active_listings/active_listings_{now_file}.jsonl",
        text=(Path(settings.paths.exports_dir) / "jsonl/active_listings.jsonl").read_text("utf-8"),
        content_type="application/json",
    )
    bq_client.load_jsonl_from_gcs(
        bucket=settings.gcs.ebay_bucket,
        object_name=f"logs/active_listings/active_listings_{now_file}.jsonl",
        dataset=settings.bigquery.ebay_dataset,
        table="active_listings",
        schema_path=Path("configs/bigquery/schemas/active_listings.json"),
        write_disposition="WRITE_APPEND",
    )
    logger.info("Completed sync_active_listings")
    notify(settings.slack.notify_channel, "Completed sync_active_listings..")


def main():
    sync_active_listings()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Failed to update listings")
        raise
