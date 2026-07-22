from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import Field

from ..common import Model


class Money(Model):
    currency: str | None = None
    value: str | None = None

    @property
    def decimal(self) -> Decimal | None:
        if self.value is None:
            return None
        try:
            return Decimal(self.value)
        except (InvalidOperation, ValueError):
            return None


class PricingSummary(Model):
    price: Money | None = None
    auction_start_price: Money | None = None
    auction_reserve_price: Money | None = None
    minimum_advertised_price: Money | None = None
    original_retail_price: Money | None = None
    pricing_visibility: str | None = None


class Listing(Model):
    listing_id: str | None = None
    listing_status: str | None = None
    sold_quantity: int | None = None
    listing_on_hold: Any | None = None


class ListingPolicies(Model):
    fulfillment_policy_id: str | None = None
    payment_policy_id: str | None = None
    return_policy_id: str | None = None

    best_offer_terms: Any | None = None
    e_bay_plus_if_eligible: bool | None = None

    # keep the rest flexible / optional
    product_compliance_policy_ids: Any | None = None
    regional_product_compliance_policies: Any | None = None
    regional_take_back_policies: Any | None = None
    shipping_cost_overrides: Any | None = None
    take_back_policy_id: Any | None = None


class Tax(Model):
    apply_tax: bool | None = None
    third_party_tax_category: Any | None = None
    vat_percentage: Any | None = None


class Offer(Model):
    """
    Typed view over an eBay offer record (sell/inventory offer).
    Designed to match your sample payload; unknown fields are ignored.
    """

    offer_id: str
    sku: str | None = None

    marketplace_id: str | None = None
    merchant_location_key: str | None = None

    format: str | None = None  # FIXED_PRICE, AUCTION, etc.
    status: str | None = None  # PUBLISHED, UNPUBLISHED, etc.

    category_id: str | None = None
    secondary_category_id: str | None = None

    listing_duration: str | None = None
    listing_start_date: str | None = None  # keep as str; parse later if needed

    available_quantity: int | None = None
    quantity_limit_per_buyer: int | None = None
    lot_size: int | None = None

    hide_buyer_details: bool | None = None
    include_catalog_product_details: bool | None = None

    listing: Listing | None = None
    listing_policies: ListingPolicies | None = None

    pricing_summary: PricingSummary | None = None
    listing_description: str | None = None

    store_category_names: list[str] = Field(default_factory=list)

    tax: Tax | None = None

    # keep raw payload for debugging/auditing
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Offer:
        # Support both snake_case and camelCase payloads safely
        if "offerId" in data and "offer_id" not in data:
            data = {**data, "offer_id": data.get("offerId")}
        return cls.model_validate({**data, "raw": data})

    # ---- Convenience helpers ----
    @property
    def listing_id(self) -> str | None:
        return self.listing.listing_id if self.listing else None

    @property
    def listing_status(self) -> str | None:
        return self.listing.listing_status if self.listing else None

    @property
    def price(self) -> Decimal | None:
        """
        Returns Decimal price (e.g., Decimal('1.49')) if available.
        """
        if not self.pricing_summary or not self.pricing_summary.price:
            return None
        return self.pricing_summary.price.decimal

    @property
    def currency(self) -> str | None:
        if not self.pricing_summary or not self.pricing_summary.price:
            return None
        return self.pricing_summary.price.currency
