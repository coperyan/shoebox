from typing import Any

import pytest

from shoebox.clients.ebay.errors import (
    AD_ALREADY_EXISTS_ERROR,
    LISTING_NOT_VISIBLE_ERROR,
    EbayApiError,
)
from shoebox.clients.ebay.marketing import MarketingClient


def api_error(error_id: int | None = None) -> EbayApiError:
    """An EbayApiError carrying just the eBay errorId the client branches on."""
    return EbayApiError(f"error {error_id}", error_id=error_id)


class FakeApi:
    """Records sell_marketing_* calls and replays queued responses."""

    def __init__(self, responses: dict[str, Any] | None = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = responses or {}

    def __getattr__(self, name: str):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            result = self.responses.get(name)
            if isinstance(result, list) and result and isinstance(result[0], Exception):
                raised = result.pop(0)
                raise raised
            if isinstance(result, Exception):
                raise result
            return result

        return call


class FakeSession:
    def __init__(self, api: FakeApi, campaign_id: str = "DEFAULT_CAMPAIGN"):
        self.api = api
        self.settings = type("S", (), {"ebay": type("E", (), {"campaign_id": campaign_id})()})()


def build_client(responses=None, campaign_id="DEFAULT_CAMPAIGN"):
    api = FakeApi(responses)
    return MarketingClient(FakeSession(api, campaign_id)), api


def test_get_campaigns_filters_by_status():
    client, _ = build_client(
        {
            "sell_marketing_get_campaigns": [
                {"record": {"campaign_id": "1", "campaign_status": "RUNNING"}},
                {"record": {"campaign_id": "2", "campaign_status": "ENDED"}},
                {"total": 2},
            ]
        }
    )

    assert len(client.get_campaigns()) == 2
    assert [c["campaign_id"] for c in client.get_campaigns(status="RUNNING")] == ["1"]


def test_get_campaign_ads_merges_campaign_context():
    client, _ = build_client(
        {
            "sell_marketing_get_campaigns": [
                {
                    "record": {
                        "campaign_id": "1",
                        "campaign_name": "A",
                        "campaign_status": "RUNNING",
                    }
                },
                {"record": {"campaign_id": "2", "campaign_name": "B", "campaign_status": "PAUSED"}},
            ],
            "sell_marketing_get_ads": [{"record": {"ad_id": "9", "listing_id": "555"}}],
        }
    )

    rows = client.get_campaign_ads()

    # Only the RUNNING campaign is queried by default.
    assert len(rows) == 1
    assert rows[0] == {
        "campaign_id": "1",
        "campaign_name": "A",
        "campaign_status": "RUNNING",
        "ad_id": "9",
        "listing_id": "555",
    }


def test_promote_by_inventory_reference_uses_default_campaign():
    client, api = build_client()
    client.promote_by_inventory_reference(sku="ABC", rate=10)

    name, kwargs = api.calls[-1]
    assert name == "sell_marketing_create_ad_by_listing_id"
    assert kwargs["campaign_id"] == "DEFAULT_CAMPAIGN"
    assert kwargs["body"]["inventoryReferenceId"] == "ABC"
    assert kwargs["body"]["bidPercentage"] == 10


def test_promote_by_listing_id_sends_listing_body():
    client, api = build_client()
    client.promote_by_listing_id(listing_id=555, rate=20, campaign_id="C2")

    name, kwargs = api.calls[-1]
    assert name == "sell_marketing_create_ad_by_listing_id"
    assert kwargs["campaign_id"] == "C2"
    assert kwargs["body"] == {"bidPercentage": 20, "listingId": "555"}


def test_promote_swallows_ad_already_exists():
    client, api = build_client(
        {"sell_marketing_create_ad_by_listing_id": api_error(AD_ALREADY_EXISTS_ERROR)}
    )

    client.promote_by_inventory_reference(sku="ABC", rate=10)

    # Treated as success — no retry.
    assert len(api.calls) == 1


def test_promote_retries_then_gives_up_without_raising(monkeypatch):
    monkeypatch.setattr("shoebox.clients.ebay.marketing.time.sleep", lambda _: None)
    client, api = build_client({"sell_marketing_create_ad_by_listing_id": api_error(500)})

    client.promote_by_inventory_reference(sku="ABC", rate=10, max_tries=3)

    assert len(api.calls) == 3


def test_create_ads_by_inventory_reference_stringifies_rate():
    client, api = build_client()
    client.create_ads_by_inventory_reference(sku="ABC", rate=7, campaign_id="C1")

    name, kwargs = api.calls[-1]
    assert name == "sell_marketing_create_ads_by_inventory_reference"
    assert kwargs["body"]["bidPercentage"] == "7"


def test_delete_ad_raises_on_error():
    client, _ = build_client({"sell_marketing_delete_ad": api_error(123)})
    with pytest.raises(EbayApiError):
        client.delete_ad(campaign_id="C1", ad_id="A1")


def test_volume_discount_builds_sorted_rules_with_base_tier():
    client, api = build_client({"sell_marketing_create_item_promotion": {"ok": True}})

    client.create_volume_discount_promotion(
        listing_id="555", name="Bundle", discount_tiers=[{3: 20}, {2: 15}]
    )

    body = api.calls[-1][1]["body"]
    assert body["promotionType"] == "VOLUME_DISCOUNT"
    assert body["inventoryCriterion"]["listingIds"] == ["555"]
    assert [(r["discountSpecification"]["minQuantity"]) for r in body["discountRules"]] == [1, 2, 3]
    assert [r["discountBenefit"]["percentageOffOrder"] for r in body["discountRules"]] == [
        "0",
        "15",
        "20",
    ]
    assert [r["ruleOrder"] for r in body["discountRules"]] == [1, 2, 3]


def test_volume_discount_retries_when_listing_not_visible(monkeypatch):
    monkeypatch.setattr("shoebox.clients.ebay.marketing.time.sleep", lambda _: None)
    client, api = build_client(
        {
            "sell_marketing_create_item_promotion": [
                api_error(LISTING_NOT_VISIBLE_ERROR),
                api_error(LISTING_NOT_VISIBLE_ERROR),
            ]
        }
    )

    client.create_volume_discount_promotion(
        listing_id="555", name="Bundle", discount_tiers=[{2: 15}]
    )

    assert len(api.calls) == 3


def test_volume_discount_raises_on_other_errors():
    client, _ = build_client({"sell_marketing_create_item_promotion": api_error(999)})

    with pytest.raises(EbayApiError):
        client.create_volume_discount_promotion(
            listing_id="555", name="Bundle", discount_tiers=[{2: 15}]
        )
