"""Error translation at the SDK boundary, and the lazily built Trading client."""

from __future__ import annotations

import json

import pytest
from ebay_rest import Error as EbayRestError

from shoebox.clients.ebay.errors import (
    INVENTORY_TRANSIENT_ERROR,
    OFFER_NOT_FOUND_ERROR,
    EbayApiError,
)
from shoebox.clients.ebay.session import RestApi, unwrap_records


def _rest_error(error_id: int | None) -> EbayRestError:
    detail = (
        json.dumps({"errors": [{"errorId": error_id, "message": "boom"}]}) if error_id else None
    )
    return EbayRestError(number=99999, reason="eBay rejected the call", detail=detail)


class TestEbayApiError:
    def test_parses_the_error_id_out_of_the_sdk_detail(self):
        e = EbayApiError.from_rest_error(_rest_error(OFFER_NOT_FOUND_ERROR))
        assert e.error_id == OFFER_NOT_FOUND_ERROR
        assert e.number == 99999
        assert e.reason == "eBay rejected the call"
        assert e.errors[0]["message"] == "boom"

    def test_no_detail_means_no_error_id(self):
        e = EbayApiError.from_rest_error(_rest_error(None))
        assert e.error_id is None
        assert "eBay rejected the call" in str(e)

    def test_non_json_detail_is_tolerated(self):
        e = EbayApiError.from_rest_error(
            EbayRestError(number=1, reason="r", detail="<html>not json</html>")
        )
        assert e.error_id is None
        assert e.errors == []


class TestRestApi:
    class FakeSdk:
        marker = "not callable"

        def sell_inventory_get_inventory_item(self, sku):
            raise _rest_error(INVENTORY_TRANSIENT_ERROR)

        def sell_inventory_get_offers(self, sku):
            yield {"record": {"offer_id": "1"}}
            raise _rest_error(OFFER_NOT_FOUND_ERROR)

        def sell_marketing_get_campaigns(self):
            return [{"record": {"campaign_id": "c"}}, {"total": 1}]

    def test_translates_errors_raised_on_the_call(self):
        api = RestApi(self.FakeSdk())
        with pytest.raises(EbayApiError) as info:
            api.sell_inventory_get_inventory_item(sku="A")
        assert info.value.error_id == INVENTORY_TRANSIENT_ERROR
        assert isinstance(info.value.__cause__, EbayRestError)

    def test_translates_errors_raised_while_iterating_a_generator(self):
        api = RestApi(self.FakeSdk())
        gen = api.sell_inventory_get_offers(sku="A")
        assert next(gen) == {"record": {"offer_id": "1"}}
        with pytest.raises(EbayApiError) as info:
            next(gen)
        assert info.value.error_id == OFFER_NOT_FOUND_ERROR

    def test_plain_results_and_attributes_pass_through(self):
        api = RestApi(self.FakeSdk())
        assert unwrap_records(api.sell_marketing_get_campaigns()) == [{"campaign_id": "c"}]
        assert api.marker == "not callable"


def test_unwrap_records_skips_control_entries():
    resp = [{"record": {"a": 1}}, {"total": 2}, "junk", {"record": {"a": 2}}]
    assert unwrap_records(resp) == [{"a": 1}, {"a": 2}]


class TestLazyTrading:
    def test_client_construction_does_not_read_the_trading_token(self, monkeypatch, tmp_path):
        from shoebox.clients.ebay import client as mod
        from shoebox.clients.ebay.session import EbaySession

        token_file = tmp_path / "missing.json"
        settings = type(
            "S",
            (),
            {"ebay": type("E", (), {"trading_token_path": str(token_file), "campaign_id": "c"})()},
        )()
        monkeypatch.setattr(
            mod,
            "build_session",
            lambda s=None: EbaySession(api=RestApi(object()), settings=settings),
        )

        client = mod.EbayClient(settings=settings)  # would raise if it touched the token file

        with pytest.raises(FileNotFoundError, match="Missing Trading API token file"):
            client.trading  # noqa: B018 -- the access is the point

        token_file.write_text(json.dumps({"token": "t"}))
        assert client.trading.token == "t"
        assert client.trading is client.trading  # cached
