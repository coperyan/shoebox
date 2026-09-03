"""Scratch harness: rebuild promoted-listings campaigns around listing age and price.

Replaces the old per-set campaign layout ("2025 Topps Chrome", "2026 Bowman", ...)
with four campaigns keyed on how long a card has been sitting and what it costs:

    General - New       posted <= 30 days, under $20     2.5%
    General - Aging     posted 31-90 days, under $20     5.0%
    General - Stale     posted 91+ days,   under $20     8.0%
    Offsite - High Value  $20 and up, any age            8.0%

All four are onsite Cost-Per-Sale campaigns with a fixed ad rate. "Offsite - High
Value" keeps the name for continuity but is *not* an offsite CPC campaign -- those
are funded by a daily budget rather than an ad rate and need ad groups per listing,
which is a different shape than the other three.

Only sports-card singles are eligible: fixed-price, no variations, eBay leaf
category 261328. A listing already carrying an ad in a live campaign is skipped
rather than moved, since eBay allows a listing in only one CPS campaign at a time.

Kept out of ``shoebox/pipelines/`` on purpose -- this is a one-off reorganization,
not a recurring flow, and it only touches eBay when run directly with --apply.

    python scripts/reorganize_ad_campaigns.py                 # dry run + CSV plan
    python scripts/reorganize_ad_campaigns.py --apply         # create campaigns + ads
    python scripts/reorganize_ad_campaigns.py --limit 20 --apply

The dry run writes the same CSV the apply run does, so the assignment can be
reviewed in full before anything is created.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from shoebox.clients.ebay.client import EbayClient
from shoebox.clients.ebay.trading import EBAY_NS, _dig, _ensure_list
from shoebox.settings import get_settings
from shoebox.utils.logging_setup import setup_logging

logger = logging.getLogger(__name__)

# eBay leaf category for Sports Trading Cards -> Singles. Everything else in the
# store (e.g. 183454, CCG singles) is out of scope for these campaigns.
SPORTS_SINGLES_CATEGORY = "261328"

# Trading listing types that are Buy It Now. Auctions ("Chinese") are excluded.
BUY_IT_NOW_TYPES = {"FixedPriceItem", "StoresFixedPrice"}

HIGH_VALUE_MIN_PRICE = 20.0

MARKETPLACE_ID = "EBAY_US"

# createAd tops out at 500 listings per bulk call.
AD_CHUNK_SIZE = 500

# GetMyeBaySelling does not return PrimaryCategory, but the natural-search URL it
# does return carries the leaf category as a query param.
_CATEGORY_IN_URL = re.compile(r"[?&]category=(\d+)")

# StartTime comes back as 2026-07-16T20:40:02.000Z.
_START_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


@dataclass(frozen=True)
class CampaignSpec:
    """One target campaign and the rule that decides what lands in it."""

    name: str
    bid_percentage: str
    # Inclusive age bounds in days; None means unbounded on that side.
    min_age_days: float | None = None
    max_age_days: float | None = None
    # True  -> price >= HIGH_VALUE_MIN_PRICE
    # False -> price <  HIGH_VALUE_MIN_PRICE
    high_value: bool = False

    def matches(self, *, price: float, age_days: float) -> bool:
        if self.high_value != (price >= HIGH_VALUE_MIN_PRICE):
            return False
        if self.min_age_days is not None and age_days < self.min_age_days:
            return False
        if self.max_age_days is not None and age_days > self.max_age_days:
            return False
        return True


# Order matters: the first spec a listing matches wins. High value is checked
# first so a $25 card 200 days old lands in High Value, not Stale.
CAMPAIGN_SPECS: tuple[CampaignSpec, ...] = (
    CampaignSpec(name="Offsite - High Value", bid_percentage="8.0", high_value=True),
    CampaignSpec(name="General - New", bid_percentage="2.5", max_age_days=30),
    CampaignSpec(name="General - Aging", bid_percentage="5.0", min_age_days=30, max_age_days=90),
    CampaignSpec(name="General - Stale", bid_percentage="8.0", min_age_days=90),
)


@dataclass
class Listing:
    item_id: str
    title: str
    sku: str | None
    price: float
    age_days: float
    start_time: str


@dataclass
class Plan:
    """What the run intends to do, before any of it happens."""

    by_campaign: dict[str, list[Listing]] = field(default_factory=dict)
    skipped_already_promoted: list[Listing] = field(default_factory=list)
    skipped_ineligible: int = 0


# ----------------------------------------------------------------------
# Reading listings
# ----------------------------------------------------------------------


def _price_of(item: dict[str, Any]) -> float | None:
    node = _dig(item, ["SellingStatus", "CurrentPrice"], None)
    text = node.get("#text") if isinstance(node, dict) else node
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _category_of(item: dict[str, Any]) -> str | None:
    url = _dig(item, ["ListingDetails", "ViewItemURLForNaturalSearch"], "") or ""
    match = _CATEGORY_IN_URL.search(url)
    return match.group(1) if match else None


def _age_days(item: dict[str, Any], *, now: datetime) -> tuple[float, str] | None:
    start = _dig(item, ["ListingDetails", "StartTime"], None)
    if not start:
        return None
    try:
        started = datetime.strptime(start, _START_TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
    return (now - started).total_seconds() / 86400, start


def fetch_active_listings(client: EbayClient) -> list[dict[str, Any]]:
    """Page the seller's active listings, keeping the raw Trading item nodes.

    ``TradingClient.get_active_listings`` is not reusable here: it flattens
    each item down to a fixed set of fields and drops ListingType, Variations,
    and the natural-search URL -- exactly the three this script filters on.
    Widening that method would change the dict shape that ``sync_active_listings``
    loads into BigQuery against a fixed schema, so the raw nodes are read here
    instead and the shared client is left alone.
    """
    legacy = client.legacy_api
    items: list[dict[str, Any]] = []
    page = 1

    while True:
        body = f"""<?xml version="1.0" encoding="utf-8"?>
                <GetMyeBaySellingRequest xmlns="{EBAY_NS}">
                <RequesterCredentials>
                    <eBayAuthToken>{legacy.token}</eBayAuthToken>
                </RequesterCredentials>
                <ErrorLanguage>en_US</ErrorLanguage>
                <WarningLevel>High</WarningLevel>
                <ActiveList>
                    <Include>true</Include>
                    <Pagination>
                    <EntriesPerPage>200</EntriesPerPage>
                    <PageNumber>{page}</PageNumber>
                    </Pagination>
                    <Sort>TimeLeft</Sort>
                </ActiveList>
                </GetMyeBaySellingRequest>"""

        payload = legacy._trading_call(
            call_name="GetMyeBaySelling",
            body=body,
            site_id="0",
            compatibility_level="1259",
        )

        page_items = [
            item
            for item in _ensure_list(_dig(payload, ["ActiveList", "ItemArray", "Item"], None))
            if isinstance(item, dict)
        ]
        items.extend(page_items)

        total_pages_text = _dig(
            payload, ["ActiveList", "PaginationResult", "TotalNumberOfPages"], None
        )
        try:
            total_pages = int(total_pages_text) if total_pages_text else 1
        except (TypeError, ValueError):
            total_pages = 1

        logger.info(
            "Fetched active listings page %d/%d (%d items)", page, total_pages, len(page_items)
        )

        if not page_items or page >= total_pages:
            break
        page += 1

    return items


def promoted_listing_ids(client: EbayClient) -> set[str]:
    """Listing IDs that already carry an ad in a campaign that is not ended.

    eBay permits a listing in only one CPS campaign at a time, so anything in
    here has to be left where it is. Campaigns whose ads cannot be read (CPC
    campaigns answer getAds with a 35045) are logged and treated as empty --
    their ads live under ad groups and do not block a CPS ad anyway.
    """
    promoted: set[str] = set()

    for campaign in client.marketing.get_campaigns():
        if campaign.get("campaign_status") == "ENDED":
            continue
        campaign_id = campaign.get("campaign_id")
        try:
            ads = client.marketing.get_ads(campaign_id)
        except Exception as e:
            logger.warning(
                "Could not read ads for campaign %r (%s): %s",
                campaign.get("campaign_name"),
                campaign_id,
                str(e).splitlines()[0],
            )
            continue

        listing_ids = {str(ad.get("listing_id")) for ad in ads if ad.get("listing_id")}
        promoted |= listing_ids
        logger.info(
            "Campaign %r [%s] holds %d ads",
            campaign.get("campaign_name"),
            campaign.get("campaign_status"),
            len(listing_ids),
        )

    return promoted


# ----------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------


def build_plan(
    items: list[dict[str, Any]],
    *,
    already_promoted: set[str],
    now: datetime,
    limit: int | None = None,
) -> Plan:
    plan = Plan(by_campaign={spec.name: [] for spec in CAMPAIGN_SPECS})

    for item in items:
        item_id = str(item.get("ItemID") or "")
        if not item_id:
            plan.skipped_ineligible += 1
            continue

        if item.get("ListingType") not in BUY_IT_NOW_TYPES:
            plan.skipped_ineligible += 1
            continue
        if "Variations" in item:
            plan.skipped_ineligible += 1
            continue
        if _category_of(item) != SPORTS_SINGLES_CATEGORY:
            plan.skipped_ineligible += 1
            continue

        price = _price_of(item)
        aged = _age_days(item, now=now)
        if price is None or aged is None:
            logger.warning("Skipping item_id=%s: unparseable price or start time", item_id)
            plan.skipped_ineligible += 1
            continue
        age_days, start_time = aged

        listing = Listing(
            item_id=item_id,
            title=str(item.get("Title") or ""),
            sku=item.get("SKU"),
            price=price,
            age_days=age_days,
            start_time=start_time,
        )

        if item_id in already_promoted:
            plan.skipped_already_promoted.append(listing)
            continue

        for spec in CAMPAIGN_SPECS:
            if spec.matches(price=price, age_days=age_days):
                plan.by_campaign[spec.name].append(listing)
                break
        else:
            logger.warning(
                "item_id=%s matched no campaign (price=%.2f age=%.1fd)", item_id, price, age_days
            )
            plan.skipped_ineligible += 1

    if limit is not None:
        for name, listings in plan.by_campaign.items():
            plan.by_campaign[name] = listings[:limit]

    return plan


def write_plan_csv(plan: Plan, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rates = {spec.name: spec.bid_percentage for spec in CAMPAIGN_SPECS}

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "campaign",
                "bid_percentage",
                "item_id",
                "sku",
                "price",
                "age_days",
                "start_time",
                "title",
            ]
        )
        for spec in CAMPAIGN_SPECS:
            for listing in plan.by_campaign[spec.name]:
                writer.writerow(
                    [
                        spec.name,
                        rates[spec.name],
                        listing.item_id,
                        listing.sku or "",
                        f"{listing.price:.2f}",
                        f"{listing.age_days:.1f}",
                        listing.start_time,
                        listing.title,
                    ]
                )
        for listing in plan.skipped_already_promoted:
            writer.writerow(
                [
                    "SKIPPED_ALREADY_PROMOTED",
                    "",
                    listing.item_id,
                    listing.sku or "",
                    f"{listing.price:.2f}",
                    f"{listing.age_days:.1f}",
                    listing.start_time,
                    listing.title,
                ]
            )


# ----------------------------------------------------------------------
# Applying
# ----------------------------------------------------------------------


def ensure_campaign(client: EbayClient, spec: CampaignSpec, *, start: datetime) -> str:
    """Return the campaign ID for ``spec``, creating the campaign if needed.

    Matching is by exact name against campaigns that are not ended, so a rerun
    reuses what the previous run created instead of making a duplicate.
    """
    for campaign in client.marketing.get_campaigns():
        if campaign.get("campaign_status") == "ENDED":
            continue
        if campaign.get("campaign_name") == spec.name:
            campaign_id = str(campaign.get("campaign_id"))
            logger.info("Reusing existing campaign %r (%s)", spec.name, campaign_id)
            return campaign_id

    body = {
        "campaignName": spec.name,
        "marketplaceId": MARKETPLACE_ID,
        "startDate": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "fundingStrategy": {
            "fundingModel": "COST_PER_SALE",
            "adRateStrategy": "FIXED",
            "bidPercentage": spec.bid_percentage,
        },
    }
    client.api.sell_marketing_create_campaign(body=body, content_type="application/json")
    logger.info("Created campaign %r at %s%%", spec.name, spec.bid_percentage)

    # createCampaign returns no body, so the ID comes from a lookup by name.
    campaign = client.api.sell_marketing_get_campaign_by_name(campaign_name=spec.name)
    campaign_id = str(campaign.get("campaign_id"))
    logger.info("Campaign %r has id %s", spec.name, campaign_id)
    return campaign_id


def create_ads(
    client: EbayClient, *, campaign_id: str, spec: CampaignSpec, listings: list[Listing]
) -> tuple[int, int]:
    """Create ads for ``listings`` in bulk. Returns (created, failed)."""
    created = 0
    failed = 0

    for start in range(0, len(listings), AD_CHUNK_SIZE):
        chunk = listings[start : start + AD_CHUNK_SIZE]
        body = {
            "requests": [
                {"listingId": listing.item_id, "bidPercentage": spec.bid_percentage}
                for listing in chunk
            ]
        }

        try:
            response = client.api.sell_marketing_bulk_create_ads_by_listing_id(
                body=body,
                content_type="application/json",
                campaign_id=campaign_id,
            )
        except Exception:
            logger.exception(
                "Bulk ad creation failed outright for %r (%d listings in this chunk)",
                spec.name,
                len(chunk),
            )
            failed += len(chunk)
            continue

        # Per-listing outcomes: a 2xx statusCode means the ad was created, and
        # anything else carries an errors array explaining why it was not.
        for entry in response.get("responses") or []:
            status = entry.get("status_code") or entry.get("statusCode") or 0
            if 200 <= int(status) < 300:
                created += 1
                continue
            failed += 1
            errors = entry.get("errors") or []
            message = errors[0].get("message") if errors else "unknown error"
            logger.warning(
                "Ad failed listing_id=%s status=%s: %s",
                entry.get("listing_id") or entry.get("listingId"),
                status,
                message,
            )

        logger.info(
            "%r: processed %d/%d listings",
            spec.name,
            min(start + AD_CHUNK_SIZE, len(listings)),
            len(listings),
        )

    return created, failed


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create the campaigns and ads. Without this the run only writes the CSV plan.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Cap the number of listings per campaign (for a small live test).",
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=[spec.name for spec in CAMPAIGN_SPECS],
        help="Restrict the run to one campaign; repeatable.",
    )
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    now = datetime.now(UTC)

    specs = [s for s in CAMPAIGN_SPECS if not args.only or s.name in args.only]

    client = EbayClient()

    logger.info("Reading active listings...")
    items = fetch_active_listings(client)
    logger.info("Read %d active listings", len(items))

    logger.info("Reading existing campaign ads...")
    already_promoted = promoted_listing_ids(client)
    logger.info("%d listings already promoted in a live campaign", len(already_promoted))

    plan = build_plan(items, already_promoted=already_promoted, now=now, limit=args.limit)

    csv_path = (
        Path(settings.paths.exports_dir)
        / "csv"
        / f"ad_campaign_plan_{now.strftime('%Y_%m_%d_%H_%M')}.csv"
    )
    write_plan_csv(plan, csv_path)

    logger.info("--- plan ---")
    for spec in CAMPAIGN_SPECS:
        marker = "" if spec in specs else "  (not selected)"
        logger.info(
            "  %-22s %5s%%  %5d listings%s",
            spec.name,
            spec.bid_percentage,
            len(plan.by_campaign[spec.name]),
            marker,
        )
    logger.info(
        "  %-22s %5s   %5d listings", "skipped (promoted)", "", len(plan.skipped_already_promoted)
    )
    logger.info("  %-22s %5s   %5d listings", "skipped (ineligible)", "", plan.skipped_ineligible)
    logger.info("Plan written to %s", csv_path)

    if not args.apply:
        logger.info("Dry run -- nothing created. Rerun with --apply to execute.")
        return

    start = now + timedelta(minutes=2)
    totals_created = 0
    totals_failed = 0

    for spec in specs:
        listings = plan.by_campaign[spec.name]
        if not listings:
            logger.info("%r has no listings; skipping", spec.name)
            continue

        campaign_id = ensure_campaign(client, spec, start=start)
        created, failed = create_ads(client, campaign_id=campaign_id, spec=spec, listings=listings)
        totals_created += created
        totals_failed += failed
        logger.info("%r: %d ads created, %d failed", spec.name, created, failed)

    logger.info("Done -- %d ads created, %d failed", totals_created, totals_failed)


if __name__ == "__main__":
    main()
