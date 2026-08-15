from __future__ import annotations

import logging
import time
from collections.abc import Callable
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

    def _call_with_retry(
        self,
        fn: Callable[[], Any],
        *,
        label: str,
        max_tries: int = 3,
    ) -> Any:
        """
        Invoke ``fn()``, retrying on eBay's transient errorId 25001
        ("Core Inventory Service internal error", HTTP 500) with linear backoff
        (5s, 10s, ...). Any other error, or 25001 on the final attempt, is raised.

        ``label`` identifies the call in log messages (e.g. "publish sku=ABC").
        """
        for attempt in range(1, max_tries + 1):
            try:
                return fn()
            except self.session.Error as e:
                error_details = self.session.parse_error(e)
                if error_details.get("errorId") == 25001 and attempt < max_tries:
                    wait = attempt * 5
                    logger.warning(
                        "eBay transient 500 (25001) on %s (attempt %d/%d), retrying in %ds...",
                        label,
                        attempt,
                        max_tries,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    raise

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

        out["inventory_item"] = self._call_with_retry(
            lambda: self.api.sell_inventory_create_or_replace_inventory_item(
                body=inventory_item,
                content_language="en-US",
                content_type="application/json",
                sku=sku,
            ),
            label=f"inventory upsert sku={sku}",
        )

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

            out["publish"] = self._call_with_retry(
                lambda: self.api.sell_inventory_publish_offer(offer_id=offer_id),
                label=f"publish sku={sku}",
            )
            logger.info(
                "Published offer sku=%s offer_id=%s listing_id=%s",
                sku,
                offer_id,
                out["publish"].get("listing_id"),
            )

            if promote_listing:
                self.marketing.promote_by_inventory_reference(
                    sku=sku, rate=promote_rate, campaign_id=campaign_id
                )

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
                self.marketing.delete_ad(campaign_id=campaign_id, ad_id=existing_ad_id)
                logger.info("Deleted ad ad_id=%s for sku=%s", existing_ad_id, sku)
            except Exception as e:
                logger.warning("Failed to delete ad ad_id=%s: %s", existing_ad_id, e)

        out["inventory_item"] = self._call_with_retry(
            lambda: self.api.sell_inventory_create_or_replace_inventory_item(
                body=inventory_item_body,
                content_language="en-US",
                content_type="application/json",
                sku=sku,
            ),
            label=f"inventory upsert sku={sku}",
        )

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
                self.marketing.create_ads_by_inventory_reference(
                    sku=sku, rate=promote_rate, campaign_id=campaign_id
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
            out["inventory_items"][sku] = self._call_with_retry(
                lambda payload=payload, sku=sku: (
                    self.api.sell_inventory_create_or_replace_inventory_item(
                        body=payload,
                        content_language="en-US",
                        content_type="application/json",
                        sku=sku,
                    )
                ),
                label=f"inventory upsert sku={sku}",
            )
            logger.info(
                "[%s] Step 1/4: [%d/%d] Upserted inventory item sku=%s",
                group_key,
                idx,
                total_items,
                sku,
            )
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
            out["publish"] = self._call_with_retry(
                lambda: self.api.sell_inventory_publish_offer_by_inventory_item_group(
                    body={
                        "inventoryItemGroupKey": group_key,
                        "marketplaceId": "EBAY_US",
                    },
                    content_type="application/json",
                ),
                label=f"publish group={group_key}",
            )

            listing_id = out["publish"].get("listingId") or out["publish"].get("listing_id")
            logger.info("[%s] Step 4/4 done — listing_id=%s", group_key, listing_id)

            if promote_listing and listing_id:
                logger.info("[%s] Promoting listing listing_id=%s...", group_key, listing_id)
                self.marketing.promote_by_listing_id(
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
                    self.marketing.create_volume_discount_promotion(
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


_default: EbayClient | None = None


def get_client(settings: Settings | None = None) -> EbayClient:
    global _default
    if _default is None:
        _default = EbayClient(settings=settings)
    return _default
