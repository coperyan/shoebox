from shoebox.clients.ebay_rest.session import EbaySession
from shoebox.models.ebay.negotiation_offer import NegotiationOffer


class NegotiationClient:
    def __init__(self, session: EbaySession):
        self.api = session.api
        self.legacy_api = session.legacy_api

    def find_eligible_items(self) -> list[str]:
        resp = self.api.sell_negotiation_find_eligible_items(x_ebay_c_marketplace_id="EBAY_US")
        return [x["record"] for x in resp if "record" in x]

    def send_offer(self, offer: NegotiationOffer) -> None:
        resp = self.api.sell_negotiation_send_offer_to_interested_buyers(
            x_ebay_c_marketplace_id="EBAY_US",
            content_type="application/json",
            body=offer.to_json,
        )
        return [x["record"] for x in resp if "record" in x]


# tmp = NegotiationClient()
# test = negotiation_client.api.sell_negotiation_find_eligible_items(
#             x_ebay_c_marketplace_id="EBAY_US"
#         )
# test = [x["record"] for x in test if "record" in x]
