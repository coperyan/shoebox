"""Error types for the eBay clients, and the eBay error ids they react to.

REST calls made through the session's :class:`~.session.RestApi` raise
:class:`EbayApiError` rather than the SDK's own ``ebay_rest.Error``, with eBay's
``errorId`` parsed out so callers branch on a number instead of a string.
"""

from __future__ import annotations

import json
from typing import Any

# --- eBay errorId values the clients handle specially -------------------------
# Sell Inventory
INVENTORY_TRANSIENT_ERROR = 25001  # "Core Inventory Service internal error" (HTTP 500); retryable
OFFER_NOT_FOUND_ERROR = 25713  # getOffers: "This Offer is not available" -- SKU has no offer yet
# Sell Marketing
AD_ALREADY_EXISTS_ERROR = 35036  # the listing is already promoted in this campaign
LISTING_NOT_VISIBLE_ERROR = 38227  # a just-published listing is not yet visible to marketing
# Any Sell API: the token was not granted the scope (HTTP 403, domain ACCESS)
INSUFFICIENT_SCOPE_ERROR = 1100
# Trading API: the daily call allowance for this call is used up
TRADING_QUOTA_ERROR_CODE = "518"


class EbayClientError(RuntimeError):
    """The client was misused, or eBay answered with a shape it cannot act on.

    Distinct from :class:`EbayApiError`: no call failed, the code path just
    cannot continue (two offers for one SKU, a publish response with no offer
    id, an unknown ``existing_offer_action``).
    """


class EbayApiError(RuntimeError):
    """An eBay REST call failed.

    ``error_id`` is eBay's own ``errorId`` from the first entry of the
    response's ``errors`` array, or ``None`` when the failure happened before
    eBay answered (a token problem inside the SDK, a bad parameter). ``number``
    and ``reason`` are the SDK's classification; ``errors`` is the full array.
    """

    def __init__(
        self,
        message: str,
        *,
        error_id: int | None = None,
        number: int | None = None,
        reason: str | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_id = error_id
        self.number = number
        self.reason = reason
        self.errors = errors or []

    @classmethod
    def from_rest_error(cls, e: Any) -> EbayApiError:
        """Build from an ``ebay_rest.Error`` (duck-typed: ``number``/``reason``/``detail``)."""
        errors = _parse_errors(getattr(e, "detail", None))
        first = errors[0] if errors else {}
        return cls(
            str(e),
            error_id=first.get("errorId"),
            number=getattr(e, "number", None),
            reason=getattr(e, "reason", None),
            errors=errors,
        )


def _parse_errors(detail: Any) -> list[dict[str, Any]]:
    """Pull eBay's ``errors`` array out of the SDK's ``detail`` payload, if it is one."""
    if not detail:
        return []
    data = detail
    if not isinstance(data, dict):
        try:
            data = json.loads(detail)
        except (TypeError, ValueError):
            return []
    if not isinstance(data, dict):
        return []
    errors = data.get("errors")
    if not isinstance(errors, list):
        return []
    return [x for x in errors if isinstance(x, dict)]


class TradingQuotaExceeded(RuntimeError):
    """eBay's daily call allowance for this Trading call is used up.

    Distinct from an ordinary failure because waiting is the only remedy: no
    amount of retrying, backing off, or reducing concurrency helps until the
    allowance resets at midnight Pacific.
    """
