import hashlib
from dataclasses import dataclass
from typing import Any

from shoebox.settings import get_settings
from shoebox.transforms.listing_builder import (
    autographed,
    base_inventory_item_payload,
    fit_inventory_description,
    insert_set,
    league,
    relic,
    rookie,
    sport,
    store_footer_html,
)


@dataclass
class ChecklistVariationRow:
    record_id: str
    set_year: str
    subset_id: str
    set_name: str
    subset_name: str
    subset_type: str
    derived_card_number: str | None
    card_number: str
    qty: int
    price: float
    player: str
    team: str | None
    display_name: str
    note: str | None


def build_variation_sku(subset_id: str, card_number: str) -> str:
    """Stable 50-char SKU for one variation item."""
    base = f"{subset_id}|{card_number}"
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest().upper()
    return digest[:50].ljust(50, "0")


def build_group_key(subset_id: str) -> str:
    """Stable inventory item group key (<=50 chars) from subset_id."""
    digest = hashlib.sha1(subset_id.encode("utf-8")).hexdigest().upper()
    return f"GRP-{digest}"[:50]


def _variation_features(rows: list[ChecklistVariationRow]) -> list[str]:
    r0 = rows[0]
    features = []
    if insert_set(r0.subset_name, r0.subset_type):
        features.append("Insert")
    else:
        features.append("[Base]")
    if any(relic(r.subset_name, None) for r in rows):
        features.append("Memorabilia")
    if any(rookie(r.note, None, r.subset_name) for r in rows):
        features.append("Rookie")
    return features


def build_variation_aspects(rows: list[ChecklistVariationRow]) -> dict[str, list[str]]:
    """
    Item specifics for the item group.
    Multi-value fields (Player, Team) collect all unique values across variations.
    Card Number is omitted — it is the varying attribute handled by variesBy.
    """
    r0 = rows[0]
    aspects: dict[str, Any] = {}
    aspects["Sport"] = [sport(r0.set_name)]
    aspects["Player/Athlete"] = sorted(set(r.player for r in rows if r.player))[:30]
    aspects["Season"] = [r0.set_year]
    aspects["Year Manufactured"] = [r0.set_year if len(r0.set_year) == 4 else r0.set_year[:4]]
    aspects["Features"] = _variation_features(rows)
    aspects["Set"] = [r0.set_name]
    unique_teams = sorted(set(r.team for r in rows if r.team))[:30]
    if unique_teams:
        aspects["Team"] = unique_teams
    aspects["League"] = [league(r0.set_name)]
    aspects["Autographed"] = ["Yes" if autographed(r0.subset_name) else "No"]
    aspects["Type"] = ["Sports Trading Card"]
    aspects["Card Size"] = ["Standard"]
    aspects["Card Thickness"] = ["35 Pt."]
    aspects["Country/Region of Manufacture"] = ["United States"]
    aspects["Graded"] = ["No"]
    aspects["Vintage"] = ["No"]
    aspects["Language"] = ["English"]
    aspects["Original/Licensed Reprint"] = ["Original"]
    if "Topps" in r0.set_name or "Bowman" in r0.set_name:
        aspects["Manufacturer"] = ["Topps"]
    insert = insert_set(r0.subset_name, r0.subset_type)
    if insert:
        aspects["Insert Set"] = [insert]
    return aspects


def build_variation_title(rows: list[ChecklistVariationRow]) -> str:
    r0 = rows[0]
    insert = insert_set(r0.subset_name, r0.subset_type)

    def _sort_key(r: ChecklistVariationRow):
        cn = r.derived_card_number or r.card_number
        try:
            return (0, int(cn))
        except (ValueError, TypeError):
            return (1, str(cn))

    sorted_rows = sorted(rows, key=_sort_key)
    min_card = sorted_rows[0].card_number
    max_card = sorted_rows[-1].card_number

    parts = [r0.set_name]
    if insert:
        parts.append(insert)
    parts.append(f"#{min_card}-{max_card} You Pick, Complete Your Set!")

    title = " ".join(parts)
    if len(title) > 80:
        title = title[:77] + "..."
    return title


