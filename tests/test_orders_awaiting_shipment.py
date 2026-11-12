"""orders-awaiting-shipment with nothing to ship (the Slack Home-tab button's quiet days)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from shoebox.services import orders_awaiting_shipment as oas


class FakeFulfillment:
    def __init__(self, orders):
        self.orders = orders

    def get_orders(self, **kwargs):
        return self.orders


@pytest.fixture
def no_orders(monkeypatch):
    def client():
        return SimpleNamespace(fulfillment=FakeFulfillment([]))

    sent = []
    monkeypatch.setattr(oas, "EbayClient", client)
    monkeypatch.setattr(oas, "notify", lambda **kw: sent.append(kw))
    monkeypatch.setattr(
        oas, "get_settings", lambda: SimpleNamespace(slack=SimpleNamespace(notify_channel="C1"))
    )
    return sent


def test_get_orders_is_empty_without_line_items(no_orders):
    assert oas.get_orders().empty


def test_message_says_nothing_to_ship(no_orders):
    oas.display_orders(pull_order=True, buyer_order=False, display=False, message=True)
    assert no_orders == [
        {"channel": "C1", "message": "📦 *Orders Awaiting Shipment* — nothing to ship"}
    ]


def test_display_only_posts_nothing(no_orders, capsys):
    oas.display_orders(display=True, message=False)
    assert no_orders == []
    assert "No orders awaiting shipment" in capsys.readouterr().out
