from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
from pydantic import Field

from ..common import Model


# -------- Money / Amount --------
class Amount(Model):
    currency: str | None = None
    value: str | None = None
    converted_from_currency: str | None = None
    converted_from_value: str | None = None

    @property
    def decimal(self) -> Decimal | None:
        if self.value is None:
            return None
        try:
            return Decimal(self.value)
        except (InvalidOperation, ValueError):
            return None


# -------- Addresses / Contact --------
class ContactAddress(Model):
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    country_code: str | None = None
    county: str | None = None
    postal_code: str | None = None
    state_or_province: str | None = None


class Phone(Model):
    phone_number: str | None = None


class BuyerRegistrationAddress(Model):
    company_name: str | None = None
    contact_address: ContactAddress | None = None
    email: str | None = None
    full_name: str | None = None
    primary_phone: Phone | None = None


class TaxAddress(Model):
    city: str | None = None
    country_code: str | None = None
    postal_code: str | None = None
    state_or_province: str | None = None


class Buyer(Model):
    buyer_registration_address: BuyerRegistrationAddress | None = None
    tax_address: TaxAddress | None = None
    tax_identifier: Any | None = None
    username: str | None = None


# -------- Cancel status --------
class CancelStatus(Model):
    cancelled_date: str | None = None
    cancel_requests: list[Any] = Field(default_factory=list)
    cancel_state: str | None = None


# -------- Fulfillment instructions --------
class ShipTo(Model):
    company_name: str | None = None
    contact_address: ContactAddress | None = None
    email: str | None = None
    full_name: str | None = None
    primary_phone: Phone | None = None


class ShippingStep(Model):
    shipping_carrier_code: str | None = None
    shipping_service_code: str | None = None
    ship_to: ShipTo | None = None
    ship_to_reference_id: Any | None = None


class FulfillmentStartInstruction(Model):
    appointment: Any | None = None
    ebay_supported_fulfillment: bool | None = None
    final_destination_address: Any | None = None
    fulfillment_instructions_type: str | None = None
    max_estimated_delivery_date: str | None = None
    min_estimated_delivery_date: str | None = None
    pickup_step: Any | None = None
    shipping_step: ShippingStep | None = None


# -------- Line item pieces --------
class DeliveryCost(Model):
    discount_amount: Any | None = None
    handling_cost: Any | None = None
    import_charges: Any | None = None
    shipping_cost: Amount | None = None
    shipping_intermediation_fee: Any | None = None


class EbayCollectAndRemitTax(Model):
    amount: Amount | None = None
    ebay_reference: Any | None = None
    tax_type: str | None = None
    collection_method: str | None = None


class ItemLocation(Model):
    country_code: str | None = None
    location: str | None = None
    postal_code: str | None = None


class VariationAspect(Model):
    name: str | None = None
    value: str | None = None


class LineItemFulfillmentInstructions(Model):
    guaranteed_delivery: bool | None = None
    max_estimated_delivery_date: str | None = None
    min_estimated_delivery_date: str | None = None
    ship_by_date: str | None = None


class LineItemProperties(Model):
    buyer_protection: bool | None = None
    from_best_offer: Any | None = None
    sold_via_ad_campaign: bool | None = None


class LineItem(Model):
    applied_promotions: list[Any] = Field(default_factory=list)
    compatibility_properties: Any | None = None
    delivery_cost: DeliveryCost | None = None
    discounted_line_item_cost: Any | None = None
    ebay_collect_and_remit_taxes: list[EbayCollectAndRemitTax] | None = None
    ebay_collected_charges: Any | None = None
    gift_details: Any | None = None
    item_location: ItemLocation | None = None
    legacy_item_id: str | None = None
    legacy_variation_id: Any | None = None
    line_item_cost: Amount | None = None
    line_item_fulfillment_instructions: LineItemFulfillmentInstructions | None = None
    line_item_fulfillment_status: str | None = None
    line_item_id: str | None = None
    linked_order_line_items: Any | None = None
    listing_marketplace_id: str | None = None
    properties: LineItemProperties | None = None
    purchase_marketplace_id: str | None = None
    quantity: int | None = None
    refunds: Any | None = None
    sku: str | None = None
    sold_format: str | None = None
    taxes: list[Any] = Field(default_factory=list)
    title: str | None = None
    total: Amount | None = None
    variation_aspects: list[VariationAspect] | None = None
    legacy_variation_id: str | None = None


# -------- Payments --------
class Payment(Model):
    amount: Amount | None = None
    payment_date: str | None = None
    payment_holds: Any | None = None
    payment_method: str | None = None
    payment_reference_id: str | None = None
    payment_status: str | None = None


class PaymentSummary(Model):
    payments: list[Payment] = Field(default_factory=list)
    refunds: list[Any] = Field(default_factory=list)
    total_due_seller: Amount | None = None


# -------- Order pricing summary --------
class PricingSummary(Model):
    adjustment: Any | None = None
    delivery_cost: Amount | None = None
    delivery_discount: Any | None = None
    fee: Any | None = None
    price_discount: Any | None = None
    price_subtotal: Amount | None = None
    tax: Any | None = None
    total: Amount | None = None


