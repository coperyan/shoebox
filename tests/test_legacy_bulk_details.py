"""Concurrency and connection reuse in the Trading client."""

from __future__ import annotations

import threading

import pytest

from shoebox.clients.ebay_legacy import eBayLegacyClient


def _client(monkeypatch) -> eBayLegacyClient:
    """A client with no credentials read and no network wired up."""
    monkeypatch.setattr(eBayLegacyClient, "_authenticate", lambda self: None)
    client = eBayLegacyClient()
    client.token = "test-token"
    return client


class TestSession:
    def test_calls_reuse_one_pooled_session(self, monkeypatch):
        client = _client(monkeypatch)
        assert client._session is not None

        adapter = client._session.get_adapter("https://api.ebay.com/ws/api.dll")
        assert adapter._pool_maxsize >= eBayLegacyClient.pool_size

    def test_trading_calls_go_through_the_session(self, monkeypatch):
        client = _client(monkeypatch)
        seen = []

        class FakeResponse:
            text = "<GetItemResponse><Ack>Success</Ack></GetItemResponse>"

            def raise_for_status(self):
                pass

        monkeypatch.setattr(
            client._session,
            "post",
            lambda *a, **kw: seen.append(a) or FakeResponse(),
        )
        client._trading_call(call_name="GetItem", body="<x/>", site_id="0", compatibility_level="1")
        assert len(seen) == 1

    def test_no_silent_retries_on_mutating_calls(self, monkeypatch):
        # A replayed ReviseFixedPriceItem/EndItem is worse than a visible error.
        client = _client(monkeypatch)
        adapter = client._session.get_adapter("https://api.ebay.com/ws/api.dll")
        assert adapter.max_retries.total == 0


class TestGetItemDetailsBulk:
    def test_results_keep_the_requested_order(self, monkeypatch):
        client = _client(monkeypatch)
        monkeypatch.setattr(
            eBayLegacyClient,
            "get_item_details",
            lambda self, item_id, **kw: {"item_id": item_id},
        )

        ids = [str(i) for i in range(50)]
        details, failures = client.get_item_details_bulk(ids, max_workers=8)

        assert [d["item_id"] for d in details] == ids
        assert failures == {}

    def test_a_failed_item_is_collected_not_raised(self, monkeypatch):
        client = _client(monkeypatch)

        def flaky(self, item_id, **kw):
            if item_id == "2":
                raise RuntimeError("GetItem exploded")
            return {"item_id": item_id}

        monkeypatch.setattr(eBayLegacyClient, "get_item_details", flaky)
        details, failures = client.get_item_details_bulk(["1", "2", "3"], max_workers=4)

        assert [d["item_id"] for d in details] == ["1", "3"]
        assert "GetItem exploded" in failures["2"]

    def test_calls_actually_overlap(self, monkeypatch):
        client = _client(monkeypatch)
        barrier = threading.Barrier(4, timeout=5)

        def blocking(self, item_id, **kw):
            # Deadlocks unless four calls are genuinely in flight together.
            barrier.wait()
            return {"item_id": item_id}

        monkeypatch.setattr(eBayLegacyClient, "get_item_details", blocking)
        details, failures = client.get_item_details_bulk(["1", "2", "3", "4"], max_workers=4)
        assert len(details) == 4

    def test_workers_never_exceed_the_connection_pool(self, monkeypatch):
        client = _client(monkeypatch)
        peak = 0
        live = 0
        lock = threading.Lock()

        def counting(self, item_id, **kw):
            nonlocal peak, live
            with lock:
                live += 1
                peak = max(peak, live)
            try:
                return {"item_id": item_id}
            finally:
                with lock:
                    live -= 1

        monkeypatch.setattr(eBayLegacyClient, "get_item_details", counting)
        client.get_item_details_bulk([str(i) for i in range(80)], max_workers=1000)
        assert peak <= eBayLegacyClient.pool_size

    def test_progress_is_reported(self, monkeypatch):
        client = _client(monkeypatch)
        monkeypatch.setattr(
            eBayLegacyClient, "get_item_details", lambda self, item_id, **kw: {"item_id": item_id}
        )
        seen = []
        client.get_item_details_bulk(
            ["1", "2", "3"], max_workers=2, on_progress=lambda d, t: seen.append((d, t))
        )
        assert [d for d, _ in seen] == [1, 2, 3]
        assert {t for _, t in seen} == {3}

    def test_empty_input_makes_no_calls(self, monkeypatch):
        client = _client(monkeypatch)

        def explode(self, item_id, **kw):
            raise AssertionError("should not be called")

        monkeypatch.setattr(eBayLegacyClient, "get_item_details", explode)
        assert client.get_item_details_bulk([]) == ([], {})


