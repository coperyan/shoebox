from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from ...models.ebay.item_summary import ItemSummary
from .session import EbaySession

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 200
_MAX_RESULTS = 10_000


class BrowseClient:
    def __init__(self, session: EbaySession):
        self.api = session.api

    def search(
        self,
        *,
        q: str | None = None,
        category_ids: str | None = None,
        filter: str | None = None,
        sort: str | None = None,
        aspect_filter: str | None = None,
        limit: int = _DEFAULT_LIMIT,
        max_results: int | None = None,
    ) -> list[ItemSummary]:
        """
        Search eBay listings via the Browse API.

        Args:
            q: Keyword query string.
            category_ids: Comma-separated category ID(s) to restrict results.
            filter: eBay filter string, e.g. "price:[10..50],priceCurrency:USD".
            sort: Sort order — e.g. "price", "-price", "newlyListed", "bestMatch".
            aspect_filter: Aspect filter string, e.g. "categoryId:212,Graded:{Yes}".
            limit: Page size per API call (max 200).
            max_results: Cap total items returned. Defaults to 10,000 (API max).

        Returns:
            List of ItemSummary models.
        """
        if not q and not category_ids:
            raise ValueError("At least one of 'q' or 'category_ids' must be provided.")

        cap = min(max_results or _MAX_RESULTS, _MAX_RESULTS)
        results: list[ItemSummary] = []

        for item in self._paginate(
            q=q,
            category_ids=category_ids,
            filter=filter,
            sort=sort,
            aspect_filter=aspect_filter,
            page_limit=min(limit, _DEFAULT_LIMIT),
        ):
            results.append(item)
            if len(results) >= cap:
                break

        logger.info("Browse search returned %d items (cap=%d)", len(results), cap)
        return results

    def _paginate(
        self,
        *,
        q: str | None,
        category_ids: str | None,
        filter: str | None,
        sort: str | None,
        aspect_filter: str | None,
        page_limit: int,
    ) -> Iterator[ItemSummary]:
        kwargs: dict[str, Any] = {"limit": page_limit}
        if q:
            kwargs["q"] = q
        if category_ids:
            kwargs["category_ids"] = category_ids
        if filter:
            kwargs["filter"] = filter
        if sort:
            kwargs["sort"] = sort
        if aspect_filter:
            kwargs["aspect_filter"] = aspect_filter

        for raw in self.api.buy_browse_search(**kwargs):
            if "record" in raw:
                yield ItemSummary.from_api(raw["record"])

    def get_item(self, item_id: str) -> dict[str, Any]:
        """Fetch a single item by its Browse API item ID."""
        return self.api.buy_browse_get_item(item_id=item_id)

    def get_item_by_legacy_id(self, legacy_item_id: str) -> dict[str, Any]:
        """Fetch a single item by its legacy listing ID."""
        return self.api.buy_browse_get_item_by_legacy_id(legacy_item_id=legacy_item_id)
