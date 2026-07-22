import logging

from shoebox.clients.ebay_rest.client import EbayClient
from shoebox.models.ebay.negotiation_offer import (
    NegotiationOffer,
)
from shoebox.utils.pricing import calculate_new_price

logger = logging.getLogger(__name__)


def main(dry_run: bool = False, max_price: float = 19.99):
    """Send discount offers to watchers of eligible listings priced <= max_price."""
    ebay_api = EbayClient()

    eligible_listings = ebay_api.negotiation.find_eligible_items()
    details = []
    for listing in eligible_listings:
        resp = ebay_api.legacy_api.get_item_details(item_id=listing["listing_id"])
        details.append(resp)

    negos: list[NegotiationOffer] = []
    for d in details:
        if float(d["price"]) > max_price:
            continue
        offer = calculate_new_price(float(d["price"]))

        negos.append(
            NegotiationOffer.from_api(
                listing_id=d["item_id"], price=offer, quantity=int(d["quantity"])
            )
        )

    logger.info("%d eligible listing(s), %d offer(s) to send", len(details), len(negos))

    if dry_run:
        for nego in negos:
            logger.info("[dry_run] Would send offer: %s", nego)
        return

    for nego in negos:
        ebay_api.negotiation.send_offer(nego)
    logger.info("Sent %d offer(s)", len(negos))
