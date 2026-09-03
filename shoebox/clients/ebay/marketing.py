"""Sell Marketing API: promoted-listings ads, campaigns, and item promotions.

Every ``sell_marketing_*`` call in the codebase goes through this client so the
retry/error-code handling for promotions lives in one place.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .session import EbaySession

logger = logging.getLogger(__name__)

# eBay error IDs these calls treat specially.
AD_ALREADY_EXISTS_ERROR = 35036
LISTING_NOT_VISIBLE_ERROR = 38227

DEFAULT_MARKETPLACE_ID = "EBAY_US"
RUNNING_CAMPAIGN_STATUS = "RUNNING"


class MarketingClient:
    def __init__(self, session: EbaySession):
        self.session = session
        self.api = session.api
        self.settings = session.settings

    def _campaign_id(self, campaign_id: str | None) -> str:
        """Fall back to the configured default campaign when none is given."""
        return campaign_id or self.settings.ebay.campaign_id

    # ------------------------------------------------------------------
    # Campaigns and ads
    # ------------------------------------------------------------------

    def get_campaigns(self, *, status: str | None = None) -> list[dict[str, Any]]:
        """
        Return promoted-listings campaign records.

        Pass ``status`` (e.g. "RUNNING") to filter on ``campaign_status``.
        """
        resp = self.api.sell_marketing_get_campaigns()
        records = [x["record"] for x in resp if "record" in x]
        if status is None:
            return records
        return [c for c in records if c.get("campaign_status") == status]

    def get_ads(self, campaign_id: str) -> list[dict[str, Any]]:
        """Return the ad records belonging to a campaign."""
        resp = self.api.sell_marketing_get_ads(campaign_id=campaign_id)
        return [x["record"] for x in resp if "record" in x]

    def get_campaign_ads(
        self, *, status: str | None = RUNNING_CAMPAIGN_STATUS
    ) -> list[dict[str, Any]]:
        """
        Return every ad across campaigns, flattened with its campaign context.

        Each row carries campaign_id / campaign_name / campaign_status alongside
        the ad fields, which is what the relist pipeline joins on listing_id.
        Defaults to running campaigns only.
        """
        results: list[dict[str, Any]] = []
        for campaign in self.get_campaigns(status=status):
            campaign_fields = {
                "campaign_id": campaign.get("campaign_id"),
                "campaign_name": campaign.get("campaign_name"),
                "campaign_status": campaign.get("campaign_status"),
            }
            try:
                results.extend(
                    {**campaign_fields, **ad} for ad in self.get_ads(campaign.get("campaign_id"))
                )
            except Exception as e:
                logger.warning(
                    f"Getting ads for campaign ID {campaign.get('campaign_id')} failed\n"
                )
                logger.warning(e)
        return results

    def delete_ad(self, *, campaign_id: str, ad_id: str) -> Any:
        """
        Delete a single ad from a campaign.

        Raises on failure; callers that treat removal as best-effort (such as the
        relist flow) are responsible for swallowing the error.
        """
        return self.api.sell_marketing_delete_ad(campaign_id=campaign_id, ad_id=ad_id)

    # ------------------------------------------------------------------
    # Creating ads
    # ------------------------------------------------------------------

    def _create_ad_with_retry(
        self,
        fn: Callable[[], Any],
        *,
        label: str,
        max_tries: int = 2,
        sleep_s: int = 10,
    ) -> None:
        """
        Run an ad-creation call, tolerating the "ad already exists" case.

        eBay's 35036 means the listing is already promoted in the campaign, which
        is a success for our purposes. Other failures are retried up to
        ``max_tries`` and then logged rather than raised, since promotion is a
        non-fatal step in the listing flows.
        """
        tries = 0
        while tries < max_tries:
            try:
                fn()
                return
            except self.session.Error as e:
                error_details = self.session.parse_error(e)
                if error_details.get("errorId") == AD_ALREADY_EXISTS_ERROR:
                    logger.info("Ad already exists for %s", label)
                    return

                tries += 1
                if tries >= max_tries:
                    logger.warning("Promoting %s failed: %s", label, e)
                    return

                time.sleep(sleep_s)

    def promote_by_inventory_reference(
        self,
        *,
        sku: str,
        rate: int,
        campaign_id: str | None = None,
        max_tries: int = 2,
        sleep_s: int = 10,
    ) -> None:
        """
        Promote a newly published listing by its inventory reference (SKU).
        """
        campaign_id = self._campaign_id(campaign_id)
        self._create_ad_with_retry(
            lambda: self.api.sell_marketing_create_ad_by_listing_id(
                body={
                    "bidPercentage": rate,
                    "inventoryReferenceId": sku,
                    "inventoryReferenceType": "INVENTORY_ITEM",
                },
                content_type="application/json",
                campaign_id=campaign_id,
            ),
            label=f"sku={sku}",
            max_tries=max_tries,
            sleep_s=sleep_s,
        )

    def promote_by_listing_id(
        self,
        *,
        listing_id: str,
        rate: int,
        campaign_id: str | None = None,
        max_tries: int = 2,
        sleep_s: int = 10,
    ) -> None:
        """
        Promote a listing by its listing ID (used after a variation publish).
        """
        campaign_id = self._campaign_id(campaign_id)
        self._create_ad_with_retry(
            lambda: self.api.sell_marketing_create_ad_by_listing_id(
                body={"bidPercentage": rate, "listingId": str(listing_id)},
                content_type="application/json",
                campaign_id=campaign_id,
            ),
            label=f"listing_id={listing_id}",
            max_tries=max_tries,
            sleep_s=sleep_s,
        )

    def create_ads_by_inventory_reference(
        self,
        *,
        sku: str,
        rate: int,
        campaign_id: str | None = None,
    ) -> Any:
        """
        Create ads for every listing under an inventory reference (SKU).

        Raises on failure; the relist flow treats promotion as best-effort and
        handles the error itself.
        """
        campaign_id = self._campaign_id(campaign_id)
        return self.api.sell_marketing_create_ads_by_inventory_reference(
            body={
                "bidPercentage": str(rate),
                "inventoryReferenceId": sku,
                "inventoryReferenceType": "INVENTORY_ITEM",
            },
            content_type="application/json",
            campaign_id=campaign_id,
        )

    # ------------------------------------------------------------------
    # Item promotions
    # ------------------------------------------------------------------

    def create_item_promotion(self, body: dict[str, Any]) -> Any:
        """Create an item promotion from a fully-formed request body."""
        return self.api.sell_marketing_create_item_promotion(
            body=body,
            content_type="application/json",
        )

    def create_volume_discount_promotion(
        self,
        *,
        listing_id: str,
        name: str,
        discount_tiers: list[dict[int, int]],
        end_date: str | None = None,
        max_tries: int = 10,
        marketplace_id: str = DEFAULT_MARKETPLACE_ID,
    ) -> dict[str, Any]:
        """
        Create a VOLUME_DISCOUNT item promotion for listing_id.

        discount_tiers: list of single-entry dicts mapping min_quantity → pct_off_order.
            e.g. [{2: 15}, {3: 20}, {4: 25}]
        A base rule of {1: 0} is automatically inserted when not provided.

        A freshly published listing is not immediately visible to the marketing
        API (error 38227), so this retries with backoff and pushes the start date
        forward on each attempt.
        """
        rules_map: dict[int, int] = {}
        for tier in discount_tiers:
            for qty, pct in tier.items():
                rules_map[qty] = pct
        if 1 not in rules_map:
            rules_map[1] = 0

        now = datetime.now(UTC)
        start = (now + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end = end_date or (now + timedelta(days=365 * 3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        body = {
            "marketplaceId": marketplace_id,
            "promotionType": "VOLUME_DISCOUNT",
            "promotionStatus": "SCHEDULED",
            "name": name,
            "priority": "PRIORITY_1",
            "startDate": start,
            "endDate": end,
            "inventoryCriterion": {
                "inventoryCriterionType": "INVENTORY_BY_VALUE",
                "listingIds": [listing_id],
            },
            "discountRules": [
                {
                    "ruleOrder": i + 1,
                    "discountBenefit": {"percentageOffOrder": str(pct)},
                    "discountSpecification": {"minQuantity": qty},
                }
                for i, (qty, pct) in enumerate(sorted(rules_map.items()))
            ],
        }

        logger.debug("Body for volume pricing request: %s", body)

        for attempt in range(1, max_tries + 1):
            try:
                result = self.create_item_promotion(body)
                logger.info(
                    "Created volume discount promotion listing_id=%s name=%r",
                    listing_id,
                    name,
                )
                return result
            except self.session.Error as e:
                error_details = self.session.parse_error(e)
                if (
                    error_details.get("errorId") == LISTING_NOT_VISIBLE_ERROR
                    and attempt < max_tries
                ):
                    wait = attempt * 30
                    logger.warning(
                        "Listing not yet visible to marketing API (attempt %d/%d), retrying in %ds...",
                        attempt,
                        max_tries,
                        wait,
                    )
                    time.sleep(wait)
                    body["startDate"] = (datetime.now(UTC) + timedelta(minutes=1)).strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z"
                    )
                else:
                    raise
