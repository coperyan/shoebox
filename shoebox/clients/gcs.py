from __future__ import annotations

import logging
from pathlib import Path

from google.cloud import storage
from google.oauth2 import service_account

from shoebox.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class GCSClient:
    """Thin wrapper around google.cloud.storage.Client.

    - No global settings evaluated at import time.
    - Supports service-account JSON file or ADC.
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
            creds = service_account.Credentials.from_service_account_file(str(credentials_path))
            self.client = storage.Client(project=self.project_id, credentials=creds)
        else:
            logger.warning(
                "Service account JSON not found at %s; falling back to ADC.",
                credentials_path,
            )
            self.client = storage.Client(project=self.project_id)

    def upload_text(
        self,
        *,
        bucket: str,
        object_name: str,
        text: str,
        content_type: str = "text/plain",
    ) -> None:
        blob = self.client.bucket(bucket).blob(object_name)
        blob.upload_from_string(text, content_type=content_type)

    def upload_file(
        self,
        *,
        bucket: str,
        object_name: str,
        file_path: Path,
        content_type: str | None = None,
        make_public: bool = False,
    ) -> str:
        blob = self.client.bucket(bucket).blob(object_name)
        blob.upload_from_filename(filename=str(file_path), content_type=content_type)
        if make_public:
            blob.make_public()
            return blob.public_url
        return blob.name

    def make_public(self, *, bucket: str, object_name: str) -> str:
        blob = self.client.bucket(bucket).blob(object_name)
        blob.make_public()
        return blob.public_url
