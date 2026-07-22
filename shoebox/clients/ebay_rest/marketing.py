from .session import EbaySession


class MarketingClient:
    def __init__(self, session: EbaySession):
        self.api = session.api
