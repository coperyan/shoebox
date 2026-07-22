from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from google.cloud import bigquery, storage
from google.oauth2 import service_account

from shoebox.clients.bigquery import BigQueryClient
from shoebox.models.image_log import ImageLogEntry
from shoebox.settings import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImageLogPaths:
    """Local files used to avoid re-uploading and to append new upload records."""

    cache_jsonl: Path
    append_jsonl: Path


class ImageLogClient:
    """Upload images to GCS and log net-new uploads.

    Features:
    - Uploads card images to settings.gcs.image_bucket
    - Maintains a local JSONL cache index to prevent re-upload
    - Writes net-new uploads to an append JSONL, which can be loaded to BigQuery
    - Retains a local mirrored copy of uploaded images under:
        settings.paths.scans_dir / 'GCS' / <object_name>

    NOTE: This client does not call `make_public`. If your bucket/object is public,
    the returned blob.public_url will be usable. Otherwise, treat it as a reference.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        gcs_prefix: str = "images",
        bq_table: str = "image_log",
        paths: ImageLogPaths | None = None,
        exports_dir: Path | None = None,
    ) -> None:
        self.settings = settings or get_settings()

        self.gcp_project = self.settings.gcp.project_id
        self.image_bucket = self.settings.gcs.image_bucket
        self.image_log_bucket = self.settings.gcs.image_log_bucket
        self.gcs_prefix = gcs_prefix.strip("/")

        self.bq_dataset = self.settings.bigquery.images_dataset
        self.bq_table = bq_table

        if exports_dir is None:
            exports_dir = Path(self.settings.paths.exports_dir) / "jsonl"
        exports_dir.mkdir(parents=True, exist_ok=True)

        if paths is None:
            paths = ImageLogPaths(
                cache_jsonl=exports_dir / "image_cache.jsonl",
                append_jsonl=exports_dir / "image_log_append.jsonl",
            )
        self.paths = paths
        self.paths.cache_jsonl.parent.mkdir(parents=True, exist_ok=True)
        self.paths.append_jsonl.parent.mkdir(parents=True, exist_ok=True)

        self._storage = self._build_storage_client()
        self._bq = self._build_bigquery_client()

        self._cache: dict[str, ImageLogEntry] = {}
        self._load_cache()

    def _build_storage_client(self) -> storage.Client:
        sa_path = Path(self.settings.gcp.service_account_json)
        if sa_path.exists():
            creds = service_account.Credentials.from_service_account_file(str(sa_path))
            return storage.Client(project=self.gcp_project, credentials=creds)
        logger.warning("Service account JSON not found at %s; using ADC.", sa_path)
        return storage.Client(project=self.gcp_project)

    def _build_bigquery_client(self) -> bigquery.Client:
        sa_path = Path(self.settings.gcp.service_account_json)
        if sa_path.exists():
            creds = service_account.Credentials.from_service_account_file(str(sa_path))
            return bigquery.Client(project=self.gcp_project, credentials=creds)
        logger.warning("Service account JSON not found at %s; using ADC.", sa_path)
        return bigquery.Client(project=self.gcp_project)

    # -----------------------------
    # Cache key + object naming
    # -----------------------------
    @staticmethod
    def build_cache_key(
        *,
        bucket: str,
        set_name: str,
        subset_name: str,
        parallel_variety: str | None,
        card_number: str,
        side: str | None,
        original_filename: str,
    ) -> str:
        pv = (parallel_variety or "").strip()
        s = (side or "").strip()
        return "|".join(
            [
                bucket.strip(),
                set_name.strip(),
                subset_name.strip(),
                pv,
                card_number.strip(),
                s,
                original_filename.strip(),
            ]
        )

    def _local_gcs_mirror_path(self, object_name: str) -> Path:
        base = Path(self.settings.paths.scans_dir) / "GCS"
        return base / object_name

    def build_object_name(
        self,
        *,
        set_name: str,
        subset_name: str,
        parallel_variety: str | None,
        card_number: str,
        side: str | None,
        original_filename: str,
    ) -> str:
        def clean(x: str) -> str:
            x = (x or "").strip().replace("\\", "/")
            x = x.replace(":", "-")
            return "_".join(x.split())

        pv = clean(parallel_variety) if parallel_variety else "base"
        s = clean(side) if side else "img"
        return "/".join(
            [
                self.gcs_prefix,
                clean(set_name),
                clean(subset_name),
                pv,
                clean(card_number),
                f"{s}__{clean(original_filename)}",
            ]
        )

    # -----------------------------
    # Local cache file (persistent)
    # -----------------------------
    def _load_cache(self) -> None:
        self._cache = {}
        if not self.paths.cache_jsonl.exists():
            return

        with self.paths.cache_jsonl.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    entry = ImageLogEntry(**obj)
                    self._cache[entry.cache_key] = entry
                except Exception:
                    continue

    def _upsert_cache_entry(self, entry: ImageLogEntry) -> None:
        self._cache[entry.cache_key] = entry
        tmp = self.paths.cache_jsonl.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for e in self._cache.values():
                f.write(e.model_dump_json() + "\n")
        tmp.replace(self.paths.cache_jsonl)

    def _append_new_upload(self, entry: ImageLogEntry) -> None:
        with self.paths.append_jsonl.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")

    def clear_append_log(self) -> None:
        if self.paths.append_jsonl.exists():
            self.paths.append_jsonl.unlink()

    # -----------------------------
    # Public API
    # -----------------------------
    def upload_image(
        self,
        *,
        file_path: Path,
        set_name: str,
        subset_name: str,
        card_number: str,
        parallel_variety: str | None = None,
        side: str | None = None,
        object_name: str | None = None,
        local_path_for_log: str | None = None,
        delete_original: bool = True,
    ) -> ImageLogEntry:
        file_path = Path(file_path)

        original_filename = file_path.name

        cache_key = self.build_cache_key(
            bucket=self.image_bucket,
            set_name=set_name,
            subset_name=subset_name,
            parallel_variety=parallel_variety,
            card_number=card_number,
            side=side,
            original_filename=original_filename,
        )

        if cache_key in self._cache:
            return self._cache[cache_key]
        else:
            if not file_path.exists():
                raise FileNotFoundError(str(file_path))

        if object_name is None:
            object_name = self.build_object_name(
                set_name=set_name,
                subset_name=subset_name,
                parallel_variety=parallel_variety,
                card_number=card_number,
                side=side,
                original_filename=original_filename,
            )

        bucket = self._storage.bucket(self.image_bucket)
        blob = bucket.blob(object_name)
        blob.upload_from_filename(str(file_path))

        # Retain a local mirrored copy
        local_gcs_path = self._local_gcs_mirror_path(object_name)
        local_gcs_path.parent.mkdir(parents=True, exist_ok=True)
        local_gcs_path.write_bytes(file_path.read_bytes())

        if delete_original:
            try:
                os.remove(file_path)
            except Exception:
                logger.warning("Failed to delete original file: %s", file_path)

        entry = ImageLogEntry(
            set_name=set_name,
            subset_name=subset_name,
            parallel_variety=parallel_variety,
            card_number=card_number,
            side=side,
            local_path=local_path_for_log or str(file_path),
            original_filename=original_filename,
            content_type=blob.content_type,
            gcs_bucket=self.image_bucket,
            gcs_object=object_name,
            public_url=getattr(blob, "public_url", None),
            uploaded_at_utc=datetime.now(UTC).isoformat(),
            cache_key=cache_key,
        )

        self._upsert_cache_entry(entry)
        self._append_new_upload(entry)
        return entry

    # -----------------------------
    # Append log -> GCS(log bucket) -> BigQuery
    # -----------------------------
    def upload_append_log_to_gcs(
        self,
        *,
        destination_prefix: str = "logs/image_log",
        filename: str | None = None,
    ) -> str | None:
        if not self.paths.append_jsonl.exists() or self.paths.append_jsonl.stat().st_size == 0:
            return None

        destination_prefix = destination_prefix.strip("/")
        if filename is None:
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            filename = f"image_log_append_{ts}.jsonl"

        object_name = f"{destination_prefix}/{filename}"
        bucket = self._storage.bucket(self.image_log_bucket)
        blob = bucket.blob(object_name)
        blob.upload_from_filename(str(self.paths.append_jsonl))

        return f"gs://{self.image_log_bucket}/{object_name}"

    def sync_append_log_to_bigquery(self, *, write_disposition: str = "WRITE_APPEND") -> bool:
        if not self.bq_dataset:
            raise ValueError("settings.bigquery.images_dataset is required")

        if not self.paths.append_jsonl.exists() or self.paths.append_jsonl.stat().st_size == 0:
            return False

        table_id = f"{self._bq.project}.{self.bq_dataset}.{self.bq_table}"

        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=write_disposition,
            schema=BigQueryClient._load_schema(Path("configs/bigquery/schemas/image_log.json")),
        )

        with self.paths.append_jsonl.open("rb") as f:
            job = self._bq.load_table_from_file(f, table_id, job_config=job_config)
        job.result()
        return True

    def flush_append_log(
        self,
        *,
        upload_to_gcs: bool = True,
        destination_prefix: str = "logs/image_log",
        sync_to_bigquery: bool = True,
        clear_after_success: bool = True,
    ) -> tuple[str | None, bool]:
        gs_uri = None
        if upload_to_gcs:
            gs_uri = self.upload_append_log_to_gcs(destination_prefix=destination_prefix)

        bq_loaded = False
        if sync_to_bigquery:
            bq_loaded = self.sync_append_log_to_bigquery(write_disposition="WRITE_APPEND")

        if clear_after_success and (gs_uri is not None or bq_loaded):
            self.clear_append_log()

        return gs_uri, bq_loaded
