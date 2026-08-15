import ast
from datetime import UTC, datetime, timedelta
from typing import Any

from shoebox.models.ebay_listing import EbayListingDraft
from shoebox.models.listing_queue import ListingQueueRow
from shoebox.settings import get_settings
from shoebox.utils.shorten_team import shorten_team_name

## Condition mapping
_CONDITION_DESCRIPTORS = {
    "NEAR_MINT_OR_BETTER": "400010",
    "EXCELLENT": "400011",
    "VERY_GOOD": "400012",
    "POOR": "400013",
}

# Store description footer appended to every listing. `{store_name}` is filled
# from settings.store.name at build time (see store_footer_html).
_STORE_FOOTER_TEMPLATE = """
<div style="font-family: Arial, Helvetica, sans-serif; font-size: 15px; line-height: 1.55; color: #1a1a1a; max-width: 760px;">
 
  <p style="margin: 0 0 16px;">
    Thanks for visiting <strong>{{store name}}</strong> &mdash; 12,000+ cards shipped, 100% positive feedback.
    Every card is scanned front and back, so what you see is exactly what ships.
  </p>
 
  <h3 style="margin: 22px 0 8px; font-size: 16px;">Flat shipping &mdash; buy as many cards as you want</h3>
  <ul style="margin: 0 0 16px; padding-left: 20px;">
    <li><strong>Shipping starts at $0.78 and barely moves.</strong> Add more cards for just $0.30 each &mdash; your cart shows the real total up front, with nothing to refund after the fact. <em>Add everything to your cart and check out once</em> so the combined rate applies.</li>
    <li><strong>Free shipping on orders over $20.</strong> Higher-value cards ship free, fully tracked and insured.</li>
    <li><strong>Orders ship same day</strong> when placed by 5:00 PM Pacific, Monday through Saturday.</li>
    <li><strong>International buyers welcome</strong> via eBay International Shipping &mdash; duties and delivery handled by eBay, tracked end to end.</li>
  </ul>
 
  <h3 style="margin: 22px 0 8px; font-size: 16px;">How your cards are packed</h3>
  <ul style="margin: 0 0 16px; padding-left: 20px;">
    <li><strong>Orders under $20:</strong> penny sleeve &rarr; card saver &rarr; team bag &rarr; protective envelope, shipped via eBay Standard Envelope with tracking.</li>
    <li><strong>Orders over $20 (ships free):</strong> penny sleeve &rarr; top loader &rarr; team bag &rarr; secured between two ding defenders &rarr; bubble mailer, shipped USPS Ground Advantage with full tracking and insurance.</li>
  </ul>
  <p style="margin: 0 0 16px; font-size: 14px; color: #444;">
    A note on envelope tracking: scans sometimes update a day or two behind the actual delivery. This is normal for
    letter mail and your card is on its way. If anything looks off, message me and I'll sort it out.
  </p>
 
  <h3 style="margin: 22px 0 8px; font-size: 16px;">Condition &amp; returns</h3>
  <ul style="margin: 0 0 16px; padding-left: 20px;">
    <li><strong>30-day returns, free.</strong> If a card isn't what you expected, tell me and I'll make it right &mdash; no hassle, no restocking fee.</li>
    <li>Cards are in the condition shown in the scans. Any notable flaw is called out in the listing; if I haven't mentioned one, the scan is the full story.</li>
    <li><strong>Want a closer look before you buy?</strong> Message me and I'll send additional images or closeups of any corner, edge, or surface. Happy to do it.</li>
  </ul>
 
  <h3 style="margin: 22px 0 8px; font-size: 16px;">Building a set? Buying in bulk?</h3>
  <ul style="margin: 0 0 16px; padding-left: 20px;">
    <li>I run <strong>Complete Your Set</strong> listings for most modern Topps and Bowman releases &mdash; pick exactly the numbers you still need, with volume discounts as your order grows.</li>
    <li><strong>Bulk deals:</strong> message me. I've put together plenty of 50+ card deals and I'll land on a price that works for both of us.</li>
    <li>Looking for a specific player, team, or parallel that isn't listed? Ask &mdash; there's a good chance it's in the box and not yet scanned.</li>
  </ul>
 
  <div style="margin: 24px 0 8px; padding: 14px 16px; background: #f5f5f5; border-left: 3px solid #333;">
    <strong>Follow {{store name}}</strong> to get first look at new listings &mdash; I add cards several times a week,
    and set-completion inventory moves quickly. Hit "Save Seller" at the top of this page.
  </div>
 
  <p style="margin: 16px 0 0; font-size: 14px; color: #444;">
    Questions, offers, or you just want to talk ball &mdash; message me anytime. I usually reply within a few hours.
  </p>
 
</div>
"""


