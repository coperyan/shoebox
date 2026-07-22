from __future__ import annotations

import csv
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from shoebox.clients.bigquery import BigQueryClient
from shoebox.clients.gcs import GCSClient

TransformFn = Callable[[dict[str, str], str], dict]  # (csv_row, source_file) -> bq_row_dict


@dataclass(frozen=True)
class TableAssetConfig:
    name: str
    local_csv_path: Path

    # GCS staging
    gcs_bucket: str
    gcs_prefix: str  # e.g. "metadata/checklist"

    # BigQuery target
    bq_dataset: str
    bq_table: str
    schema_path: Path

    # Load behavior
    write_disposition: str = "WRITE_APPEND"
    jsonl_filename: str | None = None


class TableAsset:
    """
    Generic: Local CSV -> (transform) -> JSONL -> upload to GCS -> load to BigQuery.
    Differences per table come from config + transform function.
    """

    def __init__(
        self,
        cfg: TableAssetConfig,
        *,
        transform: TransformFn,
        bq: BigQueryClient | None = None,
        gcs: GCSClient | None = None,
    ):
        self.cfg = cfg
        self.transform = transform
        self.bq = bq or BigQueryClient()
        self.gcs = gcs or GCSClient()

    def iter_csv_rows(self) -> Iterable[dict[str, str]]:
        def _norm_header(h: str) -> str:
            # strip whitespace + remove UTF-8 BOM if present
            return (h or "").strip().lstrip("\ufeff")

        with self.cfg.local_csv_path.open("r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                raise ValueError(f"No headers found in CSV: {self.cfg.local_csv_path}")

            # normalize headers once
            norm_fieldnames = [_norm_header(h) for h in reader.fieldnames]
            reader.fieldnames = norm_fieldnames

            for row in reader:
                # normalize keys on each row too (defensive)
                yield {_norm_header(k): v for k, v in row.items()}

    def iter_transformed_rows(self) -> Iterable[dict]:
        source_file = self.cfg.local_csv_path.name
        for row in self.iter_csv_rows():
            yield self.transform(row, source_file)

    def write_jsonl(self, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        jsonl_name = self.cfg.jsonl_filename or f"{self.cfg.name}.jsonl"
        jsonl_path = output_dir / jsonl_name

        with jsonl_path.open("w", encoding="utf-8") as f:
            for r in self.iter_transformed_rows():
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        return jsonl_path

    def upload_jsonl_to_gcs(self, jsonl_path: Path) -> str:
        # NEW: append UTC timestamp to blob name
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")

        stem = jsonl_path.stem
        suffix = jsonl_path.suffix
        blob_name = f"{stem}_{ts}{suffix}"

        object_name = f"{self.cfg.gcs_prefix.rstrip('/')}/{blob_name}"

        self.gcs.upload_text(
            bucket=self.cfg.gcs_bucket,
            object_name=object_name,
            text=jsonl_path.read_text(encoding="utf-8"),
            content_type="application/json",
        )
        return object_name

    def load_to_bigquery(self, gcs_object_name: str) -> None:
        self.bq.load_jsonl_from_gcs(
            bucket=self.cfg.gcs_bucket,
            object_name=gcs_object_name,
            dataset=self.cfg.bq_dataset,
            table=self.cfg.bq_table,
            schema_path=self.cfg.schema_path,
            write_disposition=self.cfg.write_disposition,
        )

    def run(self, output_dir: Path) -> Path:
        jsonl_path = self.write_jsonl(output_dir=output_dir)
        gcs_object = self.upload_jsonl_to_gcs(jsonl_path)
        self.load_to_bigquery(gcs_object_name=gcs_object)
        return jsonl_path
