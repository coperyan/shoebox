from ...models.ebay.inventory_item import InventoryItem
from ...models.ebay.offer import Offer
from .session import EbaySession


class InventoryClient:
    def __init__(self, session: EbaySession):
        self.api = session.api

    def get_inventory_item(self, sku: str) -> InventoryItem:
        resp = self.api.sell_inventory_get_inventory_item(sku=sku)
        return InventoryItem.from_api(resp)

    def get_inventory_items(self) -> list[InventoryItem]:
        resp = self.api.sell_inventory_get_inventory_items()
        return [InventoryItem.from_api(r["record"]) for r in resp if "record" in r]

    def get_bulk_inventory_item(self, skus: list) -> list[InventoryItem]:
        chunksize = 25
        results = []
        for i in range(0, len(skus), chunksize):
            iter_skus = skus[i : i + chunksize]
            resp = self.api.sell_inventory_bulk_get_inventory_item(
                body={"requests": [{"sku": s} for s in iter_skus]}
            )
            results.extend(
                [x["inventory_item"] for x in resp["responses"] if "inventory_item" in x]
            )
        return [InventoryItem.from_api(r) for r in results]

    def get_sku_offers(sku: str) -> list[Offer]:
        return None

    def get_offer(offer_id: str) -> Offer:
        return None

    def update_offer(offer: Offer) -> None:
        return None

    def create_offer(offer: Offer) -> None:
        return None

    def publish_offer(offer_id: str) -> None:
        return None
