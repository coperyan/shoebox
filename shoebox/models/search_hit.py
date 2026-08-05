"""Rows produced by the saved-search watcher.

Two distinct shapes:

- ``SeenEntry`` — local only. The dedup record: "search X has already seen item
  Y". Refreshed on every run so ``last_price`` stays current, which is the seam
  a future price-drop alert would build on.
- ``SearchHit`` — the durable BigQuery row for ``ebay.search_hits``. Flat, since
  ``BigQueryClient._load_schema`` has no nested RECORD support.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field

from .common import Model, utc_now
from .ebay.item_summary import ItemSummary

# The only hit type in v1. Price-drop and ending-soon alerts would add values
# here rather than change the schema.
HIT_TYPE_NEW_LISTING = "NEW_LISTING"


def build_cache_key(search_name: str, item_id: str) -> str:
    """Dedup key. Deliberately not '|'-joined: eBay item IDs are themselves
    pipe-delimited (``v1|123456789|0``), so that separator would be ambiguous."""
    return f"{search_name}\x1f{item_id}"


class SeenEntry(Model):
    """One item a search has already observed. Local cache only."""

    search_name: str
    item_id: str
    first_seen_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)
    last_price: Decimal | None = None
    last_price_currency: str | None = None
    title: str | None = None
    # False for seeded items and for hits past the max_notify cap -- they are
    # recorded precisely so they never alert.
    notified: bool = False

    @property
    def cache_key(self) -> str:
        return build_cache_key(self.search_name, self.item_id)


class SearchHit(Model):
    """One row of ``ebay.search_hits``. Field order matches the BQ schema."""

    run_id: str
    search_name: str
    item_id: str
    cache_key: str
    hit_type: str = HIT_TYPE_NEW_LISTING
    hit_at: datetime = Field(default_factory=utc_now)
    first_seen_at: datetime = Field(default_factory=utc_now)
    is_seed: bool = False
    notified: bool = False
    notified_at: datetime | None = None
    slack_channel: str | None = None
    slack_parent_ts: str | None = None

    title: str | None = None
    item_web_url: str | None = None
    thumbnail_url: str | None = None
    item_origin_date: datetime | None = None

    last_price: Decimal | None = None
    last_price_currency: str | None = None
    current_bid: Decimal | None = None
    shipping_cost: Decimal | None = None
    free_shipping: bool | None = None

    buying_options: list[str] = Field(default_factory=list)
    condition: str | None = None
    condition_id: str | None = None
    seller_username: str | None = None
    seller_feedback_score: int | None = None
    item_location_country: str | None = None
    leaf_category_id: str | None = None
    item_end_date: datetime | None = None

    @classmethod
    def from_item(
        cls,
        item: ItemSummary,
        *,
        search_name: str,
        run_id: str,
        hit_at: datetime,
        first_seen_at: datetime | None = None,
        is_seed: bool = False,
        notified: bool = False,
        notified_at: datetime | None = None,
        slack_channel: str | None = None,
        slack_parent_ts: str | None = None,
    ) -> SearchHit:
        # Imported here to avoid a transforms -> models -> transforms cycle.
        from ..transforms.search_filters import cheapest_shipping

        item_id = item.item_id or ""

        return cls(
            run_id=run_id,
            search_name=search_name,
            item_id=item_id,
            cache_key=build_cache_key(search_name, item_id),
            hit_at=hit_at,
            first_seen_at=first_seen_at or hit_at,
            is_seed=is_seed,
            notified=notified,
            notified_at=notified_at,
            slack_channel=slack_channel,
            slack_parent_ts=slack_parent_ts,
            title=item.title,
            item_web_url=item.item_web_url,
            # Stored at eBay's native size; the Slack renderer asks for its own.
            thumbnail_url=item.thumbnail(),
            item_origin_date=item.item_origin_date,
            last_price=item.price_decimal,
            last_price_currency=item.price.currency if item.price else None,
            current_bid=item.current_bid_decimal,
            shipping_cost=cheapest_shipping(item),
            free_shipping=item.free_shipping,
            buying_options=list(item.buying_options),
            condition=item.condition,
            condition_id=item.condition_id,
            seller_username=item.seller.username if item.seller else None,
            seller_feedback_score=item.seller.feedback_score if item.seller else None,
            item_location_country=(item.item_location.country if item.item_location else None),
            leaf_category_id=(item.leaf_category_ids[0] if item.leaf_category_ids else None),
            item_end_date=item.item_end_date,
        )
