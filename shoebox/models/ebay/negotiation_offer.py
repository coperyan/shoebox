## Response: https://developer.ebay.com/api-docs/sell/negotiation/types/api:Offer
## Request: https://developer.ebay.com/api-docs/sell/negotiation/types/api:CreateOffersRequest

from pydantic import Field

from shoebox.settings import get_settings

from ..common import Model


def _default_offer_message() -> str:
    return get_settings().store.offer_message


class NegotiationOffer(Model):
    listing_id: str
    price: float
    quantity: int

    allow_counter_offer: bool = False
    message: str = Field(default_factory=_default_offer_message)
    duration_unit: str = "DAY"
    duration_value: int = 1
    currency: str = "USD"

    @property
    def offer_duration(self) -> dict:
        return {"unit": self.duration_unit, "value": self.duration_value}

    @property
    def amount(self) -> dict:
        return {"currency": self.currency, "value": str(self.price)}

    @property
    def offered_item(self) -> list[dict]:
        return [
            {
                "listingId": self.listing_id,
                "price": self.amount,
                "quantity": self.quantity,
            }
        ]

    @property
    def to_json(self) -> dict:
        return {
            "allowCounterOffer": self.allow_counter_offer,
            "message": self.message,
            # "offerDuration": self.offer_duration,
            "offeredItems": self.offered_item,
        }

    @classmethod
    def from_api(cls, **kwargs) -> "NegotiationOffer":
        # Preserve raw for debugging/auditing
        return cls.model_validate({**kwargs})


class NegotationOfferResponse:
    pass