def store_footer_html(store_name: str | None = None) -> str:
    """Render the shared listing footer with the configured store name."""
    if store_name is None:
        store_name = get_settings().store.name
    return _STORE_FOOTER_TEMPLATE.format(store_name=store_name)


def build_description(
    set_name: str,
    card_number: str,
    features: list,
    insert: str = None,
    parallel: str = None,
    player: str = None,
    team: str = None,
) -> str:
    specifics = f"<div>Set: {set_name}</div>"
    if insert:
        specifics += f"<div>Insert: {insert}</div>"
    if parallel:
        specifics += f"<div>Parallel: {parallel}</div>"
    specifics += f"<div>Card #: {card_number}</div>"
    specifics += f"<div>Player: {player}</div>"
    if team:
        specifics += f"<div>Team: {team}</div>"
    if features:
        specifics += f"<div>Features: {', '.join(features)}"
    specifics += store_footer_html()
    return specifics


def multi_str_split(s: str):
    return s.split(" / ")


def insert_set(subset_name: str, subset_type: str) -> str:
    if subset_type == "Insert":
        return subset_name
    else:
        return None


def autographed(subset_name: str) -> bool:
    return True if "auto" in subset_name.lower() else False


def rookie(note: str, note_check: str, subset_name: str) -> bool:
    if note and "RC" in note:
        return True
    elif note_check and "RC" in note_check:
        return True
    elif "Rookie" in subset_name:
        return True
    else:
        return False


def short_print(note: str, parallel_note: str) -> bool:
    if note and "SP" in note:
        return True
    elif parallel_note and "SP" in parallel_note:
        return True
    else:
        return False


def relic(subset_name: str, parallel_note: str) -> bool:
    if "relic" in subset_name.lower():
        return True
    elif parallel_note and "MEM" in parallel_note:
        return True
    else:
        return False


def sport(set_name: str) -> str:
    for sport in ["Football", "Basketball"]:
        if sport in set_name:
            return sport
    return "Baseball"


def league(set_name: str) -> str:
    s = sport(set_name)
    if s == "Football":
        return "National Football League (NFL)"
    elif s == "Basketball":
        return "National Basketball Association (NBA)"
    else:
        return "Major League (MLB)"


def features(r: ListingQueueRow) -> list:
    f = []
    if insert_set(r.subset_name, r.subset_type):
        f.append("Insert")
    else:
        f.append("[Base]")

    if relic(r.subset_name, parallel_note=r.parallel_note):
        f.append("Memorabilia")

    if r.parallel_variety:
        f.append("Parallel/Variety")

    if rookie(r.note, r.note_check, r.subset_name):
        f.append("Rookie")

    if r.print_run:
        f.append("Serial Numbered")

    if short_print(r.note, r.parallel_note):
        f.append("Short Print")

    return f


def replace_title_elements(title: str, ctr: int, card_number) -> str:
    if ctr == 1:
        return title.replace(" -", "")
    elif ctr == 2:
        return title.replace(f" #{card_number}", "")
    elif ctr == 3:
        return (
            title.replace(" Baseball", "")
            .replace(" Basketball", "")
            .replace(" Football", "")
        )
    elif ctr == 4:
        return title.replace(" Refractor", "")
    elif ctr == 5:
        return title.replace(" Chrome", "")


