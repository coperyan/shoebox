from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
from google.api_core.exceptions import BadRequest, GoogleAPICallError
from google.cloud import bigquery
from google.oauth2 import service_account

from shoebox.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class BigQueryClient:
    """Small BigQuery helper.

    Design:
    - No global settings evaluated at import time.
    - Accept Settings for testability and consistency.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        project_id: str | None = None,
        credentials_path: Path | None = None,
    ):
        settings = settings or get_settings()

        self.project_id = project_id or settings.gcp.project_id
        credentials_path = credentials_path or Path(settings.gcp.service_account_json)

        if credentials_path.exists():
            credentials = service_account.Credentials.from_service_account_file(
                str(credentials_path)
            )
            self.client = bigquery.Client(
                project=self.project_id,
                credentials=credentials,
            )
        else:
            # Fall back to ADC (useful for Cloud Run / dev w/ gcloud auth)
            logger.warning(
                "Service account JSON not found at %s; falling back to ADC.",
                credentials_path,
            )
            self.client = bigquery.Client(project=self.project_id)

        self.query_dir = Path(settings.paths.query_dir)

    def run_query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        return_df: bool = True,
    ) -> pd.DataFrame | None:
        """Run a SQL file located under settings.paths.query_dir."""

        sql_path = self.query_dir / sql
        if not sql_path.exists():
            raise FileNotFoundError(f"SQL file not found: {sql_path}")

        query = sql_path.read_text(encoding="utf-8")
        if params:
            query = query.format_map(params)

        try:
            job = self.client.query(query)
            job.result()
        except (BadRequest, GoogleAPICallError):
            logger.exception("BigQuery query failed")
            raise
        except Exception as e:
            logger.exception("Unexpected error running BigQuery query")
            raise RuntimeError(f"BigQuery query failed: {e}") from e

        if return_df:
            return job.to_dataframe()
        return None

    def load_jsonl_from_gcs(
        self,
        *,
        bucket: str,
        object_name: str,
        dataset: str,
        table: str,
        schema_path: Path | None = None,
        write_disposition: str = "WRITE_APPEND",
    ) -> None:
        """Load newline-delimited JSON from GCS into BigQuery."""

        table_id = f"{self.project_id}.{dataset}.{table}"
        uri = f"gs://{bucket}/{object_name}"

        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=write_disposition,
        )

        if schema_path:
            job_config.schema = self._load_schema(schema_path)

        load_job = self.client.load_table_from_uri(uri, table_id, job_config=job_config)
        load_job.result()

    @staticmethod
    def _load_schema(schema_path: Path) -> list[bigquery.SchemaField]:
        with schema_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        return [
            bigquery.SchemaField(
                name=field["name"],
                field_type=field["type"],
                mode=field.get("mode", "NULLABLE"),
                description=field.get("description"),
            )
            for field in raw
        ]