def build_variation_description(rows: list[ChecklistVariationRow]) -> str:
    r0 = rows[0]
    insert = insert_set(r0.subset_name, r0.subset_type)
    html = f"<div>Set: {r0.set_name}</div>"
    if insert:
        html += f"<div>Insert Set: {insert}</div>"
    html += "<div>Select a card from the dropdown above.</div>"
    html += store_footer_html()
    # The group description becomes the listing description for a
    # multi-variation listing, and eBay caps it at 4000 characters.
    return fit_inventory_description(html)


def build_individual_inventory_item(
    row: ChecklistVariationRow,
    image_urls: list[str] | None = None,
) -> dict[str, Any]:
    """
    Individual inventory item payload for one variation.
    product.aspects contains only the varying attribute (Card) so eBay can
    link this SKU to its variation option in the item group.
    If image_urls are provided they are set on product.imageUrls so eBay
    can display the card-specific image when that variation is selected.
    """
    product: dict[str, Any] = {
        "aspects": {
            "Card": [row.display_name],
        }
    }
    if image_urls:
        product["imageUrls"] = image_urls

    return base_inventory_item_payload(quantity=int(row.qty), product=product)


def build_item_group_payload(
    rows: list[ChecklistVariationRow],
    skus: list[str],
    image_urls: list[str],
    has_per_variation_images: bool = False,
) -> dict[str, Any]:
    """
    Inventory item group payload (parent record grouping all variations).
    When has_per_variation_images=True, aspectsImageVariesBy is set so eBay
    displays each card's own image when that variation is selected.
    """
    varies_by: dict[str, Any] = {
        "specifications": [
            {
                "name": "Card",
                "values": [r.display_name for r in rows],
            }
        ]
    }
    if has_per_variation_images:
        varies_by["aspectsImageVariesBy"] = ["Card"]

    return {
        "title": build_variation_title(rows),
        "description": build_variation_description(rows),
        "aspects": build_variation_aspects(rows),
        "imageUrls": image_urls,
        "variantSKUs": skus,
        "variesBy": varies_by,
    }


def variation_store_category(row: ChecklistVariationRow) -> str:
    if autographed(row.subset_name):
        return "Autographs"
    if relic(row.subset_name, None):
        return "Relics"
    if insert_set(row.subset_name, row.subset_type):
        return "Inserts"
    return "Base Cards"


def build_sku_offer_payload(
    sku: str,
    row: ChecklistVariationRow,
    store_category: str,
    category_id: str | None = None,
    listing_start_date: str | None = None,
) -> dict[str, Any]:
    """
    Individual offer payload for one variation SKU.
    Each variation gets its own offer with its own price, so per-card pricing
    is fully supported. All offers in the group are published together via
    publishByInventoryItemGroup.
    """
    store = get_settings().store
    body: dict[str, Any] = {
        "sku": sku,
        "marketplaceId": "EBAY_US",
        "format": "FIXED_PRICE",
        "availableQuantity": int(row.qty),
        "categoryId": category_id or store.category_id,
        "listingDuration": "GTC",
        "merchantLocationKey": store.merchant_location_key,
        "listingPolicies": {
            # Variation ("You Pick") listings use the variation fulfillment
            # policy (e.g. free shipping) rather than the price-tiered one.
            "fulfillmentPolicyId": store.policies.fulfillment_policy_id_variation,
            "paymentPolicyId": store.policies.payment_policy_id,
            "returnPolicyId": store.policies.return_policy_id,
        },
        "storeCategoryNames": [store_category],
        "pricingSummary": {"price": {"value": float(row.price), "currency": "USD"}},
    }
    if listing_start_date:
        body["listingStartDate"] = listing_start_date
    return body
