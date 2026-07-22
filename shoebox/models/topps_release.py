from __future__ import annotations

import re
from datetime import date

from pydantic import Field

from shoebox.models.common import Model


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    return s or "release"


class ToppsRelease(Model):
    """A single product release scraped from topps.com/release-calendar."""

    name: str
    release_date: date
    url: str | None = None
    sport: str | None = None
    description: str | None = None
    is_pre_order: bool = False
    source_id: str | None = Field(
        default=None,
        description="Stable id from the source page if available (e.g. product slug).",
    )

    @property
    def stable_key(self) -> str:
        """Stable, idempotent key used to upsert the calendar event.

        Combines a slug of the source identifier (or name) with the release
        date so that re-runs match the same event even if the name's casing
        or punctuation drifts slightly between scrapes.
        """
        base = self.source_id or self.name
        return f"{_slugify(base)}-{self.release_date.isoformat()}"
