from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from ...models.ebay.item_summary import ItemSummary
from .session import EbaySession

logger = logging.getLogger(__name__)

# Default cap on total items returned by a single search() call. eBay fills
# pages with up to 200 records, so this is one page's worth.
_DEFAULT_LIMIT = 200
# eBay returns at most 10,000 items for any one result set.
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
            limit: Default cap on total items returned, used when ``max_results``
                is not given. This is *not* a page size — ebay_rest fills pages
                with up to 200 records regardless (see note below).
            max_results: Cap total items returned, overriding ``limit``.
                Capped at 10,000 (the API maximum for one result set).

        Returns:
            List of ItemSummary models.

        Note:
            ebay_rest co-opts the underlying ``limit`` parameter as
            "records desired in total" rather than the page size eBay's own API
            documents (``a_p_i_private.py``: ``records_desired = kwargs["limit"]``,
            and the generator stops once it hits zero). Page size is chosen
            internally as ``min(records_desired, 200)``. So the total cap is what
            must be passed down; sending a page size instead silently truncates
            every search at 200 items.
        """
        if not q and not category_ids:
            raise ValueError("At least one of 'q' or 'category_ids' must be provided.")

        cap = min(max_results if max_results is not None else limit, _MAX_RESULTS)
        results: list[ItemSummary] = []

        for item in self._paginate(
            q=q,
            category_ids=category_ids,
            filter=filter,
            sort=sort,
            aspect_filter=aspect_filter,
            total_limit=cap,
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
        total_limit: int,
    ) -> Iterator[ItemSummary]:
        kwargs: dict[str, Any] = {"limit": total_limit}
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

    def aspect_refinements(
        self,
        *,
        q: str | None = None,
        category_ids: str | None = None,
        filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """List the aspects available to filter this search on, with counts.

        Returns eBay's ``aspectDistributions``: one entry per aspect
        (``localized_aspect_name``) holding its possible values and how many
        listings carry each.

        This deliberately bypasses ``api.buy_browse_search``. That wrapper is a
        paginating generator whose ``page_controls`` list contains
        ``"refinement"``, so it discards the refinement payload entirely and no
        amount of ``fieldgroups`` will bring it back. ``_method_single`` is the
        SDK's own single-response path, wired with the same arguments its
        ``buy_browse_search`` uses.

        One API call. ``limit=1`` because the item summaries are not wanted —
        only the refinements attached to the response.
        """
        from ebay_rest.api import buy_browse
        from ebay_rest.error import Error as BuyBrowseError

        kwargs: dict[str, Any] = {"fieldgroups": "ASPECT_REFINEMENTS", "limit": 1}
        if q:
            kwargs["q"] = q
        if category_ids:
            kwargs["category_ids"] = category_ids
        if filter:
            kwargs["filter"] = filter

        response = self.api._method_single(
            buy_browse.Configuration,
            "/buy/browse/v1",
            buy_browse.ItemSummaryApi,
            buy_browse.ApiClient,
            "search",
            BuyBrowseError,
            False,
            ["buy.browse", "item_summary"],
            None,
            **kwargs,
        )

        refinement = (response or {}).get("refinement") or {}
        aspects = refinement.get("aspect_distributions") or []
        logger.info("Browse returned %d aspect(s) for refinement", len(aspects))
        return aspects

    def get_item(self, item_id: str) -> dict[str, Any]:
        """Fetch a single item by its Browse API item ID."""
        return self.api.buy_browse_get_item(item_id=item_id)

    def get_item_by_legacy_id(self, legacy_item_id: str) -> dict[str, Any]:
        """Fetch a single item by its legacy listing ID."""
        return self.api.buy_browse_get_item_by_legacy_id(legacy_item_id=legacy_item_id)