# -------- Order --------
class Order(Model):
    order_id: str

    buyer: Buyer | None = None
    buyer_checkout_notes: Any | None = None
    cancel_status: CancelStatus | None = None

    creation_date: str | None = None
    last_modified_date: str | None = None

    ebay_collect_and_remit_tax: bool | None = None
    fulfillment_hrefs: list[Any] = Field(default_factory=list)
    fulfillment_start_instructions: list[FulfillmentStartInstruction] = Field(default_factory=list)

    line_items: list[LineItem] = Field(default_factory=list)

    order_fulfillment_status: str | None = None
    order_payment_status: str | None = None

    payment_summary: PaymentSummary | None = None
    pricing_summary: PricingSummary | None = None

    program: Any | None = None
    sales_record_reference: str | None = None
    seller_id: str | None = None

    total_fee_basis_amount: Amount | None = None
    total_marketplace_fee: Amount | None = None

    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Order:
        # Support camelCase variants if they ever show up (optional)
        if "orderId" in data and "order_id" not in data:
            data = {**data, "order_id": data.get("orderId")}
        return cls.model_validate({**data, "raw": data})

    # ---- Convenience helpers (optional) ----
    @property
    def total_paid(self) -> Decimal | None:
        """
        Common “what did the buyer pay?” convenience.
        Uses pricing_summary.total if present.
        """
        if not self.pricing_summary or not self.pricing_summary.total:
            return None
        return self.pricing_summary.total.decimal

    @property
    def ship_to(self) -> ShipTo | None:
        """
        Convenience: primary ship_to from first fulfillment instruction (SHIP_TO).
        """
        if not self.fulfillment_start_instructions:
            return None
        instr = self.fulfillment_start_instructions[0]
        return instr.shipping_step.ship_to if instr and instr.shipping_step else None

    @property
    def flattened_line_items(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        buyer = self.buyer
        buyer_addr = buyer.buyer_registration_address if buyer else None
        buyer_contact = buyer_addr.contact_address if buyer_addr else None

        for li in self.line_items:
            # ---- Variation helpers ----
            variation_list = li.variation_aspects or []
            variation_dict = {
                (va.name or "").strip(): (va.value or "").strip()
                for va in variation_list
                if va and (va.name or "").strip()
            }
            # Useful for BQ/pandas if you want a single column
            variation_json = (
                json.dumps(variation_dict, ensure_ascii=False) if variation_dict else None
            )

            # ---- Taxes can be missing ----
            taxes_list = li.ebay_collect_and_remit_taxes or []
            tax_total = sum(
                (
                    t.amount.decimal
                    for t in taxes_list
                    if t and t.amount and t.amount.decimal is not None
                ),
                Decimal("0"),
            )

            row: dict[str, Any] = {
                # ---- Order-level ----
                "order_id": self.order_id,
                "seller_id": self.seller_id,
                "sales_record_reference": self.sales_record_reference,
                "order_creation_date": self.creation_date,
                "order_last_modified_date": self.last_modified_date,
                "order_payment_status": self.order_payment_status,
                "order_fulfillment_status": self.order_fulfillment_status,
                "ebay_collect_and_remit_tax": self.ebay_collect_and_remit_tax,
                # ---- Buyer ----
                "buyer_username": buyer.username if buyer else None,
                "buyer_full_name": buyer_addr.full_name if buyer_addr else None,
                "buyer_email": buyer_addr.email if buyer_addr else None,
                "buyer_city": buyer_contact.city if buyer_contact else None,
                "buyer_state": (buyer_contact.state_or_province if buyer_contact else None),
                "buyer_postal_code": (buyer_contact.postal_code if buyer_contact else None),
                "buyer_country": buyer_contact.country_code if buyer_contact else None,
                # ---- Line item ----
                "line_item_id": li.line_item_id,
                "sku": li.sku,
                "legacy_item_id": li.legacy_item_id,
                "legacy_variation_id": li.legacy_variation_id,
                "title": li.title,
                "quantity": li.quantity,
                "sold_format": li.sold_format,
                "listing_marketplace_id": li.listing_marketplace_id,
                "purchase_marketplace_id": li.purchase_marketplace_id,
                "line_item_fulfillment_status": li.line_item_fulfillment_status,
                # ---- Variation details ----
                # Most useful representation for joins/analytics:
                "variation_aspects_json": variation_json,
                # Optional
                "variation_aspects_insert": variation_dict.get("Insert"),
                # Optional: pull a common key if your variations use "Card"
                "variation_card": variation_dict.get("Card") or variation_dict.get("Card #"),
                # If you want the full dict available (fine for python, not BQ):
                "variation_aspects": variation_dict or None,
                # ---- Pricing ----
                "item_price": li.line_item_cost.decimal if li.line_item_cost else None,
                "item_currency": (li.line_item_cost.currency if li.line_item_cost else None),
                "shipping_price": (
                    li.delivery_cost.shipping_cost.decimal
                    if li.delivery_cost and li.delivery_cost.shipping_cost
                    else None
                ),
                "tax_total": tax_total if taxes_list else None,
                "line_item_total": li.total.decimal if li.total else None,
                # ---- Delivery ----
                "ship_by_date": (
                    li.line_item_fulfillment_instructions.ship_by_date
                    if li.line_item_fulfillment_instructions
                    else None
                ),
                "min_estimated_delivery_date": (
                    li.line_item_fulfillment_instructions.min_estimated_delivery_date
                    if li.line_item_fulfillment_instructions
                    else None
                ),
                "max_estimated_delivery_date": (
                    li.line_item_fulfillment_instructions.max_estimated_delivery_date
                    if li.line_item_fulfillment_instructions
                    else None
                ),
                # ---- Flags ----
                "sold_via_ad_campaign": (
                    li.properties.sold_via_ad_campaign if li.properties else None
                ),
                "buyer_protection": (li.properties.buyer_protection if li.properties else None),
            }

            rows.append(row)

        return rows

    @property
    def flattened_df(self) -> pd.DataFrame:
        return pd.json_normalize(self.flattened_line_items)
