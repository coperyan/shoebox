"""InventoryClient: offer lookup semantics, the 25001 retry, and id normalization."""

from __future__ import annotations

from typing import Any

import pytest

from shoebox.clients.ebay.errors import (
    INVENTORY_TRANSIENT_ERROR,
    OFFER_NOT_FOUND_ERROR,
    EbayApiError,
    EbayClientError,
)
from shoebox.clients.ebay.inventory import InventoryClient, listing_id_of, offer_id_of


def api_error(error_id: int | None) -> EbayApiError:
    return EbayApiError(f"error {error_id}", error_id=error_id)


class FakeApi:
    """Records sell_inventory_* calls and replays queued responses (or raises them)."""

    def __init__(self, responses: dict[str, Any] | None = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = responses or {}

    def __getattr__(self, name: str):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            result = self.responses.get(name)
            if isinstance(result, list) and result and isinstance(result[0], Exception):
                raise result.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        return call


def build_client(responses=None):
    api = FakeApi(responses)
    session = type("S", (), {"api": api})()
    return InventoryClient(session), api


class TestOffers:
    def test_get_offers_returns_typed_offers(self):
        client, _ = build_client(
            {
                "sell_inventory_get_offers": [
                    {"record": {"offer_id": "1", "sku": "A", "store_category_names": ["/x"]}},
                    {"total": 1},
                ]
            }
        )
        offers = client.get_offers("A")
        assert [o.offer_id for o in offers] == ["1"]
        assert offers[0].store_category_names == ["/x"]
        assert offers[0].raw["sku"] == "A"

    def test_get_offers_is_empty_when_the_sku_has_no_offer_yet(self):
        client, _ = build_client({"sell_inventory_get_offers": api_error(OFFER_NOT_FOUND_ERROR)})
        assert client.get_offers("A") == []

    def test_get_offers_raises_other_errors(self):
        client, _ = build_client({"sell_inventory_get_offers": api_error(500)})
        with pytest.raises(EbayApiError):
            client.get_offers("A")

    def test_find_offer_none_one_many(self):
        none, _ = build_client({"sell_inventory_get_offers": []})
        assert none.find_offer("A") is None

        one, _ = build_client({"sell_inventory_get_offers": [{"record": {"offer_id": "1"}}]})
        assert one.find_offer("A").offer_id == "1"

        many, _ = build_client(
            {
                "sell_inventory_get_offers": [
                    {"record": {"offer_id": "1"}},
                    {"record": {"offer_id": "2"}},
                ]
            }
        )
        with pytest.raises(EbayClientError, match="More than one offer"):
            many.find_offer("A")

    def test_create_offer_sends_content_headers_and_does_not_retry(self):
        client, api = build_client(
            {"sell_inventory_create_offer": api_error(INVENTORY_TRANSIENT_ERROR)}
        )
        with pytest.raises(EbayApiError):
            client.create_offer({"sku": "A"})
        assert len(api.calls) == 1
        name, kwargs = api.calls[0]
        assert name == "sell_inventory_create_offer"
        assert kwargs["content_language"] == "en-US"
        assert kwargs["content_type"] == "application/json"


class TestRetry:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr("shoebox.clients.ebay.inventory.time.sleep", lambda _: None)

    def test_upsert_retries_transient_25001_then_succeeds(self):
        client, api = build_client(
            {
                "sell_inventory_create_or_replace_inventory_item": [
                    api_error(INVENTORY_TRANSIENT_ERROR),
                    api_error(INVENTORY_TRANSIENT_ERROR),
                ]
            }
        )
        client.upsert_inventory_item("A", {"product": {}})
        assert len(api.calls) == 3
        assert api.calls[0][1]["sku"] == "A"

    def test_upsert_gives_up_after_max_tries(self):
        client, api = build_client(
            {
                "sell_inventory_create_or_replace_inventory_item": [
                    api_error(INVENTORY_TRANSIENT_ERROR) for _ in range(3)
                ]
            }
        )
        with pytest.raises(EbayApiError):
            client.upsert_inventory_item("A", {})
        assert len(api.calls) == 3

    def test_other_errors_are_not_retried(self):
        client, api = build_client(
            {"sell_inventory_create_or_replace_inventory_item": api_error(25002)}
        )
        with pytest.raises(EbayApiError):
            client.upsert_inventory_item("A", {})
        assert len(api.calls) == 1

    def test_publish_by_group_builds_the_body(self):
        client, api = build_client(
            {"sell_inventory_publish_offer_by_inventory_item_group": {"listingId": "9"}}
        )
        resp = client.publish_offer_by_group("G1")
        assert listing_id_of(resp) == "9"
        _, kwargs = api.calls[0]
        assert kwargs["body"] == {"inventoryItemGroupKey": "G1", "marketplaceId": "EBAY_US"}


def test_id_normalizers_accept_both_spellings():
    assert offer_id_of({"offer_id": 12}) == "12"
    assert offer_id_of({"offerId": "34"}) == "34"
    assert offer_id_of({}) is None
    assert offer_id_of(None) is None
    assert listing_id_of({"listing_id": "1"}) == "1"
    assert listing_id_of({"listingId": 2}) == "2"
