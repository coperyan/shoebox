from __future__ import annotations

import functools

from shoebox.settings import Settings

from .analytics import AnalyticsClient
from .browse import BrowseClient
from .fulfillment import FulfillmentClient
from .inventory import InventoryClient
from .marketing import MarketingClient
from .negotiation import NegotiationClient
from .session import EbaySession, build_session
from .stores import StoresClient
from .trading import TradingClient


class EbayClient:
    """
    One object per eBay seller account, holding a sub-client per eBay API.

    This is a composition root, not a place for behavior: each attribute wraps
    one API (``inventory``, ``marketing``, ``stores``, ``browse``,
    ``fulfillment``, ``analytics``, ``negotiation``, and the XML ``trading``
    API), and workflows that span several of them live in ``shoebox.services``.

    ``api`` is the underlying ``ebay_rest`` object with its errors translated
    to ``EbayApiError``. It is an escape hatch for one-off scripts that need a
    call no sub-client wraps yet; pipelines should not use it.
    """

    def __init__(self, settings: Settings | None = None):
        self.session: EbaySession = build_session(settings)
        self.settings = self.session.settings
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
        return TradingClient(token_path=self.settings.ebay.trading_token_path)


_default: EbayClient | None = None


def get_client(settings: Settings | None = None) -> EbayClient:
    global _default
    if _default is None:
        _default = EbayClient(settings=settings)
    return _default
