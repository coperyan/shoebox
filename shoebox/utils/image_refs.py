"""Resolve a queue row's image reference to a local file.

A queue row's ``image_front`` / ``image_back`` can be one of three things:

- a local path (what the Streamlit file picker and the Excel loader produce);
- a bare scan number such as ``"123"`` (typed in the UI, the workbook or the iOS
  app), resolved to ``scans_dir/<scan_prefix><padded><scan_extension>`` by the
  configured scan naming convention;
- a ``gs://`` URI (a photo the iOS app uploaded through the API), downloaded to
  ``scans_dir/app_uploads/<basename>`` once and reused on re-runs.

A scan number whose file is not on disk is fetched from OneDrive when the
``onedrive`` settings are configured (the Cloud Run job started from the app
has no sync folder), into the very path the workstation would use, so the
image-log cache keys and GCS object names are identical on both hosts.

``create-listings`` calls :func:`materialize_scan` right before
``ImageLogClient.upload_image``, so the dedup cache, object naming, mirroring
and image log behave identically for all three kinds. The upload deletes the
local file; for a scan that came from OneDrive, :func:`archive_scan` then
moves the OneDrive original into ``onedrive.listed_subfolder`` so that the
scans folder, like ``scans_dir`` on the workstation, only holds unlisted cards.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from shoebox.clients.gcs import download_gs_file, is_gs_uri, parse_gs_uri
from shoebox.settings import Settings, get_settings
from shoebox.ui.helpers import handle_image_path

logger = logging.getLogger(__name__)

UPLOADS_SUBDIR = "app_uploads"

ImageSource = Literal["local", "onedrive", "gs", "cached"]


@dataclass(frozen=True)
class MaterializedImage:
    """A queue image as a local file, and where that file came from: already
    on disk (``local``), fetched from OneDrive by scan name (``onedrive``),
    downloaded from the app's ``gs://`` upload (``gs``), or gone but already
    in the image log, so its earlier upload is reused (``cached``, no file)."""

    path: Path
    source: ImageSource


def classify_image_ref(ref: str | None) -> Literal["blank", "gs", "path"]:
    if ref is None or not str(ref).strip():
        return "blank"
    if is_gs_uri(ref.strip()):
        return "gs"
    return "path"


# One client per Settings object, so a whole run shares one refreshed token.
_onedrive_clients: dict[int, Any] = {}
_onedrive_lock = threading.Lock()


def _default_onedrive(settings: Settings) -> Any:
    from shoebox.clients.onedrive import OneDriveClient

    key = id(settings)
    with _onedrive_lock:
        client = _onedrive_clients.get(key)
        if client is None:
            client = _onedrive_clients[key] = OneDriveClient(settings)
    return client


def scan_path(ref: str, *, settings: Settings | None = None) -> Path:
    """The local file :func:`materialize_scan` resolves ``ref`` to, whether or
    not it exists yet. Raises ValueError for a blank reference."""
    kind = classify_image_ref(ref)
    if kind == "blank":
        raise ValueError("empty image reference")
    ref = ref.strip()
    if kind == "gs":
        settings = settings or get_settings()
        _, name = parse_gs_uri(ref)
        basename = Path(name).name
        if not basename:
            raise ValueError(f"gs:// URI names a bucket, not an object: {ref!r}")
        return Path(settings.paths.scans_dir) / UPLOADS_SUBDIR / basename
    return Path(handle_image_path(ref))


def materialize_scan(
    ref: str,
    *,
    settings: Settings | None = None,
    download: Callable[[str, Path], Path] | None = None,
    onedrive: Any = None,
) -> MaterializedImage:
    """Return a local file for ``ref`` and its source (see module docstring).

    Raises ValueError for a blank reference. A ``gs://`` object that does not
    exist raises FileNotFoundError from the download; the caller (the per-row
    try/except in ``run_listings``) records it and moves on. ``download``
    defaults to :func:`download_gs_file`, looked up at call time so tests can
    patch it on this module.
    """
    if download is None:
        download = download_gs_file
    local = scan_path(ref, settings=settings)
    ref = ref.strip()
    if is_gs_uri(ref):
        if not local.exists():
            download(ref, local)
        return MaterializedImage(local, "gs")

    if local.exists():
        return MaterializedImage(local, "local")
    settings = settings or get_settings()
    if settings.onedrive.enabled:
        if onedrive is None:
            onedrive = _default_onedrive(settings)
        onedrive.download(local.name, local)
        return MaterializedImage(local, "onedrive")
    return MaterializedImage(local, "local")


def materialize_image(
    ref: str,
    *,
    settings: Settings | None = None,
    download: Callable[[str, Path], Path] | None = None,
    onedrive: Any = None,
) -> Path:
    """:func:`materialize_scan` for callers that only want the file."""
    return materialize_scan(ref, settings=settings, download=download, onedrive=onedrive).path


def archive_scan(
    image: MaterializedImage,
    *,
    settings: Settings | None = None,
    onedrive: Any = None,
) -> str | None:
    """After the upload: move a scan that was fetched from OneDrive into
    ``onedrive.listed_subfolder``; returns its new OneDrive path, or None when
    there was nothing to move (a local file, an app photo, the feature off).

    Best-effort, like the workstation's delete of the original: the listing
    is already made, so a failure here is logged, never raised. A 403 means
    the token predates the ``Files.ReadWrite`` scope.
    """
    if image.source != "onedrive":
        return None
    settings = settings or get_settings()
    subfolder = settings.onedrive.listed_subfolder.strip("/")
    if not subfolder or not settings.onedrive.enabled:
        return None
    if onedrive is None:
        onedrive = _default_onedrive(settings)
    try:
        return onedrive.move(image.path.name, subfolder)
    except Exception:
        logger.warning(
            "Could not move %s into OneDrive %s/%s; it stays in the scans folder",
            image.path.name,
            settings.onedrive.scans_folder,
            subfolder,
            exc_info=True,
        )
        return None
