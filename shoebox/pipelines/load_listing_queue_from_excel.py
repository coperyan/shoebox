import logging
from pathlib import Path

import pandas as pd

from shoebox.clients.gcs import GCSClient, parse_gs_uri
from shoebox.models.listing_queue import ListingQueueRow
from shoebox.settings import get_settings
from shoebox.transforms.queue_enrichment import (
    build_enriched_json,
    load_checklist_and_parallels,
)
from shoebox.ui.helpers import handle_image_path
from shoebox.utils.jsonl import write_jsonl

logger = logging.getLogger(__name__)


def get_input_df():
    settings = get_settings()
    input_path = Path(settings.paths.data_dir) / "Inputs (Param).xlsm"
    df = pd.read_excel(
        input_path,
        sheet_name="Inputs",
        dtype={"Card #": str, "Image Front": str, "Image Back": str},
    )
    missing_images = df[df["Image Front"].isnull() | df["Image Back"].isnull()]
    if len(missing_images) > 0:
        logger.warning(
            "Dropping %d rows w/ missing images:\n%s",
            len(missing_images),
            missing_images.head().to_string(),
        )
        df = df[~df["ID"].isin(missing_images.ID.values)]
    return df


def write_enriched_json(df: pd.DataFrame) -> Path:
    settings = get_settings()
    export_path = Path(settings.paths.exports_dir) / "jsonl/listing_queue_enriched.jsonl"
    cdf, pdf = load_checklist_and_parallels()
    enriched_rows = []
    for _, row in df.iterrows():
        enriched_rows.append(
            build_enriched_json(
                ListingQueueRow(
                    set_name=row["Set Name"],
                    subset_name=row["Subset Name"],
                    parallel_variety=(
                        row["Parallel/Variety"] if not pd.isnull(row["Parallel/Variety"]) else None
                    ),
                    card_number=row["Card #"],
                    quantity=row["Quantity"],
                    price=(row["Price"] if not pd.isnull(row["Price"]) else 0.00),
                    image_front=handle_image_path(str(row["Image Front"])),
                    image_back=handle_image_path(str(row["Image Back"])),
                ),
                cdf=cdf,
                pdf=pdf,
            )
        )
    write_jsonl(export_path, enriched_rows)
    return export_path


def upload_queue_file(local_path: Path, queue_uri: str, gcs: GCSClient | None = None) -> None:
    """Copy the local queue over the ``paths.queue_uri`` object, which is what
    ``create-listings`` reads when that setting is on -- without this it would
    list whatever the GCS queue last held instead of the file just written."""
    bucket, name = parse_gs_uri(queue_uri)
    (gcs or GCSClient()).upload_text(
        bucket=bucket,
        object_name=name,
        text=local_path.read_text(encoding="utf-8"),
        content_type="application/json",
    )
    logger.info("Copied %s to %s", local_path, queue_uri)


def create_queue_file():
    df = get_input_df()
    export_path = write_enriched_json(df)
    queue_uri = get_settings().paths.queue_uri
    if queue_uri:
        upload_queue_file(export_path, queue_uri)
    logger.info(
        "Wrote %d records to the enriched jsonl:\n%s",
        len(df),
        df[["Set Name", "Subset Name", "Parallel/Variety", "Card #"]].to_string(index=False),
    )
