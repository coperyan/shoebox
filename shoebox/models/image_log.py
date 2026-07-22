from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import Field

from .common import Model, utc_now


class ImageLogEntry(Model):
    """One uploaded image (front/back/etc.) persisted to BigQuery via JSONL.

    We log *listing metadata* rather than an opaque card_id.

    Privacy / size notes:
    - We intentionally do NOT log file bytes.
    - We intentionally do NOT log file size.
    - We intentionally do NOT log a file-content SHA256.

    Cache behavior:
    - We cache by a deterministic `cache_key` derived from
      (set_name, subset_name, parallel_variety, card_number, side, original filename)
      and the target bucket/prefix.
    """

    # listing metadata
    set_name: str = Field(..., description="Checklist set name")
    subset_name: str = Field(..., description="Checklist subset name")
    parallel_variety: str | None = Field(default=None, description="Parallel/variety (nullable)")
    card_number: str = Field(..., description="Card number")
    side: str | None = Field(default=None, description="front|back|other")

    # local source
    local_path: str = Field(..., description="Original local file path")
    original_filename: str = Field(..., description="Original filename")
    content_type: str | None = Field(default=None, description="image/jpeg, image/png, image/webp")

    # remote destination
    gcs_bucket: str
    gcs_object: str
    public_url: str | None = None

    # bookkeeping
    uploaded_at_utc: datetime = Field(default_factory=utc_now)

    # helpful for dedupe across runs
    cache_key: str = Field(
        ..., description="Deterministic cache key (bucket|metadata|side|filename)"
    )


def build_cache_key(
    *,
    bucket: str,
    set_name: str,
    subset_name: str,
    parallel_variety: str | None,
    card_number: str,
    side: str | None,
    filename: str,
) -> str:
    pv = (parallel_variety or "").strip()
    s = (side or "").strip()
    return "|".join(
        [
            bucket,
            set_name.strip(),
            subset_name.strip(),
            pv,
            card_number.strip(),
            s,
            filename.strip(),
        ]
    )


def guess_content_type(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    return None
