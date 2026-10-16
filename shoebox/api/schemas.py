"""Request/response models for the listing-queue API (docs/api.md)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shoebox.models.listing_queue import ListingQueueRow
from shoebox.settings import normalize_onedrive_folder

MAX_ROWS = 500


class QueueRowIn(BaseModel):
    """The 8 user-entered queue fields, as the phone sends them."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    set_name: str = Field(min_length=1)
    subset_name: str = Field(min_length=1)
    parallel_variety: str | None = None
    card_number: str = Field(min_length=1)
    quantity: int = Field(default=1, ge=1, le=999)
    price: float = Field(default=0.0, ge=0)
    # A bare scan number, a workstation path, or a gs:// URI from POST /images.
    image_front: str = ""
    image_back: str = ""

    def to_queue_row(self) -> ListingQueueRow:
        return ListingQueueRow(
            set_name=self.set_name,
            subset_name=self.subset_name,
            parallel_variety=self.parallel_variety or "",
            card_number=self.card_number,
            quantity=self.quantity,
            price=self.price,
            image_front=self.image_front,
            image_back=self.image_back,
        )


class DraftRowIn(QueueRowIn):
    """Same shape, but a draft may hold half-filled rows."""

    set_name: str = ""
    subset_name: str = ""
    card_number: str = ""


class QueueRowsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[QueueRowIn] = Field(min_length=1, max_length=MAX_ROWS)


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[DraftRowIn] = Field(default_factory=list, max_length=MAX_ROWS)


class Counts(BaseModel):
    rows: int
    ok: int
    errors: int


class RowResult(BaseModel):
    index: int
    ok: bool
    enriched: dict[str, Any] | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)


class PreviewResponse(BaseModel):
    rows: list[RowResult]
    counts: Counts
    metadata_version: str


class QueueWriteResponse(BaseModel):
    kind: Literal["queue", "draft"]
    submitted_at: datetime
    counts: Counts
    queue_uri: str | None
    draft_uri: str
    metadata_version: str | None


class QueueGetResponse(BaseModel):
    kind: Literal["queue", "draft"] | None
    submitted_at: datetime | None
    rows: list[DraftRowIn]
    counts: Counts
    queue_uri: str
    draft_uri: str


class CardMeta(BaseModel):
    card_number: str
    derived_card_number: str | None = None
    card_id: str | None = None
    subset_id: str | None = None
    set_year: str | None = None
    subset_type: str | None = None
    player: str | None = None
    team: str | None = None
    note: str | None = None
    note_check: str | None = None


class ParallelMeta(BaseModel):
    parallel_variety: str
    parallel_id: str | None = None
    print_run: int | None = None
    other_note: str | None = None


class SubsetMeta(BaseModel):
    subset_name: str
    cards: list[CardMeta]
    parallels: list[ParallelMeta]


class SetMeta(BaseModel):
    set_name: str
    set_year: str | None = None
    subsets: list[SubsetMeta]


class MetadataResponse(BaseModel):
    version: str
    generated_at: datetime
    ttl_s: int
    counts: dict[str, int]
    sets: list[SetMeta]


class MetadataRefreshResponse(BaseModel):
    version: str
    generated_at: datetime
    counts: dict[str, int]


class ImageUploadResponse(BaseModel):
    uri: str
    side: Literal["front", "back"]
    client_id: str | None
    bytes: int
    content_type: str


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


class StartListingRunRequest(BaseModel):
    """``POST /queue/listings``: the choices the app offers. Prices are never
    scraped on an app-triggered run. ``scans_folder`` reads the scans from
    that OneDrive folder instead of ``onedrive.scans_folder``; blank or absent
    keeps the configured one."""

    model_config = ConfigDict(extra="forbid")
    publish: bool = True
    schedule: bool = True
    scans_folder: str | None = None

    @field_validator("scans_folder")
    @classmethod
    def _folder(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return normalize_onedrive_folder(value)


class ListingRunsResponse(BaseModel):
    runs: list[dict[str, Any]]
