from shoebox.clients.ebay.client import EbayClient
from shoebox.settings import get_settings
from shoebox.utils.slack import notify


def end_oos_listings():
    ebay_api = EbayClient()
    oos = ebay_api.trading.get_out_of_stock_listings()
    ebay_api.trading.end_listings([x["item_id"] for x in oos])
    notify(get_settings().slack.notify_channel, f"Ended {len(oos)} OOS listings(s)..")
