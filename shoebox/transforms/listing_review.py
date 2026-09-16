"""Pick the listings whose asking price has stopped working.

Cards get listed in the days after a set drops, which is exactly when the
market pays the most for them. Weeks later the same card is competing with
every other copy that has since been pulled, and the opening price is quietly
above market. Nothing about the listing looks broken -- it is being shown, it
is being clicked -- it just doesn't sell.

The signal here is **traffic without interest**: enough views to know people
are finding the card, no watchers to say anyone wants it at this price, and
enough time on the market for the release-week premium to have worn off. A $4
card with 20 views and nobody watching is the canonical case.

Everything in this module is pure -- rows in, candidates and rejections out. It
holds no clock and talks to no API, which is what makes the thresholds cheap to
test and cheap to tune. ``pipelines/review_listings.py`` supplies the rows and
does something about the answers.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from ..models.listing_review import ReviewCandidate, ReviewRecord
from ..settings import ListingReviewSettings
from ..utils.pricing import calculate_new_price, round_up_to_nine

logger = logging.getLogger(__name__)

# Above the discount matrix's top tier (~$20) ``calculate_new_price`` has no
# answer, so fall back to a flat percentage -- the same shape send-offers uses.
ABOVE_MATRIX_DISCOUNT = 0.05

# Multi-variation ("You Pick") listings are one listing over many cards, so a
# single markdown means nothing. They are skipped by title, the same test
# sync_active_listing_details uses.
VARIATION_TITLE_MARKER = "complete your set"


@dataclass(frozen=True)
class Rejection:
    """A listing that was considered and passed over, and why.

    Kept rather than discarded so ``--dry-run`` can answer the question that
    actually comes up when tuning thresholds: *why isn't this card in the
    list?*
    """

    item_id: str
    title: str
    reason: str


def to_float(value: object) -> float | None:
    """Money as a float. eBay hands prices back as strings, pandas as NaN."""
    if value is None:
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if parsed != parsed else parsed  # NaN


def to_int(value: object, default: int = 0) -> int:
    """Counts as an int. Missing traffic/watch counts mean zero, not an error."""
    parsed = to_float(value)
    return default if parsed is None else int(parsed)


def parse_started_at(value: object) -> datetime | None:
    """Parse eBay's listing start time into a UTC-aware datetime."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def age_in_days(started_at: datetime, now: datetime) -> int:
    return max((now - started_at).days, 0)


def suggest_price(
    price: float,
    *,
    floor: float = 0.99,
    above_matrix_discount: float = ABOVE_MATRIX_DISCOUNT,
) -> float:
    """The price to propose for a listing currently asking ``price``.

    Deliberately the same markdown ladder the rest of the store uses
    (``utils.pricing``) rather than a second opinion invented here: a review is
    a prompt to take one step down, not a repricing engine. The suggestion is
    always strictly below the current price -- a proposal that changes nothing
    is not worth anyone's attention -- and never below ``floor``.
    """
    try:
        target = calculate_new_price(price)
    except ValueError:
        target = price * (1 - above_matrix_discount)

    suggested = round_up_to_nine(target)
    if suggested >= price:
        # Rounding landed back on the current price (or above it); take the
        # next price point down instead.
        suggested = round_up_to_nine(price - 0.10)
    return max(suggested, floor)


