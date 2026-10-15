"""Resolve a queue row's image reference to a local file.

A queue row's ``image_front`` / ``image_back`` can be one of three things:

- a local path (what the Streamlit file picker and the Excel loader produce);
- a bare scan number such as ``"123"`` (typed in the UI, the workbook or the iOS
  app), resolved to ``scans_dir/<scan_prefix><padded><scan_extension>`` by the
  configured scan naming convention;
- a ``gs://`` URI (a photo the iOS app uploaded through the API), downloaded to
  ``scans_dir/app_uploads/<basename>`` once and reused on re-runs.

``create-listings`` calls :func:`materialize_image` right before
``ImageLogClient.upload_image``, so the dedup cache, object naming, mirroring
and image log behave identically for all three kinds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from shoebox.clients.gcs import download_gs_file, is_gs_uri, parse_gs_uri
from shoebox.settings import Settings, get_settings
from shoebox.ui.helpers import handle_image_path

UPLOADS_SUBDIR = "app_uploads"


def classify_image_ref(ref: str | None) -> Literal["blank", "gs", "path"]:
    if ref is None or not str(ref).strip():
        return "blank"
    if is_gs_uri(ref.strip()):
        return "gs"
    return "path"


def materialize_image(
    ref: str,
    *,
    settings: Settings | None = None,
    download=download_gs_file,
) -> Path:
    """Return a local file for ``ref`` (see module docstring).

    Raises ValueError for a blank reference. A ``gs://`` object that does not
    exist raises FileNotFoundError from the download; the caller (the per-row
    try/except in ``run_listings``) records it and moves on.
    """
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
        dest = Path(settings.paths.scans_dir) / UPLOADS_SUBDIR / basename
        if not dest.exists():
            download(ref, dest)
        return dest

    return Path(handle_image_path(ref))
