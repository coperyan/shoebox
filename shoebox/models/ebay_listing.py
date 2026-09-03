from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from .common import Model, RawBlob, utc_now


class EbayListingDraft(Model):
    """A normalized, repo-owned representation of a listing we intend to create.

    This keeps your transforms stable even if you change eBay API libraries.
    """

    sku: str
    title: str
    # Full buyer-facing text (specifics + store footer) -> offer.listingDescription.
    description: str
    # Short specifics-only variant -> inventory item product.description, which
    # eBay caps at 4000 characters. Falls back to `description` when unset.
    item_description: str | None = None

    quantity: int = 1
    price: float = 0.0

    # eBay category/condition/etc. are typically defaults for cards
    category_id: str | None = None
    condition_id: str | None = None
    store_category: str | None = None
    listing_start_date: str | None = None

    # image urls (public)
    image_urls: list[str] = Field(default_factory=list)

    # item specifics (aspects)
    aspects: dict[str, Any] = Field(default_factory=dict)

    # optional advanced payload fragments
    inventory_item: dict[str, Any] = Field(default_factory=dict)
    offer: dict[str, Any] = Field(default_factory=dict)


class EbayListingResult(Model):
    """What happened when we attempted to create/publish a listing."""

    card_id: str | None = None
    sku: str

    created_at_utc: datetime = Field(default_factory=utc_now)

    # common identifiers
    inventory_item_group_key: str | None = None
    offer_id: str | None = None
    listing_id: str | None = None

    # status
    success: bool = True
    error_message: str | None = None

    # raw request/response blobs for debugging
    request: RawBlob = Field(default_factory=RawBlob)
    response: RawBlob = Field(default_factory=RawBlob)
