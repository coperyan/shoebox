from __future__ import annotations

import functools
import logging
from typing import Any

from shoebox.settings import Settings
from shoebox.transforms.listing_builder import (
    inventory_item_body_with_title,
    offer_body_with_store_categories,
)

from .analytics import AnalyticsClient
from .browse import BrowseClient
from .errors import EbayClientError
from .fulfillment import FulfillmentClient
from .inventory import InventoryClient, listing_id_of, offer_id_of
from .marketing import MarketingClient
from .negotiation import NegotiationClient
from .session import EbaySession, build_session
from .stores import StoresClient
from .trading import TradingClient

logger = logging.getLogger(__name__)


class EbayClient:
    """
    Facade client: stable API for pipelines.
    Internally delegates to sub-clients.
    """

    def __init__(self, settings: Settings | None = None):
        self.session: EbaySession = build_session(settings)
        self.api = self.session.api

        self.analytics = AnalyticsClient(self.session)
        self.browse = BrowseClient(self.session)
        self.fulfillment = FulfillmentClient(self.session)
        self.inventory = InventoryClient(self.session)
        self.marketing = MarketingClient(self.session)
        self.negotiation = NegotiationClient(self.session)
        self.stores = StoresClient(self.session)

    @functools.cached_property
    def trading(self) -> TradingClient:
        """Trading API (XML) client, built on first use.

        Lazy because it needs its own Auth'n'Auth token file and most
        pipelines are REST-only; they should not fail for want of it.
        """
        return TradingClient(token_path=self.session.settings.ebay.trading_token_path)

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

        out["inventory_item"] = self.inventory.upsert_inventory_item(sku, inventory_item)

        existing = self.inventory.find_offer(sku)
        if existing is None:
            logger.info("No existing offer found for sku=%s", sku)
            out["offer"] = self.inventory.create_offer(offer)
            logger.info("Created offer for sku=%s offer_id=%s", sku, offer_id_of(out["offer"]))
        elif existing_offer_action == "delete":
            self.inventory.delete_offer(existing.offer_id)
            logger.info("Deleted existing offer ID: %s", existing.offer_id)
            out["offer"] = self.inventory.create_offer(offer)
            logger.info("Created offer for sku=%s offer_id=%s", sku, offer_id_of(out["offer"]))
        elif existing_offer_action == "update":
            self.inventory.update_offer(existing.offer_id, offer)
            out["offer"] = {"offer_id": existing.offer_id}
            logger.info("Updated offer for sku=%s offer_id=%s", sku, existing.offer_id)
        else:
            raise EbayClientError(
                "Listing exists & existing_offer_action is not update or delete. "
                f"sku={sku} offer_id={existing.offer_id}"
            )

        if publish:
            offer_id = offer_id_of(out["offer"])
            if not offer_id:
                raise EbayClientError(
                    f"Offer create/update response did not contain offer_id: {out['offer']}"
                )

            out["publish"] = self.inventory.publish_offer(offer_id)
            logger.info(
                "Published offer sku=%s offer_id=%s listing_id=%s",
                sku,
                offer_id,
                listing_id_of(out["publish"]),
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

        out["inventory_item"] = self.inventory.upsert_inventory_item(sku, inventory_item_body)

        self.inventory.withdraw_offer(existing_offer_id)
        self.inventory.delete_offer(existing_offer_id)
        logger.info("Withdrew and deleted offer offer_id=%s for sku=%s", existing_offer_id, sku)

        out["offer"] = self.inventory.create_offer(offer_body)
        new_offer_id = offer_id_of(out["offer"])
        logger.info("Created offer offer_id=%s for sku=%s", new_offer_id, sku)

        out["publish"] = self.inventory.publish_offer(new_offer_id)
        logger.info("Published sku=%s listing_id=%s", sku, listing_id_of(out["publish"]))

        if promote_listing and campaign_id:
            try:
                self.marketing.create_ads_by_inventory_reference(
                    sku=sku, rate=promote_rate, campaign_id=campaign_id
                )
                logger.info("Promoted sku=%s campaign_id=%s", sku, campaign_id)
            except Exception as e:
                logger.warning("Failed to promote sku=%s: %s", sku, e)

        return out

    def update_listing_title(
        self,
        *,
        new_title: str,
        sku: str | None = None,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        """Change the title of a published single-card listing.

        Two routes, because a store accumulates listings from both APIs:

        - **With a SKU** (created through the Sell Inventory API) the title
          lives on the inventory item, so this is a createOrReplaceInventoryItem
          round-trip: fetch the item, swap ``product.title``, put it back.
        - **Without one** -- listed via the Trading API or eBay's own form --
          the inventory API can't see the listing at all, so it goes through
          Trading `ReviseFixedPriceItem` by item ID.

        Either way the listing keeps its ID, watchers, and search standing.
        When the inventory route fails on a listing that also has an item ID,
        Trading is tried as a fallback; the returned ``method`` says which one
        actually did the work.
        """
        if not sku and not item_id:
            raise ValueError("update_listing_title needs a sku or an item_id")

        if not sku:
            self.trading.revise_listing_title(item_id, new_title)
            logger.info("Retitled item_id=%s via Trading: %r", item_id, new_title)
            return {"item_id": item_id, "title": new_title, "method": "trading", "skipped": False}

        try:
            item = self.inventory.get_inventory_item(sku)
            current = item.product.title if item.product else None
            if current == new_title:
                logger.info("Title already current for sku=%s; skipping", sku)
                return {"sku": sku, "title": new_title, "method": "inventory", "skipped": True}

            body = inventory_item_body_with_title(item, new_title)
            self.inventory.upsert_inventory_item(sku, body)
        except Exception as inventory_error:
            if not item_id:
                raise
            logger.warning(
                "Inventory retitle failed for sku=%s (%s); trying Trading API",
                sku,
                inventory_error,
            )
            self.trading.revise_listing_title(item_id, new_title)
            logger.info("Retitled item_id=%s via Trading fallback: %r", item_id, new_title)
            return {
                "sku": sku,
                "item_id": item_id,
                "title": new_title,
                "method": "trading_fallback",
                "skipped": False,
            }

        logger.info("Retitled sku=%s: %r -> %r", sku, current, new_title)
        return {
            "sku": sku,
            "item_id": item_id,
            "title": new_title,
            "previous_title": current,
            "method": "inventory",
            "skipped": False,
        }

    def update_listing_store_categories(
        self,
        *,
        categories: list[str],
        category_ids: list[str] | None = None,
        sku: str | None = None,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        """Move a published listing into the given store categories.

        Two routes, mirroring :meth:`update_listing_title`:

        - **With a SKU** the categories live on the offer, so this is a
          getOffers/updateOffer round-trip addressing them by name path.
        - **Without one** it goes through Trading `ReviseFixedPriceItem`, which
          addresses them by numeric ID -- hence ``category_ids``, which the
          caller resolves from the store tree.

        ``categories`` is the full replacement list eBay will store, capped at
        two. A listing already in exactly these categories is skipped.
        """
        if not sku and not item_id:
            raise ValueError("update_listing_store_categories needs a sku or an item_id")
        if not categories:
            raise ValueError("categories must not be empty")

        if not sku:
            if not category_ids:
                raise ValueError(f"item_id={item_id} has no SKU, so Trading needs category_ids")
            self.trading.revise_store_category(
                item_id, category_ids[0], category_ids[1] if len(category_ids) > 1 else None
            )
            logger.info("Recategorized item_id=%s via Trading: %s", item_id, categories)
            return {
                "item_id": item_id,
                "categories": categories,
                "method": "trading",
                "skipped": False,
            }

        offer = self.inventory.find_offer(sku)
        if offer is None:
            raise EbayClientError(f"No offer found for SKU {sku}")

        current = list(offer.store_category_names)
        if current == list(categories):
            logger.info("Store categories already current for sku=%s; skipping", sku)
            return {
                "sku": sku,
                "categories": categories,
                "method": "inventory",
                "skipped": True,
            }

        body = offer_body_with_store_categories(offer.raw, categories)
        self.inventory.update_offer(offer.offer_id, body)
        logger.info("Recategorized sku=%s: %s -> %s", sku, current, categories)
        return {
            "sku": sku,
            "item_id": item_id,
            "categories": categories,
            "previous_categories": current,
            "method": "inventory",
            "skipped": False,
        }

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
            out["inventory_items"][sku] = self.inventory.upsert_inventory_item(sku, payload)
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
        out["item_group"] = self.inventory.upsert_inventory_item_group(group_key, item_group)
        logger.info("[%s] Step 2/4 done — item group upserted", group_key)

        # Step 3 — one offer per SKU (using sku field, not inventoryItemGroupKey)
        logger.info("[%s] Step 3/4: Creating %d offers...", group_key, total_offers)
        out["offers"] = {}
        for idx, (sku, offer_payload) in enumerate(sku_offer_map.items(), 1):
            existing = self.inventory.find_offer(sku)

            if existing is None:
                resp = self.inventory.create_offer(offer_payload)
                out["offers"][sku] = resp
                logger.info(
                    "[%s] Step 3/4: [%d/%d] Created offer sku=%s offer_id=%s",
                    group_key,
                    idx,
                    total_offers,
                    sku,
                    offer_id_of(resp),
                )
            elif existing_offer_action == "delete":
                self.inventory.delete_offer(existing.offer_id)
                resp = self.inventory.create_offer(offer_payload)
                out["offers"][sku] = resp
                logger.info(
                    "[%s] Step 3/4: [%d/%d] Replaced offer sku=%s offer_id=%s",
                    group_key,
                    idx,
                    total_offers,
                    sku,
                    offer_id_of(resp),
                )
            elif existing_offer_action == "update":
                self.inventory.update_offer(existing.offer_id, offer_payload)
                out["offers"][sku] = {"offer_id": existing.offer_id}
                logger.info(
                    "[%s] Step 3/4: [%d/%d] Updated offer sku=%s offer_id=%s",
                    group_key,
                    idx,
                    total_offers,
                    sku,
                    existing.offer_id,
                )
            else:
                raise EbayClientError(
                    f"Offer exists for sku={sku} and existing_offer_action is not update or delete."
                )
        logger.info("[%s] Step 3/4 done — %d offers created", group_key, total_offers)

        # Step 4 — publish the entire group as one variation listing
        if publish:
            logger.info("[%s] Step 4/4: Publishing variation listing...", group_key)
            out["publish"] = self.inventory.publish_offer_by_group(group_key)

            listing_id = listing_id_of(out["publish"])
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
