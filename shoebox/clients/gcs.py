from __future__ import annotations

import logging
from pathlib import Path

from google.cloud import storage
from google.oauth2 import service_account

from shoebox.settings import Settings, get_settings

logger = logging.getLogger(__name__)

GS_SCHEME = "gs://"


def is_gs_uri(value: object) -> bool:
    return isinstance(value, str) and value.startswith(GS_SCHEME)


def parse_gs_uri(uri: str) -> tuple[str, str]:
    """``gs://bucket/some/path`` -> ``("bucket", "some/path")``.

    The path may be empty (a bare bucket); surrounding slashes are dropped so
    callers can join onto it without doubling them.
    """
    if not is_gs_uri(uri):
        raise ValueError(f"not a gs:// URI: {uri!r}")
    bucket, _, name = uri[len(GS_SCHEME) :].partition("/")
    if not bucket:
        raise ValueError(f"gs:// URI has no bucket: {uri!r}")
    return bucket, name.strip("/")


def read_gs_text(uri: str, *, client: GCSClient | None = None) -> str:
    """Download one object as text. A missing object raises FileNotFoundError,
    matching what a local path would do, so callers need no GCS-specific
    error handling."""
    from google.api_core.exceptions import NotFound

    bucket, name = parse_gs_uri(uri)
    if not name:
        raise ValueError(f"gs:// URI names a bucket, not an object: {uri!r}")
    client = client or GCSClient()
    blob = client.client.bucket(bucket).blob(name)
    try:
        return blob.download_as_text(encoding="utf-8")
    except NotFound as exc:
        raise FileNotFoundError(f"Missing object: {uri}") from exc


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
