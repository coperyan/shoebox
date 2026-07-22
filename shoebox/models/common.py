from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class Model(BaseModel):
    """Base for all models: consistent config + JSONL-safe dumps."""

    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    def to_bq_json(self) -> dict[str, Any]:
        """BigQuery load-friendly dict (JSON serializable)."""
        # BigQuery handles ISO8601 strings fine for TIMESTAMP/DATETIME loads.
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class RawBlob(Model):
    """A small wrapper for storing raw request/response payloads."""

    data: dict[str, Any] = Field(default_factory=dict)
