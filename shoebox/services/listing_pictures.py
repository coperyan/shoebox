"""Keep a live listing's pictures when its inventory item is rewritten.

``createOrReplaceInventoryItem`` replaces the whole item, pictures included,
so every round-trip has to send the full picture list back. The inventory
item's own ``imageUrls`` can't supply it: eBay's ``getInventoryItem`` often
returns only the front for a listing that shows a front and a back, and
sending that back strips the back from the live listing. The live listing
(Trading ``GetItem``) is the source of truth; its pictures are copied
full-size into GCS and those copies are what go on the item.
"""

from __future__ import annotations

import requests

from shoebox.clients.ebay.client import EbayClient
from shoebox.clients.ebay.errors import EbayClientError
from shoebox.clients.gcs import GCSClient

# GCS names for a listing's pictures, by position.
_PICTURE_SIDES = ("front", "back", "combined")


def ebay_image_to_gcs(
    picture_urls: list[str], sku: str, gcs_client: GCSClient, settings
) -> list[str]:
    picture_urls = [p.replace("$_1", "$_57") for p in picture_urls]
    bucket = gcs_client.client.bucket(settings.gcs.image_bucket)
    gcs_urls = []
    for i, url in enumerate(picture_urls):
        # Name by position: listings can carry a third (combined) picture, and
        # sharing the "back" name would overwrite the back with it.
        side = _PICTURE_SIDES[i] if i < len(_PICTURE_SIDES) else f"img{i + 1}"
        resp = requests.get(url)
        tmp_img = resp.content
        blob_path = f"other/{sku}_{side}.png"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(tmp_img, content_type="image/png")
        gcs_urls.append(blob.public_url)
    return gcs_urls


def live_picture_urls(ebay: EbayClient, item_id: str) -> list[str]:
    """The pictures the live listing actually shows, in order."""
    return ebay.trading.get_item_details(item_id=item_id).get("picture_urls") or []


def rehost_live_pictures(
    ebay: EbayClient, *, item_id: str, sku: str, gcs: GCSClient, settings
) -> list[str]:
    """GCS copies of the live listing's pictures, ready to put on its item."""
    pictures = live_picture_urls(ebay, item_id)
    if not pictures:
        raise EbayClientError(f"item_id={item_id} shows no pictures; refusing to rewrite its item")
    return ebay_image_to_gcs(pictures, sku, gcs, settings)
