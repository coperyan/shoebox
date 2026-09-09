"""Sell Inventory API: inventory items, offers, and publishing.

Every ``sell_inventory_*`` call in the codebase goes through here, so two eBay
quirks are handled in this module and nowhere else:

* errorId 25001 ("Core Inventory Service internal error", HTTP 500) is
  transient. The idempotent calls (item and group upserts, offer updates,
  publishes) retry it with linear backoff. Creates, deletes and withdrawals do
  not: a replay of one of those could double up or hide a real failure.
* ``getOffers`` answers 404 / errorId 25713 ("This Offer is not available")
  for a SKU that has no offer yet. That is the normal state of a brand-new
  listing, so :meth:`InventoryClient.get_offers` returns an empty list.

ebay_rest spells ids as ``offer_id`` on some responses and ``offerId`` on
others (likewise ``listing_id`` / ``listingId``). :func:`offer_id_of` and
:func:`listing_id_of` absorb that once so callers stop hedging.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from ...models.ebay.inventory_item import InventoryItem
from ...models.ebay.offer import Offer
from .errors import (
    INVENTORY_TRANSIENT_ERROR,
    OFFER_NOT_FOUND_ERROR,
    EbayApiError,
    EbayClientError,
)
from .session import EbaySession, unwrap_records

logger = logging.getLogger(__name__)

CONTENT_LANGUAGE = "en-US"
CONTENT_TYPE = "application/json"
DEFAULT_MARKETPLACE_ID = "EBAY_US"
# bulkGetInventoryItem accepts at most 25 SKUs per request.
BULK_GET_CHUNK_SIZE = 25
# Linear backoff for 25001: 5s, 10s, ...
RETRY_BACKOFF_SECONDS = 5


def offer_id_of(response: Any) -> str | None:
    """The offer id in a createOffer / getOffers response, whichever spelling eBay used."""
    if not isinstance(response, dict):
        return None
    value = response.get("offer_id") or response.get("offerId")
    return str(value) if value else None


def listing_id_of(response: Any) -> str | None:
    """The listing id in a publishOffer response, whichever spelling eBay used."""
    if not isinstance(response, dict):
        return None
    value = response.get("listing_id") or response.get("listingId")
    return str(value) if value else None


class InventoryClient:
    def __init__(self, session: EbaySession):
        self.api = session.api

    def _call_with_retry(
        self,
        fn: Callable[[], Any],
        *,
        label: str,
        max_tries: int = 3,
    ) -> Any:
        """
        Invoke ``fn()``, retrying on eBay's transient errorId 25001 with linear
        backoff. Any other error, or 25001 on the final attempt, is raised.

        ``label`` identifies the call in log messages (e.g. "publish sku=ABC").
        """
        for attempt in range(1, max_tries + 1):
            try:
                return fn()
            except EbayApiError as e:
                if e.error_id == INVENTORY_TRANSIENT_ERROR and attempt < max_tries:
                    wait = attempt * RETRY_BACKOFF_SECONDS
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

    # ------------------------------------------------------------------
    # Inventory items
    # ------------------------------------------------------------------

    def get_inventory_item(self, sku: str) -> InventoryItem:
        resp = self.api.sell_inventory_get_inventory_item(sku=sku)
        return InventoryItem.from_api(resp)

    def get_inventory_items(self) -> list[InventoryItem]:
        records = unwrap_records(self.api.sell_inventory_get_inventory_items())
        return [InventoryItem.from_api(r) for r in records]

    def get_bulk_inventory_items(self, skus: list[str]) -> list[InventoryItem]:
        """Fetch many items by SKU, 25 per request (eBay's bulkGetInventoryItem cap)."""
        results: list[dict[str, Any]] = []
        for i in range(0, len(skus), BULK_GET_CHUNK_SIZE):
            chunk = skus[i : i + BULK_GET_CHUNK_SIZE]
            resp = self.api.sell_inventory_bulk_get_inventory_item(
                body={"requests": [{"sku": s} for s in chunk]}
            )
            results.extend(
                x["inventory_item"] for x in resp.get("responses") or [] if "inventory_item" in x
            )
        return [InventoryItem.from_api(r) for r in results]

    def upsert_inventory_item(self, sku: str, body: dict[str, Any]) -> Any:
        """createOrReplaceInventoryItem: a full PUT of the item. Retries 25001."""
        return self._call_with_retry(
            lambda: self.api.sell_inventory_create_or_replace_inventory_item(
                body=body,
                content_language=CONTENT_LANGUAGE,
                content_type=CONTENT_TYPE,
                sku=sku,
            ),
            label=f"inventory upsert sku={sku}",
        )

    def upsert_inventory_item_group(self, group_key: str, body: dict[str, Any]) -> Any:
        """createOrReplaceInventoryItemGroup for a multi-variation listing. Retries 25001."""
        return self._call_with_retry(
            lambda: self.api.sell_inventory_create_or_replace_inventory_item_group(
                inventory_item_group_key=group_key,
                body=body,
                content_language=CONTENT_LANGUAGE,
                content_type=CONTENT_TYPE,
            ),
            label=f"item group upsert group={group_key}",
        )

    # ------------------------------------------------------------------
    # Offers
    # ------------------------------------------------------------------

    def get_offers(self, sku: str) -> list[Offer]:
        """All offers on a SKU. Empty when eBay reports the SKU has none (25713)."""
        try:
            records = unwrap_records(self.api.sell_inventory_get_offers(sku=sku))
        except EbayApiError as e:
            if e.error_id != OFFER_NOT_FOUND_ERROR:
                raise
            return []
        return [Offer.from_api(r) for r in records]

    def find_offer(self, sku: str) -> Offer | None:
        """
        The single offer on a SKU, or ``None`` when there is none.

        Single-card listings carry exactly one offer per SKU; more than one is
        a state this codebase never creates, so it is raised rather than
        silently picking the first.
        """
        offers = self.get_offers(sku)
        if len(offers) > 1:
            raise EbayClientError(f"More than one offer found for sku={sku}")
        return offers[0] if offers else None

    def create_offer(self, body: dict[str, Any]) -> dict[str, Any]:
        """createOffer. Returns eBay's response; read the id with :func:`offer_id_of`."""
        return self.api.sell_inventory_create_offer(
            body=body,
            content_language=CONTENT_LANGUAGE,
            content_type=CONTENT_TYPE,
        )

    def update_offer(self, offer_id: str, body: dict[str, Any]) -> None:
        """updateOffer: a full PUT, every required field must be in ``body``. Retries 25001."""
        self._call_with_retry(
            lambda: self.api.sell_inventory_update_offer(
                offer_id=offer_id,
                content_language=CONTENT_LANGUAGE,
                content_type=CONTENT_TYPE,
                body=body,
            ),
            label=f"offer update offer_id={offer_id}",
        )

    def delete_offer(self, offer_id: str) -> None:
        self.api.sell_inventory_delete_offer(offer_id=offer_id)

    def withdraw_offer(self, offer_id: str) -> None:
        """End the live listing behind an offer without deleting the offer."""
        self.api.sell_inventory_withdraw_offer(offer_id=offer_id)

    def publish_offer(self, offer_id: str) -> dict[str, Any]:
        """publishOffer. Returns eBay's response; read the id with :func:`listing_id_of`. Retries 25001."""
        return self._call_with_retry(
            lambda: self.api.sell_inventory_publish_offer(offer_id=offer_id),
            label=f"publish offer_id={offer_id}",
        )

    def publish_offer_by_group(
        self, group_key: str, *, marketplace_id: str = DEFAULT_MARKETPLACE_ID
    ) -> dict[str, Any]:
        """publishOfferByInventoryItemGroup: one variation listing for the whole group. Retries 25001."""
        return self._call_with_retry(
            lambda: self.api.sell_inventory_publish_offer_by_inventory_item_group(
                body={"inventoryItemGroupKey": group_key, "marketplaceId": marketplace_id},
                content_type=CONTENT_TYPE,
            ),
            label=f"publish group={group_key}",
        )
