from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import Field, field_validator

from ..common import Model


class ItemPrice(Model):
    value: str | None = None
    currency: str | None = None

    @property
    def decimal(self) -> Decimal | None:
        if self.value is None:
            return None
        try:
            return Decimal(self.value)
        except (InvalidOperation, ValueError):
            return None


class ItemImage(Model):
    image_url: str | None = None


class ItemLocation(Model):
    postal_code: str | None = None
    country: str | None = None
    state_or_province: str | None = None
    city: str | None = None


class ItemSeller(Model):
    username: str | None = None
    feedback_score: int | None = None
    feedback_percentage: str | None = None


class ShippingCost(Model):
    shipping_cost: ItemPrice | None = None
    shipping_cost_type: str | None = None
    shipping_service_code: str | None = None


class ItemCategory(Model):
    category_id: str | None = None
    category_name: str | None = None


class ItemSummary(Model):
    item_id: str | None = None
    title: str | None = None
    leaf_category_ids: list[str] = Field(default_factory=list)
    categories: list[ItemCategory] = Field(default_factory=list)
    image: ItemImage | None = None
    price: ItemPrice | None = None
    item_href: str | None = None
    seller: ItemSeller | None = None
    condition: str | None = None
    condition_id: str | None = None
    item_location: ItemLocation | None = None
    buying_options: list[str] = Field(default_factory=list)
    item_web_url: str | None = None
    item_end_date: str | None = None
    # When the listing was created. The base Model ignores unknown keys, so this
    # was being silently discarded — it's the one freshness signal that doesn't
    # depend on our own seen-cache.
    item_origin_date: str | None = None
    current_bid_price: ItemPrice | None = None
    shipping_options: list[ShippingCost] = Field(default_factory=list)
    epid: str | None = None
    item_group_href: str | None = None
    thumbnail_images: list[ItemImage] = Field(default_factory=list)

    # eBay omits some array fields on some listings, but sends an explicit
    # `null` on others (shipping_options on local-pickup-only items, for
    # example). A default_factory only covers the omitted case, so without this
    # an explicit null raises and takes down the whole search.
    @field_validator(
        "leaf_category_ids",
        "categories",
        "buying_options",
        "shipping_options",
        "thumbnail_images",
        mode="before",
    )
    @classmethod
    def _null_list_is_empty(cls, v: Any) -> Any:
        return [] if v is None else v

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ItemSummary:
        return cls.model_validate(data)

    @property
    def price_decimal(self) -> Decimal | None:
        return self.price.decimal if self.price else None

    @property
    def current_bid_decimal(self) -> Decimal | None:
        return self.current_bid_price.decimal if self.current_bid_price else None

    @property
    def free_shipping(self) -> bool:
        for opt in self.shipping_options:
            if opt.shipping_cost and opt.shipping_cost.decimal == Decimal("0"):
                return True
        return False

    @property
    def is_auction(self) -> bool:
        return "AUCTION" in self.buying_options

    @property
    def is_buy_it_now(self) -> bool:
        return "FIXED_PRICE" in self.buying_options