def title(r: ListingQueueRow) -> str:
    suffix_list = []

    if autographed(r.subset_name):
        suffix_list.append("AU")
    if relic(r.subset_name, r.parallel_note):
        suffix_list.append("MEM")
    if rookie(r.note, r.note_check, r.subset_name):
        suffix_list.append("RC")
    if short_print(r.note, r.parallel_note):
        suffix_list.append("SP")

    if len(suffix_list) > 0:
        suffix = f"({','.join(suffix_list)})"
    else:
        suffix = None

    player = multi_str_split(r.player)
    if len(player) > 1:
        player = " ".join([y.split(" ")[-1] for y in player])
    else:
        player = player[0]

    title = " ".join(
        filter(
            None,
            [
                r.set_name,
                "-",
                insert_set(r.subset_name, r.subset_type),
                player,
                f"#{r.card_number}",
                r.parallel_variety,
                (shorten_team_name(r.team) if shorten_team_name(r.team) else None),
                suffix,
                (f"/{r.print_run}" if r.print_run else None),
            ],
        )
    )

    if len(title) > 80:
        ctr = 1
        while True:
            title = replace_title_elements(title, ctr, r.card_number)
            if len(title) <= 80:
                return title
            elif ctr == 5:
                return title[:77] + "..."
            else:
                ctr += 1
    return title


def store_category(r: ListingQueueRow) -> str:
    if autographed(r.subset_name):
        return "Autographs"
    elif relic(r.subset_name, r.parallel_note):
        return "Relics"
    elif r.print_run:
        return "Serial Numbered"
    elif r.parallel_variety:
        return "Parallels"
    elif insert_set(r.subset_name, r.subset_type):
        return "Inserts"
    else:
        return "Base Cards"


def build_aspects(row: ListingQueueRow) -> dict[str, Any]:
    aspects: dict[str, Any] = {}
    aspects["Sport"] = sport(row.set_name)
    aspects["Player/Athlete"] = multi_str_split(row.player)
    aspects["Season"] = row.set_year
    aspects["Year Manufactured"] = (
        row.set_year if len(row.set_year) == 4 else row.set_year[:4]
    )
    aspects["Features"] = features(row)
    aspects["Set"] = row.set_name
    if row.team:
        aspects["Team"] = multi_str_split(row.team)
    aspects["League"] = league(row.set_name)
    aspects["Autographed"] = "Yes" if autographed(row.subset_name) else "No"
    aspects["Card Number"] = row.card_number
    aspects["Type"] = "Sports Trading Card"
    aspects["Card Size"] = "Standard"
    aspects["Card Thickness"] = (
        "100 Pt." if "Memorabilia" in aspects["Features"] else "35 Pt."
    )
    aspects["Country/Region of Manufacture"] = "United States"
    aspects["Graded"] = "No"
    aspects["Vintage"] = "No"
    aspects["Language"] = "English"
    aspects["Original/Licensed Reprint"] = "Original"
    if row.parallel_variety:
        aspects["Parallel/Variety"] = row.parallel_variety
    if row.print_run:
        aspects["Print Run"] = str(row.print_run).replace(".0", "")
    if "Topps" in row.set_name or "Bowman" in row.set_name:
        aspects["Manufacturer"] = "Topps"
    if insert_set(row.subset_name, row.subset_type):
        aspects["Insert Set"] = insert_set(row.subset_name, row.subset_type)

    for k, v in aspects.items():
        if type(v) in [str, int, float]:
            aspects[k] = [v]
    return aspects


def base_inventory_item_payload(
    *, quantity: int, product: dict[str, Any]
) -> dict[str, Any]:
    """Shared inventory-item skeleton for every card listing.

    Condition descriptor 40001/400010 is eBay's trading-card grade
    "Near Mint or Better"; the package is a one-ounce plain-white-envelope
    style LETTER (7x5x1 in).
    """
    descriptor = get_settings().store.condition_descriptor
    if descriptor not in _CONDITION_DESCRIPTORS:
        # Settings doesn't validate this value, so a typo in app.yaml would
        # otherwise surface as a bare KeyError mid-listing-build.
        raise ValueError(
            f"store.condition_descriptor {descriptor!r} in app.yaml is not one of "
            f"{sorted(_CONDITION_DESCRIPTORS)}"
        )
    return {
        "condition": get_settings().store.condition,
        "conditionDescriptors": [
            {
                "name": "40001",
                "values": [_CONDITION_DESCRIPTORS[descriptor]],
            }
        ],
        "packageWeightAndSize": {
            "dimensions": {"height": 1, "length": 7, "unit": "INCH", "width": 5},
            "packageType": "LETTER",
            "shippingIrregular": False,
            "weight": {"unit": "OUNCE", "value": 1},
        },
        "product": product,
        "availability": {"shipToLocationAvailability": {"quantity": int(quantity)}},
    }


