"""Slack message builders for saved-search hits.

Deliberately *not* using ``slack_formatting.table``: it renders inside a fenced
code block, and Slack neither linkifies nor unfurls URLs there — which would
defeat the entire point of an alert you want to click through.

Each item reply ends with a bare URL on its own line so Slack can attach a
preview (requires ``unfurl_links=True`` at the call site).

Pure functions: no I/O.
"""

from __future__ import annotations

from decimal import Decimal

from ..models.ebay.item_summary import ItemSummary
from ..models.saved_search import ResolvedSearch
from ..transforms.search_filters import cheapest_shipping

# Slack renders long titles poorly in a thread; eBay titles run to 80 chars.
MAX_TITLE_CHARS = 90


def truncate_title(title: str, limit: int = MAX_TITLE_CHARS) -> str:
    if len(title) <= limit:
        return title
    return title[: limit - 1].rstrip() + "…"


def _escape_mrkdwn_link_text(text: str) -> str:
    """``<`` and ``>`` would terminate the link syntax early."""
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _money(value: Decimal | None, currency: str) -> str:
    if value is None:
        return "price unknown"
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"{symbol}{value:,.2f}"


def format_interval(delta) -> str:
    """Render a timedelta the way it was written in YAML (``15m``, ``2h``)."""
    seconds = int(delta.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0 and seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def format_parent(search: ResolvedSearch, new_count: int) -> str:
    """The in-channel header. Items land in its thread."""
    plural = "" if new_count == 1 else "s"
    bits: list[str] = []
    if search.price is not None:
        low = _money(search.price.min, search.currency) if search.price.min else ""
        high = _money(search.price.max, search.currency) if search.price.max else ""
        if low and high:
            bits.append(f"{low}–{high}")
        elif high:
            bits.append(f"under {high}")
        elif low:
            bits.append(f"over {low}")
    if search.buying_options == ["FIXED_PRICE"]:
        bits.append("Buy It Now")
    elif search.buying_options == ["AUCTION"]:
        bits.append("Auction")
    bits.append(f"every {format_interval(search.interval)}")

    return f"*🔎 {search.name}* — {new_count} new listing{plural}\n_{' · '.join(bits)}_"


def format_item(item: ItemSummary, search: ResolvedSearch) -> str:
    """One thread reply per listing."""
    title = truncate_title(_escape_mrkdwn_link_text(item.title or "(untitled)"))
    url = item.item_web_url or ""

    headline = f"*<{url}|{title}>*" if url else f"*{title}*"

    price_bits: list[str] = []
    if item.is_auction and item.current_bid_decimal is not None:
        price_bits.append(f"{_money(item.current_bid_decimal, search.currency)} (bid)")
    else:
        price_bits.append(_money(item.price_decimal, search.currency))

    price_bits.append("Auction" if item.is_auction else "Buy It Now")

    shipping = cheapest_shipping(item)
    if item.free_shipping:
        price_bits.append("Free shipping")
    elif shipping is not None:
        price_bits.append(f"+{_money(shipping, search.currency)} ship")

    meta: list[str] = []
    if item.condition:
        meta.append(item.condition)
    if item.seller and item.seller.username:
        score = item.seller.feedback_score
        seller = f"`{item.seller.username}`"
        if score is not None:
            seller += f" ({score:,})"
        meta.append(f"seller {seller}")

    lines = [headline, " · ".join(price_bits)]
    if meta:
        lines.append(" · ".join(meta))
    if url:
        # Bare, on its own line, so Slack can unfurl a preview.
        lines.append(url)
    return "\n".join(lines)


def format_overflow(total_new: int, shown: int, search: ResolvedSearch) -> str:
    """Shown when max_notify caps the replies. Never truncate silently."""
    hidden = total_new - shown
    plural = "" if hidden == 1 else "s"
    return (
        f"_+{hidden} more new listing{plural} not shown (max_notify="
        f"{search.max_notify}). All {total_new} are recorded, so they won't "
        "alert again._"
    )


def format_seed(search: ResolvedSearch, count: int) -> str:
    """First-run confirmation.

    A fully silent seed is indistinguishable from a broken config, so one line
    goes out — but no per-item spam.
    """
    return (
        f"*🌱 {search.name}* — seeded with {count} existing listing(s). "
        f"Future runs alert on new ones only (every {format_interval(search.interval)})."
    )


def format_run_summary(failures: list[tuple[str, str]]) -> str:
    """Posted only when something went wrong — a quiet channel is the goal."""
    lines = [f"*⚠️ watch-searches: {len(failures)} search(es) failed*"]
    lines += [f"• `{name}` — {error}" for name, error in failures]
    return "\n".join(lines)
