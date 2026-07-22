from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from shoebox.settings import Settings

from .analytics import AnalyticsClient
from .browse import BrowseClient
from .fulfillment import FulfillmentClient
from .inventory import InventoryClient
from .marketing import MarketingClient
from .negotiation import NegotiationClient
from .session import EbayClientError, EbaySession, build_session

logger = logging.getLogger(__name__)


class EbayClient:
    """
    Facade client: stable API for pipelines.
    Internally delegates to sub-clients.
    """

    def __init__(self, settings: Settings | None = None):
        self.session: EbaySession = build_session(settings)
        self.api = self.session.api
        self.legacy_api = self.session.legacy_api

        self.analytics = AnalyticsClient(self.session)
        self.browse = BrowseClient(self.session)
        self.fulfillment = FulfillmentClient(self.session)
        self.inventory = InventoryClient(self.session)
        self.marketing = MarketingClient(self.session)
        self.negotiation = NegotiationClient(self.session)

    def _search_existing_offers(self, sku: str) -> dict[str, Any] | None:
        resp = self.api.sell_inventory_get_offers(sku=sku)
        offers = [x for x in resp if "record" in x]
        if len(offers) > 1:
            raise EbayClientError("More than one offer found for SKU.")
        if len(offers) == 0:
            return None

        record = offers[0].get("record") or {}
        listing = record.get("listing") or {}
        return {
            "offer_id": record.get("offer_id"),
            "listing_status": listing.get("listing_status") or "NOT_LISTED",
        }

    def promote_new_listing(
        self,
        *,
        rate: int,
        sku: str,
        campaign_id: str | None = None,
        max_tries: int = 2,
        sleep_s: int = 10,
    ) -> None:
        campaign_id = campaign_id or self.session.settings.ebay.campaign_id
        tries = 0

        while tries < max_tries:
            try:
                self.api.sell_marketing_create_ad_by_listing_id(
                    body={
                        "bidPercentage": rate,
                        "inventoryReferenceId": sku,
                        "inventoryReferenceType": "INVENTORY_ITEM",
                    },
                    content_type="application/json",
                    campaign_id=campaign_id,
                )
                return
            except self.session.Error as e:
                error_details = self.session.parse_error(e)
                if error_details.get("errorId") == 35036:
                    logger.info("Ad already exists for sku=%s", sku)
                    return

                tries += 1
                if tries >= max_tries:
                    logger.warning("Promoting listing failed: %s", e)
                    return

                time.sleep(sleep_s)

    def create_listing_from_inventory_flow(
        self,
        *,
        sku: str,
        inventory_item: dict[str, Any],
        offer: dict[str, Any],
        publish: bool = True,
        existing_offer_action: str = "delete",
        promote_listing: bool = True,
        campaign_id: str | None = None,
        promote_rate: int = 10,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}

        # eBay's inventory API occasionally returns transient 500s (errorId 25001).
        # Retry up to 3 times with backoff before giving up.
        max_inv_tries = 3
        for attempt in range(1, max_inv_tries + 1):
            try:
                out["inventory_item"] = self.api.sell_inventory_create_or_replace_inventory_item(
                    body=inventory_item,
                    content_language="en-US",
                    content_type="application/json",
                    sku=sku,
                )
                break
            except self.session.Error as e:
                error_details = self.session.parse_error(e)
                error_id = error_details.get("errorId")
                # 25001 = Core Inventory Service internal error (transient 500)
                if error_id == 25001 and attempt < max_inv_tries:
                    logger.warning(
                        "eBay inventory 500 for sku=%s (attempt %d/%d), retrying in %ds...",
                        sku,
                        attempt,
                        max_inv_tries,
                        attempt * 5,
                    )
                    time.sleep(attempt * 5)
                else:
                    raise

        try:
            existing = self._search_existing_offers(sku)
        except self.session.Error as e:
            logger.info("No existing offer found for sku=%s (%s)", sku, e)
            existing = None

        if not existing:
            out["offer"] = self.api.sell_inventory_create_offer(
                body=offer,
                content_language="en-US",
                content_type="application/json",
            )
            logger.info(
                "Created offer for sku=%s offer_id=%s",
                sku,
                out["offer"].get("offer_id") or out["offer"].get("offerId"),
            )
        else:
            offer_id = existing.get("offer_id")

            if existing_offer_action == "delete":
                self.api.sell_inventory_delete_offer(offer_id=offer_id)
                logger.info("Deleted existing offer ID: %s", offer_id)
                out["offer"] = self.api.sell_inventory_create_offer(
                    body=offer,
                    content_language="en-US",
                    content_type="application/json",
                )
                logger.info(
                    "Created offer for sku=%s offer_id=%s",
                    sku,
                    out["offer"].get("offer_id") or out["offer"].get("offerId"),
                )
            elif existing_offer_action == "update":
                self.api.sell_inventory_update_offer(
                    offer_id=offer_id,
                    content_language="en-US",
                    content_type="application/json",
                    body=offer,
                )
                out["offer"] = {"offer_id": offer_id}
                logger.info("Updated offer for sku=%s offer_id=%s", sku, offer_id)
            else:
                raise EbayClientError(
                    "Listing exists & existing_offer_action is not update or delete. "
                    f"sku={sku} offer_id={offer_id}"
                )

        if publish:
            offer_id = out["offer"].get("offerId") or out["offer"].get("offer_id")
            if not offer_id:
                raise EbayClientError(
                    f"Offer create/update response did not contain offer_id: {out['offer']}"
                )

            out["publish"] = self.api.sell_inventory_publish_offer(offer_id=offer_id)
            logger.info(
                "Published offer sku=%s offer_id=%s listing_id=%s",
                sku,
                offer_id,
                out["publish"].get("listing_id"),
            )

            if promote_listing:
                self.promote_new_listing(rate=promote_rate, sku=sku, campaign_id=campaign_id)

        return out

    def refresh_listing_flow(
        self,
        *,
        sku: str,
        inventory_item_body: dict[str, Any],
        offer_body: dict[str, Any],
        existing_offer_id: str,
        existing_ad_id: str | None = None,
        campaign_id: str | None = None,
        promote_listing: bool = False,
        promote_rate: int = 7,
    ) -> dict[str, Any]:
        """
        Relist flow: replace inventory item, replace offer, publish.
        Optionally deletes an existing promoted ad then recreates it after publish.
        """
        out: dict[str, Any] = {}

        if existing_ad_id and campaign_id:
            try:
                self.api.sell_marketing_delete_ad(campaign_id=campaign_id, ad_id=existing_ad_id)
                logger.info("Deleted ad ad_id=%s for sku=%s", existing_ad_id, sku)
            except Exception as e:
                logger.warning("Failed to delete ad ad_id=%s: %s", existing_ad_id, e)

        max_inv_tries = 3
        for attempt in range(1, max_inv_tries + 1):
            try:
                out["inventory_item"] = self.api.sell_inventory_create_or_replace_inventory_item(
                    body=inventory_item_body,
                    content_language="en-US",
                    content_type="application/json",
                    sku=sku,
                )
                break
            except self.session.Error as e:
                error_details = self.session.parse_error(e)
                if error_details.get("errorId") == 25001 and attempt < max_inv_tries:
                    logger.warning(
                        "eBay inventory 500 sku=%s (attempt %d/%d), retrying in %ds...",
                        sku,
                        attempt,
                        max_inv_tries,
                        attempt * 5,
                    )
                    time.sleep(attempt * 5)
                else:
                    raise

        self.api.sell_inventory_withdraw_offer(offer_id=existing_offer_id)
        self.api.sell_inventory_delete_offer(offer_id=existing_offer_id)
        logger.info("Withdrew and deleted offer offer_id=%s for sku=%s", existing_offer_id, sku)

        out["offer"] = self.api.sell_inventory_create_offer(
            body=offer_body,
            content_language="en-US",
            content_type="application/json",
        )
        new_offer_id = out["offer"].get("offer_id") or out["offer"].get("offerId")
        logger.info("Created offer offer_id=%s for sku=%s", new_offer_id, sku)

        out["publish"] = self.api.sell_inventory_publish_offer(offer_id=new_offer_id)
        logger.info("Published sku=%s listing_id=%s", sku, out["publish"].get("listing_id"))

        if promote_listing and campaign_id:
            try:
                self.api.sell_marketing_create_ads_by_inventory_reference(
                    body={
                        "bidPercentage": str(promote_rate),
                        "inventoryReferenceId": sku,
                        "inventoryReferenceType": "INVENTORY_ITEM",
                    },
                    content_type="application/json",
                    campaign_id=campaign_id,
                )
                logger.info("Promoted sku=%s campaign_id=%s", sku, campaign_id)
            except Exception as e:
                logger.warning("Failed to promote sku=%s: %s", sku, e)

        return out

    def create_variation_listing_flow(
        self,
        *,
        group_key: str,
        sku_item_map: dict[str, Any],
        sku_offer_map: dict[str, Any],
        item_group: dict[str, Any],
        publish: bool = True,
        existing_offer_action: str = "delete",
        promote_listing: bool = True,
        campaign_id: str | None = None,
        promote_rate: int = 20,
        volume_discount_tiers: list[dict[int, int]] | None = None,
    ) -> dict[str, Any]:
        """
        Full flow for a multi-variation listing:
          1. Upsert one inventory item per SKU.
          2. Upsert the inventory item group.
          3. Create/replace one offer per SKU (each with its own price).
          4. Publish via publishByInventoryItemGroup and optionally promote.

        Individual offers per SKU are used instead of a single group offer so
        that per-variation pricing is supported and the inventoryItemGroupKey
        field (which some library versions reject) is avoided in createOffer.
        """
        campaign_id = campaign_id or self.session.settings.ebay.campaign_id

        out: dict[str, Any] = {}
        total_items = len(sku_item_map)
        total_offers = len(sku_offer_map)

        # Step 1 — individual inventory items (one per variation)
        logger.info("[%s] Step 1/4: Upserting %d inventory items...", group_key, total_items)
        out["inventory_items"] = {}
        for idx, (sku, payload) in enumerate(sku_item_map.items(), 1):
            for attempt in range(1, 4):
                try:
                    out["inventory_items"][sku] = (
                        self.api.sell_inventory_create_or_replace_inventory_item(
                            body=payload,
                            content_language="en-US",
                            content_type="application/json",
                            sku=sku,
                        )
                    )
                    logger.info(
                        "[%s] Step 1/4: [%d/%d] Upserted inventory item sku=%s",
                        group_key,
                        idx,
                        total_items,
                        sku,
                    )
                    break
                except self.session.Error as e:
                    error_details = self.session.parse_error(e)
                    if error_details.get("errorId") == 25001 and attempt < 3:
                        logger.warning(
                            "eBay inventory 500 for sku=%s (attempt %d/3), retrying in %ds…",
                            sku,
                            attempt,
                            attempt * 5,
                        )
                        time.sleep(attempt * 5)
                    else:
                        raise
        logger.info("[%s] Step 1/4 done — %d inventory items upserted", group_key, total_items)

        # Step 2 — inventory item group
        logger.info("[%s] Step 2/4: Upserting item group...", group_key)
        out["item_group"] = self.api.sell_inventory_create_or_replace_inventory_item_group(
            inventory_item_group_key=group_key,
            body=item_group,
            content_language="en-US",
            content_type="application/json",
        )
        logger.info("[%s] Step 2/4 done — item group upserted", group_key)

        # Step 3 — one offer per SKU (using sku field, not inventoryItemGroupKey)
        logger.info("[%s] Step 3/4: Creating %d offers...", group_key, total_offers)
        out["offers"] = {}
        for idx, (sku, offer_payload) in enumerate(sku_offer_map.items(), 1):
            try:
                existing = self._search_existing_offers(sku)
            except self.session.Error as e:
                logger.debug("No existing offer for sku=%s (%s)", sku, e)
                existing = None

            if not existing:
                resp = self.api.sell_inventory_create_offer(
                    body=offer_payload,
                    content_language="en-US",
                    content_type="application/json",
                )
                out["offers"][sku] = resp
                logger.info(
                    "[%s] Step 3/4: [%d/%d] Created offer sku=%s offer_id=%s",
                    group_key,
                    idx,
                    total_offers,
                    sku,
                    resp.get("offerId") or resp.get("offer_id"),
                )
            elif existing_offer_action == "delete":
                self.api.sell_inventory_delete_offer(offer_id=existing["offer_id"])
                resp = self.api.sell_inventory_create_offer(
                    body=offer_payload,
                    content_language="en-US",
                    content_type="application/json",
                )
                out["offers"][sku] = resp
                logger.info(
                    "[%s] Step 3/4: [%d/%d] Replaced offer sku=%s offer_id=%s",
                    group_key,
                    idx,
                    total_offers,
                    sku,
                    resp.get("offerId") or resp.get("offer_id"),
                )
            elif existing_offer_action == "update":
                self.api.sell_inventory_update_offer(
                    offer_id=existing["offer_id"],
                    content_language="en-US",
                    content_type="application/json",
                    body=offer_payload,
                )
                out["offers"][sku] = {"offer_id": existing["offer_id"]}
                logger.info(
                    "[%s] Step 3/4: [%d/%d] Updated offer sku=%s offer_id=%s",
                    group_key,
                    idx,
                    total_offers,
                    sku,
                    existing["offer_id"],
                )
            else:
                raise EbayClientError(
                    f"Offer exists for sku={sku} and existing_offer_action is not update or delete."
                )
        logger.info("[%s] Step 3/4 done — %d offers created", group_key, total_offers)

        # Step 4 — publish the entire group as one variation listing
        if publish:
            logger.info("[%s] Step 4/4: Publishing variation listing...", group_key)
            max_pub_tries = 3
            for attempt in range(1, max_pub_tries + 1):
                try:
                    out["publish"] = self.api.sell_inventory_publish_offer_by_inventory_item_group(
                        body={
                            "inventoryItemGroupKey": group_key,
                            "marketplaceId": "EBAY_US",
                        },
                        content_type="application/json",
                    )
                    break
                except self.session.Error as e:
                    error_details = self.session.parse_error(e)
                    if error_details.get("errorId") == 25001 and attempt < max_pub_tries:
                        logger.warning(
                            "eBay publish 500 for group=%s (attempt %d/%d), retrying in %ds...",
                            group_key,
                            attempt,
                            max_pub_tries,
                            attempt * 5,
                        )
                        time.sleep(attempt * 5)
                    else:
                        raise

            listing_id = out["publish"].get("listingId") or out["publish"].get("listing_id")
            logger.info("[%s] Step 4/4 done — listing_id=%s", group_key, listing_id)

            if promote_listing and listing_id:
                logger.info("[%s] Promoting listing listing_id=%s...", group_key, listing_id)
                self._promote_variation_listing(
                    listing_id=listing_id,
                    rate=promote_rate,
                    campaign_id=campaign_id,
                )

            if volume_discount_tiers and listing_id:
                logger.info(
                    "[%s] Creating volume discount promotion listing_id=%s tiers=%s...",
                    group_key,
                    listing_id,
                    volume_discount_tiers,
                )
                try:
                    self.create_volume_discount_promotion(
                        listing_id=str(listing_id),
                        name=item_group.get("title", group_key),
                        discount_tiers=volume_discount_tiers,
                    )
                except Exception:
                    logger.warning(
                        "Failed to create volume discount promotion for group=%s",
                        group_key,
                        exc_info=True,
                    )
            elif not listing_id:
                logger.warning(
                    "[%s] Skipping volume discount — listing_id not available", group_key
                )
            elif not volume_discount_tiers:
                logger.info("[%s] No volume_discount_tiers set, skipping promotion", group_key)

        return out

    def create_volume_discount_promotion(
        self,
        *,
        listing_id: str,
        name: str,
        discount_tiers: list[dict[int, int]],
        end_date: str | None = None,
        max_tries: int | None = 10,
    ) -> dict[str, Any]:
        """
        Create a VOLUME_DISCOUNT item promotion for listing_id.

        discount_tiers: list of single-entry dicts mapping min_quantity → pct_off_order.
            e.g. [{2: 15}, {3: 20}, {4: 25}]
        A base rule of {1: 0} is automatically inserted when not provided.
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
            "marketplaceId": "EBAY_US",
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
                result = self.api.sell_marketing_create_item_promotion(
                    body=body,
                    content_type="application/json",
                )
                logger.info(
                    "Created volume discount promotion listing_id=%s name=%r", listing_id, name
                )
                return result
            except self.session.Error as e:
                error_details = self.session.parse_error(e)
                if error_details.get("errorId") == 38227 and attempt < max_tries:
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

    def _promote_variation_listing(
        self,
        *,
        listing_id: str,
        rate: int,
        campaign_id: str | None = None,
        max_tries: int = 2,
        sleep_s: int = 10,
    ) -> None:
        campaign_id = campaign_id or self.session.settings.ebay.campaign_id
        tries = 0
        while tries < max_tries:
            try:
                self.api.sell_marketing_create_ad_by_listing_id(
                    body={"bidPercentage": rate, "listingId": str(listing_id)},
                    content_type="application/json",
                    campaign_id=campaign_id,
                )
                return
            except self.session.Error as e:
                error_details = self.session.parse_error(e)
                if error_details.get("errorId") == 35036:
                    logger.info("Ad already exists for listing_id=%s", listing_id)
                    return
                tries += 1
                if tries >= max_tries:
                    logger.warning(
                        "Promoting variation listing failed listing_id=%s: %s",
                        listing_id,
                        e,
                    )
                    return
                time.sleep(sleep_s)


_default: EbayClient | None = None


def get_client(settings: Settings | None = None) -> EbayClient:
    global _default
    if _default is None:
        _default = EbayClient(settings=settings)
    return _default