def build_inventory_item_payload(*, draft: EbayListingDraft) -> dict[str, Any]:
    return base_inventory_item_payload(
        quantity=int(draft.quantity),
        product={
            "title": draft.title,
            "description": draft.description,
            "aspects": draft.aspects,
            "imageUrls": draft.image_urls,
        },
    )


def build_offer_payload(*, draft: EbayListingDraft) -> dict[str, Any]:
    store = get_settings().store
    body: dict[str, Any] = {
        "sku": draft.sku,
        "marketplaceId": "EBAY_US",
        "format": "FIXED_PRICE",
        "availableQuantity": int(draft.quantity),
        "categoryId": draft.category_id,
        "listingDescription": draft.description,
        "listingDuration": "GTC",
        "merchantLocationKey": store.merchant_location_key,
        "listingPolicies": {
            "fulfillmentPolicyId": (
                store.policies.fulfillment_policy_id_low
                if float(draft.price) <= store.fulfillment_low_max_price
                else store.policies.fulfillment_policy_id_high
            ),
            "paymentPolicyId": store.policies.payment_policy_id,
            "returnPolicyId": store.policies.return_policy_id,
            "bestOfferTerms": {"bestOfferEnabled": True},
        },
        "storeCategoryNames": [draft.store_category],
        "pricingSummary": {"price": {"value": float(draft.price), "currency": "USD"}},
    }
    if draft.listing_start_date:
        body["listingStartDate"] = draft.listing_start_date
    return body


def rebuild_inventory_item_body(existing_item: dict, image_urls: list) -> dict:
    """Rebuild an inventory item payload from an existing eBay API item response."""
    return base_inventory_item_payload(
        quantity=existing_item["availability"]["ship_to_location_availability"][
            "quantity"
        ],
        product={
            "title": existing_item["product"]["title"],
            "description": store_footer_html(),
            "aspects": ast.literal_eval(existing_item["product"]["aspects"]),
            "imageUrls": image_urls,
        },
    )


def rebuild_offer_body(
    existing_offer: dict, new_price: float, schedule_datetime: str = None
) -> dict:
    """Rebuild an offer payload from an existing eBay API offer response with a new price."""
    store = get_settings().store
    d = {
        "sku": existing_offer["sku"],
        "marketplaceId": "EBAY_US",
        "format": "FIXED_PRICE",
        "availableQuantity": existing_offer["available_quantity"],
        "categoryId": existing_offer["category_id"],
        "listingDescription": existing_offer["listing_description"],
        "listingDuration": "GTC",
        "merchantLocationKey": store.merchant_location_key,
        "listingPolicies": {
            "fulfillmentPolicyId": (
                store.policies.fulfillment_policy_id_low
                if new_price <= store.fulfillment_low_max_price
                else store.policies.fulfillment_policy_id_high
            ),
            "paymentPolicyId": store.policies.payment_policy_id,
            "returnPolicyId": store.policies.return_policy_id,
            "bestOfferTerms": {"bestOfferEnabled": True},
        },
        "storeCategoryNames": [
            x for x in existing_offer["store_category_names"] if "None" not in x
        ],
        "pricingSummary": {"price": {"value": new_price, "currency": "USD"}},
    }
    if schedule_datetime:
        d["listingStartDate"] = schedule_datetime
    return d


def build_draft(
    *, row: ListingQueueRow, image_urls: list[str], sku: str, schedule: bool = False
) -> EbayListingDraft:
    draft_title = title(row)
    draft_desc = build_description(
        row.set_name,
        row.card_number,
        features(row),
        insert=insert_set(row.subset_name, row.subset_type),
        parallel=row.parallel_variety,
        player=row.player,
        team=row.team,
    )
    aspects = build_aspects(row)

    if schedule:
        listing_start_date = (datetime.now(UTC) + timedelta(days=19)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    else:
        listing_start_date = None

    draft = EbayListingDraft(
        sku=sku,
        title=draft_title,
        description=draft_desc,
        quantity=row.quantity,
        price=float(row.price),
        category_id=get_settings().store.category_id,
        store_category=store_category(row),
        listing_start_date=listing_start_date,
        condition_id="",
        image_urls=image_urls,
        aspects=aspects,
    )

    draft.inventory_item = build_inventory_item_payload(draft=draft)
    draft.offer = build_offer_payload(draft=draft)
    return draft