def evaluate(
    row: Mapping[str, object],
    *,
    rules: ListingReviewSettings,
    now: datetime,
    record: ReviewRecord | None = None,
    ignore_cooldown: bool = False,
) -> ReviewCandidate | Rejection:
    """Judge one listing against the review rules.

    Returns the candidate to prompt about, or the reason it was passed over.
    """
    item_id = str(row.get("item_id") or "")
    title = str(row.get("title") or item_id)

    def rejected(reason: str) -> Rejection:
        return Rejection(item_id=item_id, title=title, reason=reason)

    if VARIATION_TITLE_MARKER in title.casefold():
        return rejected("variation listing")

    price = to_float(row.get("price"))
    if price is None:
        return rejected("no price")
    if price < rules.min_price:
        return rejected(f"price ${price:.2f} under ${rules.min_price:.2f}")

    started_at = parse_started_at(row.get("start_time"))
    if started_at is None:
        return rejected("no start time")
    age = age_in_days(started_at, now)
    if age < rules.min_age_days:
        return rejected(f"listed {age}d ago, under {rules.min_age_days}d")
    if age > rules.max_age_days:
        return rejected(f"listed {age}d ago, over {rules.max_age_days}d")

    watchers = to_int(row.get("watchers"))
    if watchers > rules.max_watchers:
        return rejected(f"{watchers} watcher(s)")

    views = to_int(row.get("views"))
    if views < rules.min_views:
        return rejected(f"{views} views, under {rules.min_views}")

    if record is not None and not ignore_cooldown:
        since = (now - record.last_reviewed_at).days
        if since < rules.cooldown_days:
            return rejected(f"{record.outcome} {since}d ago (cooldown {rules.cooldown_days}d)")

    suggested = suggest_price(price, floor=rules.price_floor)
    if suggested >= price:
        return rejected(f"already at the ${rules.price_floor:.2f} floor")

    return ReviewCandidate(
        item_id=item_id,
        title=title,
        sku=(str(row["sku"]) if row.get("sku") else None),
        price=price,
        suggested_price=suggested,
        views=views,
        impressions=to_int(row.get("impressions")),
        watchers=watchers,
        age_days=age,
        times_reviewed=(record.times_reviewed if record else 0),
        view_item_url=(str(row["view_item_url"]) if row.get("view_item_url") else None),
    )


def select_candidates(
    rows: Iterable[Mapping[str, object]],
    *,
    rules: ListingReviewSettings,
    now: datetime | None = None,
    history: Mapping[str, ReviewRecord] | None = None,
    ignore_cooldown: bool = False,
    limit: int | None = None,
) -> tuple[list[ReviewCandidate], list[Rejection]]:
    """Rank every listing worth a second look, worst offender first.

    Ordering is views first: the most people who looked at a card and walked
    away is the strongest evidence the price is the problem. Price breaks ties
    so the bigger mistakes come before the pennies, and the item id keeps the
    order stable between runs.

    ``limit`` (default ``rules.max_per_run``) caps how many come back, because
    a review nobody finishes reading is a review that didn't happen.
    """
    now = now or datetime.now(UTC)
    history = history or {}

    candidates: list[ReviewCandidate] = []
    rejections: list[Rejection] = []
    for row in rows:
        verdict = evaluate(
            row,
            rules=rules,
            now=now,
            record=history.get(str(row.get("item_id") or "")),
            ignore_cooldown=ignore_cooldown,
        )
        if isinstance(verdict, Rejection):
            rejections.append(verdict)
        else:
            candidates.append(verdict)

    candidates.sort(key=lambda c: (-c.views, -c.price, c.item_id))

    cap = rules.max_per_run if limit is None else limit
    if cap is not None and cap > 0 and len(candidates) > cap:
        held = candidates[cap:]
        logger.info("Holding %d candidate(s) for a later run (cap %d)", len(held), cap)
        rejections.extend(
            Rejection(item_id=c.item_id, title=c.title, reason=f"over the {cap}-per-run cap")
            for c in held
        )
        candidates = candidates[:cap]

    return candidates, rejections


def summarize_rejections(rejections: Iterable[Rejection]) -> list[tuple[str, int]]:
    """Rejection reasons collapsed to ``(kind, count)``, commonest first.

    The reasons carry each listing's own numbers, so they are grouped by the
    part before the first digit -- "listed 31d ago, over 75d" and "listed 44d
    ago, over 75d" are one line, not two.
    """
    counts: dict[str, int] = {}
    for rejection in rejections:
        kind = "".join("#" if ch.isdigit() else ch for ch in rejection.reason)
        # Collapse runs of digits ("##d") to a single placeholder.
        while "##" in kind:
            kind = kind.replace("##", "#")
        counts[kind] = counts.get(kind, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
