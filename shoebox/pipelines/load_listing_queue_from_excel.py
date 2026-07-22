import logging
from pathlib import Path

import pandas as pd

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
        input_path, sheet_name="Inputs", dtype={"Image Front": str, "Image Back": str}
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


def write_enriched_json(df: pd.DataFrame):
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


def create_queue_file():
    df = get_input_df()
    write_enriched_json(df)
    logger.info(
        "Wrote %d records to the enriched jsonl:\n%s",
        len(df),
        df[["Set Name", "Subset Name", "Parallel/Variety", "Card #"]].to_string(index=False),
    )
