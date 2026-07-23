from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd
from unidecode import unidecode

from shoebox.settings import ensure_runtime_dirs, get_settings
from shoebox.storage.table_asset import TableAsset, TableAssetConfig
from shoebox.transforms.checklist_normalize import checklist_transform
from shoebox.transforms.parallels_normalize import parallels_transform

logger = logging.getLogger(__name__)

# Master authoring workbook, kept locally under tools/ (gitignored — real data
# never ships). Both consumers derive from this one file: BigQuery (via this
# pipeline) and the Inputs.xlsm dropdowns (via Power Query reading the same
# local file). A structural template ships as
# tools/checklist_parallel_metadata.sample.xlsx.
MASTER_DB_FILENAME = "checklist_parallel_metadata.xlsm"


def update_csvs():
    settings = get_settings()
    master_path = Path(settings.paths.tools_dir) / MASTER_DB_FILENAME
    if not master_path.exists():
        raise FileNotFoundError(
            f"Master DB not found at {master_path}. "
            "Copy tools/checklist_parallel_metadata.sample.xlsx there and add your data."
        )
    for s in ["Checklist", "Parallels"]:
        df = pd.read_excel(master_path, sheet_name=s)
        df.rename(
            columns={
                c: c.replace(" ", "_").replace("/", "_").lower()
                for c in df.columns.values
            },
            inplace=True,
        )
        with open(f"configs/bigquery/schemas/{s.lower()}.json") as f:
            schema = json.load(f)
        df.drop(
            columns=[
                c for c in df.columns.values if c not in [s["name"] for s in schema]
            ],
            inplace=True,
        )
        if s == "Parallels":
            df["print_run"] = (
                pd.to_numeric(df["print_run"], errors="coerce").round().astype("Int64")
            )
        if s == "Checklist":
            df["player"] = df["player"].astype(str)
            df["player"] = df["player"].fillna("")
            df["player"] = df["player"].apply(lambda x: unidecode(x, "utf-8"))
        df.to_csv(Path(settings.paths.data_dir) / f"{s.lower()}.csv", index=False)


def sync_metadata() -> None:
    settings = get_settings()

    update_csvs()

    exports_dir = Path(settings.paths.exports_dir) / "jsonl"

    checklist_asset = TableAsset(
        TableAssetConfig(
            name="checklist",
            local_csv_path=Path(settings.paths.data_dir) / "checklist.csv",
            gcs_bucket=settings.gcs.metadata_bucket,
            gcs_prefix="metadata/checklist",
            bq_dataset=settings.bigquery.checklist_dataset,
            bq_table="checklist",
            schema_path=Path("configs/bigquery/schemas/checklist.json"),
            write_disposition="WRITE_TRUNCATE",
        ),
        transform=checklist_transform,
    )
    checklist_jsonl = checklist_asset.run(output_dir=exports_dir)

    parallels_asset = TableAsset(
        TableAssetConfig(
            name="parallels",
            local_csv_path=Path(settings.paths.data_dir) / "parallels.csv",
            gcs_bucket=settings.gcs.metadata_bucket,
            gcs_prefix="metadata/parallels",
            bq_dataset=settings.bigquery.checklist_dataset,
            bq_table="parallels",
            schema_path=Path("configs/bigquery/schemas/parallels.json"),
            write_disposition="WRITE_TRUNCATE",
        ),
        transform=parallels_transform,
    )
    parallels_jsonl = parallels_asset.run(output_dir=exports_dir)

    logger.info("Checklist loaded from: %s", checklist_jsonl)
    logger.info("Parallels loaded from: %s", parallels_jsonl)


def main() -> None:
    ensure_runtime_dirs()
    sync_metadata()


if __name__ == "__main__":
    main()
