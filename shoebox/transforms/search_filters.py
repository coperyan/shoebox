"""Translate a ``ResolvedSearch`` into eBay Browse query parameters.

The split between server-side and client-side filtering is the crux of this
module. eBay caps a result set at ``max_results`` items *before* we see them, so
anything filtered in Python consumes result slots: a search fetching 200 items
with a post-filter that rejects 90% yields 20 usable listings. Push everything
eBay supports server-side.

The unavoidable exception is title exclusion. Browse has no negative-keyword
support at all — ``q`` is a positive match only — so "not a reprint, not a lot"
can only happen here.

Pure functions: no I/O, no clients, no settings.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

from ..models.ebay.item_summary import ItemSummary
from ..models.saved_search import ResolvedSearch

logger = logging.getLogger(__name__)

_OR_GROUP_RE = re.compile(r"\(([^)]*)\)")
_QUOTED_PHRASE_RE = re.compile(r'"([^"]*)"')


def query_terms(query: str) -> list[list[str]]:
    """Parse an eBay ``q`` string into AND-groups of acceptable alternatives.

    eBay's grammar: space-separated terms are ANDed, ``(a, b)`` is an OR group,
    ``"a b"`` is an exact phrase. ``'lincecum (auto, autograph)'`` parses to
    ``[['auto', 'autograph'], ['lincecum']]`` — every group must match, a group
    matches when any alternative does. Lowercased for the substring checks.
    """
    groups: list[list[str]] = []

    def _grab_group(match: re.Match[str]) -> str:
        alternatives = [a.strip().strip('"').lower() for a in match.group(1).split(",")]
        alternatives = [a for a in alternatives if a]
        if alternatives:
            groups.append(alternatives)
        return " "

    def _grab_phrase(match: re.Match[str]) -> str:
        phrase = match.group(1).strip().lower()
        if phrase:
            groups.append([phrase])
        return " "

    rest = _OR_GROUP_RE.sub(_grab_group, query)
    rest = _QUOTED_PHRASE_RE.sub(_grab_phrase, rest)
    groups.extend([term.strip('"').lower()] for term in rest.split() if term.strip('"'))
    return groups


def _set_clause(field: str, values: list[str]) -> str:
    """eBay set syntax: ``field:{A|B}``. Values are sorted for determinism."""
    return f"{field}:{{{'|'.join(sorted(values))}}}"


def build_browse_filter(search: ResolvedSearch) -> str | None:
    """Build the Browse ``filter`` string, or None when nothing is filtered.

    Returns None rather than "" because BrowseClient tests the value for
    truthiness before forwarding it.
    """
    clauses: list[str] = []

    if search.price is not None:
        low = "" if search.price.min is None else _fmt_decimal(search.price.min)
        high = "" if search.price.max is None else _fmt_decimal(search.price.max)
        clauses.append(f"price:[{low}..{high}]")
        # eBay rejects a price filter without a currency outright, so this is
        # injected rather than left to the config author to remember.
        clauses.append(f"priceCurrency:{search.currency}")

    # Always emitted: without it eBay silently returns FIXED_PRICE only, so an
    # auction watcher would just never fire. The model forbids an empty list.
    clauses.append(_set_clause("buyingOptions", list(search.buying_options)))

    if search.conditions:
        clauses.append(_set_clause("conditions", list(search.conditions)))

    # itemLocationCountry takes a single value; multi-country falls back to
    # passes_post_filters.
    if len(search.item_location_countries) == 1:
        clauses.append(f"itemLocationCountry:{search.item_location_countries[0]}")

    if search.delivery_country:
        clauses.append(f"deliveryCountry:{search.delivery_country}")

    if search.sellers:
        clauses.append(_set_clause("sellers", search.sellers))
    if search.exclude_sellers:
        clauses.append(_set_clause("excludeSellers", search.exclude_sellers))

    if search.free_shipping_only:
        clauses.append("maxDeliveryCost:0")

    return ",".join(clauses) if clauses else None


def build_aspect_filter(search: ResolvedSearch) -> str | None:
    """Build the ``aspect_filter`` string.

    eBay requires the category ID both as the ``category_ids`` URI parameter and
    again as the first element here, and the two must match. The model already
    guarantees exactly one category when aspects are present.
    """
    if not search.aspects:
        return None

    parts = [f"categoryId:{search.category_ids[0]}"]
    for key in sorted(search.aspects):
        values = [_escape_aspect_value(v) for v in search.aspects[key]]
        parts.append(f"{key}:{{{'|'.join(values)}}}")
    return ",".join(parts)


def _escape_aspect_value(value: str) -> str:
    """``|`` delimits aspect values, so a literal one must be escaped."""
    return value.replace("|", "\\|")


def _fmt_decimal(value: Decimal) -> str:
    """Render a price without scientific notation or a trailing ``.00``."""
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        normalized = normalized.quantize(Decimal(1))
    return format(normalized, "f")


def cheapest_shipping(item: ItemSummary) -> Decimal | None:
    """Lowest shipping cost across options, or None if none are quoted."""
    costs = [
        opt.shipping_cost.decimal
        for opt in item.shipping_options
        if opt.shipping_cost and opt.shipping_cost.decimal is not None
    ]
    return min(costs) if costs else None


def rejection_reason(item: ItemSummary, search: ResolvedSearch) -> str | None:
    """Why this listing fails the post-filters, or None if it passes.

    Returns a human-readable reason naming the offending config key and value,
    so ``preview-search`` can answer "why isn't this in my results?" instead of
    just dropping the row. First failing filter wins.
    """
    title = (item.title or "").lower()

    # eBay pads a thin result set with looser matches that don't contain every
    # query term ("results matching fewer words") -- without this re-check, a
    # narrow search intermittently floods the channel with listings that match
    # only part of the query. Substring matching on purpose: 'auto' should
    # accept 'Autograph'.
    if search.require_query_in_title and search.query:
        for alternatives in query_terms(search.query):
            if not any(alt in title for alt in alternatives):
                wanted = " or ".join(repr(a) for a in alternatives)
                return f"require_query_in_title: title missing {wanted}"

    # Browse has no negative keywords -- this is the one filter that can only
    # live here.
    for term in search.title_exclude:
        if term.lower() in title:
            return f"title_exclude: {term!r}"

    missing = [t for t in search.title_must_include_all if t.lower() not in title]
    if missing:
        return f"title_must_include_all: missing {', '.join(repr(m) for m in missing)}"

    if search.title_must_include_any and not any(
        term.lower() in title for term in search.title_must_include_any
    ):
        return "title_must_include_any: matched none"

    if search.seller_min_feedback_score is not None:
        score = item.seller.feedback_score if item.seller else None
        # Unknown feedback fails a threshold the user explicitly asked for.
        if score is None:
            return "seller_min_feedback_score: seller feedback unknown"
        if score < search.seller_min_feedback_score:
            return f"seller_min_feedback_score: {score} < {search.seller_min_feedback_score}"

    # Only post-filtered when the server-side single-value form couldn't be used.
    if len(search.item_location_countries) > 1:
        country = item.item_location.country if item.item_location else None
        if country not in set(search.item_location_countries):
            return f"item_location_countries: {country or 'unknown'} not allowed"

    if search.max_total_price is not None:
        price = item.price_decimal
        if price is None:
            return "max_total_price: price unknown"
        total = price + (cheapest_shipping(item) or Decimal(0))
        if total > search.max_total_price:
            return f"max_total_price: {total} > {search.max_total_price}"

    return None


def passes_post_filters(item: ItemSummary, search: ResolvedSearch) -> bool:
    """Apply the filters eBay has no server-side equivalent for."""
    return rejection_reason(item, search) is None


def filter_items(items: list[ItemSummary], search: ResolvedSearch) -> list[ItemSummary]:
    """Apply post-filters, logging how many result slots they cost."""
    kept = [item for item in items if passes_post_filters(item, search)]
    dropped = len(items) - len(kept)
    if dropped:
        logger.info("%s: post-filters dropped %d/%d results", search.name, dropped, len(items))
    return kept