class TestPartialSnapshotGuard:
    def test_too_many_failures_aborts_before_writing(self, monkeypatch):
        from shoebox.pipelines import sync_active_listing_details as mod

        listings = [{"item_id": str(i), "title": f"card {i}"} for i in range(20)]
        # 5 of 20 fail = 25%, over the 10% ceiling.
        failures = {str(i): "boom" for i in range(5)}
        details = [{"item_id": str(i), "start_time": "2026-01-01T00:00:00Z"} for i in range(5, 20)]

        class FakeLegacy:
            def get_active_listings(self):
                return listings

            def get_item_details_bulk(self, item_ids, **kw):
                return details, failures

        class FakeEbay:
            legacy_api = FakeLegacy()

        wrote = []
        monkeypatch.setattr(mod, "EbayClient", lambda: FakeEbay())
        monkeypatch.setattr(mod, "GCSClient", lambda: None)
        monkeypatch.setattr(mod, "BigQueryClient", lambda: None)
        monkeypatch.setattr(mod, "notify_best_effort", lambda *a, **kw: None)
        monkeypatch.setattr(mod, "write_jsonl", lambda *a, **kw: wrote.append(a))

        with pytest.raises(RuntimeError, match="refusing to write a partial snapshot"):
            mod.sync_active_listing_details()
        assert wrote == []

    def test_variation_listings_are_not_fetched(self, monkeypatch):
        from shoebox.pipelines import sync_active_listing_details as mod

        listings = [
            {"item_id": "1", "title": "2025 Topps - Jones #12"},
            {"item_id": "2", "title": "Complete Your Set - 2025 Topps Series 1"},
        ]
        asked = {}

        class FakeLegacy:
            def get_active_listings(self):
                return listings

            def get_item_details_bulk(self, item_ids, **kw):
                asked["ids"] = item_ids
                raise SystemExit  # stop before the GCS/BigQuery leg

        class FakeEbay:
            legacy_api = FakeLegacy()

        monkeypatch.setattr(mod, "EbayClient", lambda: FakeEbay())
        monkeypatch.setattr(mod, "GCSClient", lambda: None)
        monkeypatch.setattr(mod, "BigQueryClient", lambda: None)
        monkeypatch.setattr(mod, "notify_best_effort", lambda *a, **kw: None)

        with pytest.raises(SystemExit):
            mod.sync_active_listing_details()
        assert asked["ids"] == ["1"]


class TestQuotaExhaustion:
    """Error 518 is a daily allowance, so the only sane response is to stop."""

    def test_quota_error_is_raised_as_its_own_type(self, monkeypatch):
        from shoebox.clients.ebay_legacy import TradingQuotaExceeded

        client = _client(monkeypatch)

        class FakeResponse:
            text = """<GetItemResponse><Ack>Failure</Ack><Errors>
                <ErrorCode>518</ErrorCode><SeverityCode>Error</SeverityCode>
                <ShortMessage>Your application has exceeded usage limit on this call</ShortMessage>
                </Errors></GetItemResponse>"""

            def raise_for_status(self):
                pass

        monkeypatch.setattr(client._session, "post", lambda *a, **kw: FakeResponse())
        with pytest.raises(TradingQuotaExceeded, match="daily call allowance"):
            client._trading_call(
                call_name="GetItem", body="<x/>", site_id="0", compatibility_level="1"
            )

    def test_other_failures_stay_ordinary_errors(self, monkeypatch):
        from shoebox.clients.ebay_legacy import TradingQuotaExceeded

        client = _client(monkeypatch)

        class FakeResponse:
            text = """<GetItemResponse><Ack>Failure</Ack><Errors>
                <ErrorCode>17</ErrorCode><ShortMessage>Item not found</ShortMessage>
                </Errors></GetItemResponse>"""

            def raise_for_status(self):
                pass

        monkeypatch.setattr(client._session, "post", lambda *a, **kw: FakeResponse())
        with pytest.raises(RuntimeError) as excinfo:
            client._trading_call(
                call_name="GetItem", body="<x/>", site_id="0", compatibility_level="1"
            )
        assert not isinstance(excinfo.value, TradingQuotaExceeded)

    def test_the_sweep_stops_instead_of_hammering_the_wall(self, monkeypatch):
        from shoebox.clients.ebay_legacy import TradingQuotaExceeded

        client = _client(monkeypatch)
        attempted = []

        def quota_after_10(self, item_id, **kw):
            attempted.append(item_id)
            if len(attempted) > 10:
                raise TradingQuotaExceeded("allowance gone")
            return {"item_id": item_id}

        monkeypatch.setattr(eBayLegacyClient, "get_item_details", quota_after_10)

        with pytest.raises(TradingQuotaExceeded, match="Fetched"):
            client.get_item_details_bulk([str(i) for i in range(500)], max_workers=4)

        # Nowhere near all 500 -- the remaining work was cancelled.
        assert len(attempted) < 100
