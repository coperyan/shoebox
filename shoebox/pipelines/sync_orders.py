import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from shoebox.clients.bigquery import BigQueryClient
from shoebox.clients.ebay.client import EbayClient
from shoebox.clients.gcs import GCSClient
from shoebox.settings import get_settings
from shoebox.utils.jsonl import write_jsonl
from shoebox.utils.slack import notify_best_effort

logger = logging.getLogger(__name__)


def _serialize(obj):
    """JSON serializer for Decimal values produced by Order helpers."""
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def sync_orders():
    """
    Download orders from eBay, write to JSONL, upload to GCS, and load into BigQuery.

    Each order is exploded into one row per line item, matching the flattened
    schema in configs/bigquery/schemas/orders.json.
    """

    ebay_api = EbayClient()
    gcs_client = GCSClient()
    bq_client = BigQueryClient()
    settings = get_settings()

    notify_best_effort(settings.slack.notify_channel, "Starting sync_orders..")

    now = datetime.now(UTC)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_file = now.strftime("%Y_%m_%d_%H_%M_S")
    start_date = (now - timedelta(days=725)).strftime("%Y-%m-%d")

    logger.info("Fetching orders from eBay Fulfillment API...")
    orders = ebay_api.fulfillment.get_orders(start_date=start_date, status="FULFILLED")
    logger.info("Fetched %d orders.", len(orders))

    rows = []
    for order in orders:
        for row in order.flattened_line_items:
            row["file_date"] = now_str
            row.pop("variation_aspects", None)
            rows.append(row)

    logger.info("Exploded to %d line-item rows.", len(rows))

    export_path = Path(settings.paths.exports_dir) / "jsonl/orders.jsonl"
    write_jsonl(export_path, rows, default=_serialize)

    gcs_object = f"logs/orders/orders_{now_file}.jsonl"
    logger.info("Uploading to GCS: gs://%s/%s", settings.gcs.ebay_bucket, gcs_object)
    gcs_client.upload_text(
        bucket=settings.gcs.ebay_bucket,
        object_name=gcs_object,
        text=export_path.read_text("utf-8"),
        content_type="application/json",
    )

    logger.info("Loading into BigQuery: ebay.orders")
    bq_client.load_jsonl_from_gcs(
        bucket=settings.gcs.ebay_bucket,
        object_name=gcs_object,
        dataset=settings.bigquery.ebay_dataset,
        table="orders",
        schema_path=Path("configs/bigquery/schemas/orders.json"),
        write_disposition="WRITE_APPEND",
    )

    logger.info("Load from GCS to BQ complete. %d line-item rows loaded.", len(rows))

    bq_client.run_query(sql="tbl_orders_current.sql", return_df=False)

    logger.info("Created orders_current table. Sync-orders done.")
    notify_best_effort(settings.slack.notify_channel, "Completed sync_orders..")
