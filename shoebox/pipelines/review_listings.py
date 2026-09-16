"""Periodic price review for listings the market has moved past.

Cards go up in the days after a set drops, which is when they fetch the most.
A few weeks later every other copy has been pulled too, the opening price is
above market, and the listing gives no sign of being wrong -- it is shown, it
is clicked, and it quietly does not sell. This is the standing prompt to go
back and look at those: enough views to know people found the card, no
watchers to say anyone wants it at this price, and old enough for the
release-week premium to have worn off (see ``transforms/listing_review.py``
for the rules and ``review`` in ``app.yaml`` for the thresholds).

Each candidate is posted to Slack with its numbers, its photo, and a proposed
markdown from the store's standard ladder. Reply in the thread with a price to
take it, ``ok`` for the suggestion, ``keep`` to say the price is right, or
``skip`` to be asked again next run. Decisions are remembered
(``clients/review_state.py``) so nothing is raised twice inside the cooldown;
skipped and unanswered prompts are not, so they come back.

**Not a relist.** ``relist-listings`` is for listings old enough (90+ days)
that their standing in search is worth nothing, and it rebuilds them from
scratch under a new listing id. Here the listing is weeks old and perfectly
healthy -- only the number is wrong -- so the price is edited in place and the
listing keeps its id, its watchers and its age.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from shoebox.clients.ebay.client import EbayClient
from shoebox.clients.review_state import ReviewStateStore
from shoebox.models.listing_review import OUTCOME_KEPT, OUTCOME_REPRICED, ReviewCandidate
from shoebox.services.listings import ListingService
from shoebox.settings import ListingReviewSettings, get_settings
from shoebox.transforms.listing_review import (
    Rejection,
    select_candidates,
    summarize_rejections,
)
from shoebox.utils.pricing import parse_price_reply
from shoebox.utils.slack import BatchPrompt, notify, notify_batch_and_wait

logger = logging.getLogger(__name__)

# eBay's traffic report reaches back 90 days; 89 keeps clear of the boundary.
# Listings older than this would report only part of their views, which is why
# review.max_age_days sits well inside the window.
TRAFFIC_LOOKBACK_DAYS = 89

ACCEPT_KEYWORDS = {"ok", "okay", "yes", "y", "yep", "sure", "go", "approve"}
KEEP_KEYWORDS = {"keep", "hold", "leave", "no", "stay", "as is"}
SKIP_KEYWORDS = {"skip", "later", "pass", "defer"}

PostFn = Callable[..., str]
BatchFn = Callable[..., dict[str, str]]


def resolve_rules(
    rules: ListingReviewSettings, overrides: Mapping[str, object] | None = None
) -> ListingReviewSettings:
    """Config thresholds with any command-line overrides applied."""
    clean = {k: v for k, v in (overrides or {}).items() if v is not None}
    return rules.model_copy(update=clean) if clean else rules


def fetch_listing_rows(
    ebay: EbayClient, *, lookback_days: int = TRAFFIC_LOOKBACK_DAYS
) -> list[dict]:
    """Active listings joined to their view/impression counts.

    A traffic failure is fatal rather than tolerated. ``relist-listings`` can
    fall back to zeroed traffic because it has other criteria to work with;
    here traffic *is* the criterion, and a run without it would report "nothing
    to review" -- the most misleading answer available.
    """
    listings = ebay.trading.get_active_listings()
    item_ids = [str(listing["item_id"]) for listing in listings if listing.get("item_id")]
    logger.info("Fetched %d active listing(s)", len(item_ids))
    if not item_ids:
        return []

    now = datetime.now()
    try:
        report = ebay.analytics.get_traffic_report(
            date_from=(now - timedelta(days=lookback_days)).strftime("%Y%m%d"),
            date_to=now.strftime("%Y%m%d"),
            listing_ids=item_ids,
        )
    except Exception as e:
        raise RuntimeError(
            f"Could not fetch the eBay traffic report ({e}); without view counts "
            "every listing would look uninteresting. Try again later."
        ) from e

    traffic = {str(record.get("LISTING_ID")): record for record in report}
    logger.info("Matched traffic for %d of %d listing(s)", len(traffic), len(item_ids))

    rows = []
    for listing in listings:
        record = traffic.get(str(listing.get("item_id"))) or {}
        rows.append(
            {
                **listing,
                "views": record.get("LISTING_VIEWS_TOTAL"),
                "impressions": record.get("LISTING_IMPRESSION_TOTAL"),
            }
        )
    return rows


def attach_photos(ebay: EbayClient, candidates: list[ReviewCandidate]) -> None:
    """Best-effort: hang the first listing photo off each candidate.

    One ``GetItem`` call per candidate, which is why it runs after selection
    and after the per-run cap -- the sweep is a couple of dozen calls, not a
    couple of thousand. A card you can see is a card you can price, so it is
    worth the calls; a failure just means a prompt without a picture.
    """
    for candidate in candidates:
        try:
            details = ebay.trading.get_item_details(item_id=candidate.item_id)
        except Exception as e:
            logger.debug("No photo for %s: %s", candidate.item_id, e)
            continue
        pictures = details.get("picture_urls") or []
        if pictures:
            candidate.picture_url = pictures[0]


def format_review_prompt(candidate: ReviewCandidate, *, cooldown_days: int) -> BatchPrompt:
    """Build the Slack prompt for one listing.

    mrkdwn rather than a code block so the listing link renders, with the photo
    as an explicit image block (bot messages don't unfurl).
    """
    headline = (
        f"*<{candidate.view_item_url}|{candidate.title}>*"
        if candidate.view_item_url
        else f"*{candidate.title}*"
    )
    stats = (
        f"${candidate.price:.2f} · {candidate.views} views · "
        f"{candidate.impressions} impressions · {candidate.watchers} watchers · "
        f"listed {candidate.age_days}d ago"
    )
    suggestion = (
        f"Suggested: *${candidate.suggested_price:.2f}* "
        f"(−${candidate.markdown:.2f}, {candidate.markdown_pct:.0f}% off)"
    )
    if candidate.times_reviewed:
        suggestion += f"\n_Reviewed {candidate.times_reviewed}× before._"

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{headline}\n{stats}\n{suggestion}"},
        }
    ]
    if candidate.picture_url:
        blocks.append(
            {"type": "image", "image_url": candidate.picture_url, "alt_text": candidate.title}
        )
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Reply with a price, `ok` for ${candidate.suggested_price:.2f}, "
                        f"`keep` to leave it (asked again in {cooldown_days}d), "
                        "or `skip` to be asked next run."
                    ),
                }
            ],
        }
    )
    return BatchPrompt(
        key=candidate.item_id,
        text=(
            f"{candidate.title} — ${candidate.price:.2f}, {candidate.views} views, "
            f"{candidate.watchers} watchers, {candidate.age_days}d old. "
            f"Suggested ${candidate.suggested_price:.2f}."
        ),
        blocks=blocks,
    )


def decide_reply(
    candidate: ReviewCandidate, reply_text: str, *, floor: float = 0.99
) -> tuple[str, float | None, str | None]:
    """Classify a threaded reply for one candidate.

    Returns (action, amount, thread_msg): "apply" with the price to set, "keep"
    (price is right -- sleep for the cooldown), "skip" (ask again next run), or
    "retry" with a hint to post in-thread, leaving the prompt pending.
    """
    text = reply_text.strip().casefold()
    if text in ACCEPT_KEYWORDS:
        return "apply", candidate.suggested_price, None
    if text in KEEP_KEYWORDS:
        return "keep", None, None
    if text in SKIP_KEYWORDS:
        return "skip", None, None

    amount = parse_price_reply(reply_text)
    if amount is None:
        return (
            "retry",
            None,
            "Couldn't parse that — reply with a price like 3.79, or `ok` / `keep` / `skip`.",
        )
    if amount >= candidate.price:
        return (
            "retry",
            None,
            f"That's at or above the current ${candidate.price:.2f} — this is a markdown "
            "review, so reply with a lower price or `keep`.",
        )
    if amount < floor:
        return "retry", None, f"Floor is ${floor:.2f} — reply with a higher price or `keep`."
    return "apply", amount, None


def _reprice(
    ebay: EbayClient, state: ReviewStateStore, candidate: ReviewCandidate, amount: float
) -> dict:
    """Push a new price to eBay and remember the decision."""
    result = ListingService(ebay).update_price(
        new_price=amount, sku=candidate.sku, item_id=candidate.item_id
    )
    state.record(
        candidate.item_id,
        outcome=OUTCOME_REPRICED,
        price=candidate.price,
        new_price=amount,
    )
    return result


def _log_preview(candidates: list[ReviewCandidate], rejections: list[Rejection]) -> None:
    """Print the would-be prompts, and why everything else was passed over."""
    import pandas as pd
    from rich.console import Console

    from shoebox.utils.render_table import render_table

    console = Console()
    if candidates:
        preview = pd.DataFrame(
            [
                {
                    "item_id": c.item_id,
                    "title": c.title[:48],
                    "price": f"${c.price:.2f}",
                    "suggested": f"${c.suggested_price:.2f}",
                    "views": c.views,
                    "watch": c.watchers,
                    "age": f"{c.age_days}d",
                    "seen": f"{c.views_per_day:.1f}/day",
                }
                for c in candidates
            ]
        )
        console.print(render_table(preview, title=f"Price review ({len(candidates)} listing(s))"))
    else:
        console.print("[yellow]No listings matched the review rules.[/yellow]")

    if rejections:
        console.print("\n[bold]Passed over:[/bold]")
        for reason, count in summarize_rejections(rejections):
            console.print(f"  {count:>5}  {reason}")


def main(
    dry_run: bool = False,
    auto: bool = False,
    force: bool = False,
    limit: int | None = None,
    timeout_s: int = 900,
    overrides: Mapping[str, object] | None = None,
    *,
    ebay: EbayClient | None = None,
    state: ReviewStateStore | None = None,
    post: PostFn = notify,
    run_batch: BatchFn = notify_batch_and_wait,
    now: datetime | None = None,
) -> list[ReviewCandidate]:
    """Surface listings whose price has stopped working and act on the replies."""
    settings = get_settings()
    rules = resolve_rules(settings.review, overrides)
    ebay = ebay or EbayClient()
    state = state or ReviewStateStore(settings=settings)
    now = now or datetime.now(UTC)

    rows = fetch_listing_rows(ebay)
    candidates, rejections = select_candidates(
        rows,
        rules=rules,
        now=now,
        history=state.load(),
        ignore_cooldown=force,
        limit=limit,
    )
    logger.info("%d listing(s) up for review, %d passed over", len(candidates), len(rejections))

    if dry_run:
        _log_preview(candidates, rejections)
        return candidates

    # A listing that has sold or been relisted will never be seen again under
    # this item id, so its history is dead weight.
    state.prune(str(row.get("item_id")) for row in rows)

    channel = rules.channel or settings.slack.pricing_channel
    if not candidates:
        logger.info("Nothing to review")
        return []

    attach_photos(ebay, candidates)

    if auto:
        repriced, failed = 0, 0
        for candidate in candidates:
            try:
                _reprice(ebay, state, candidate, candidate.suggested_price)
                repriced += 1
            except Exception:
                logger.warning("Failed to reprice %s", candidate.item_id, exc_info=True)
                failed += 1
        summary = f"🏷️ *price review* — auto-repriced {repriced} listing(s)."
        if failed:
            summary += f" {failed} failed; see the logs."
        post(channel, summary)
        return candidates

    parent_ts = post(
        channel,
        f"🏷️ *price review* — {len(candidates)} listing(s) look priced above the market. "
        "Reply to each with a price, `ok`, `keep`, or `skip`.",
    )

    by_id = {c.item_id: c for c in candidates}
    # Replies can arrive faster than eBay answers, and the shared ebay_rest
    # client isn't guaranteed thread-safe (token refresh), so the writes --
    # eBay and the state file alike -- are serialized.
    write_lock = asyncio.Lock()

    async def on_reply(key: str, reply_text: str, post_thread) -> str | None:
        candidate = by_id[key]
        action, amount, thread_msg = decide_reply(candidate, reply_text, floor=rules.price_floor)
        if action == "skip":
            return "⏭️ Skipped — back next run"
        if action == "retry":
            await post_thread(thread_msg)
            return None
        if action == "keep":
            async with write_lock:
                await asyncio.to_thread(
                    state.record,
                    candidate.item_id,
                    outcome=OUTCOME_KEPT,
                    price=candidate.price,
                )
            return f"🔒 Kept at ${candidate.price:.2f}"

        try:
            async with write_lock:
                await asyncio.to_thread(_reprice, ebay, state, candidate, amount)
        except Exception as e:
            logger.warning("Reprice failed for %s: %s", key, e)
            await post_thread(f"⚠️ eBay rejected the change: {e} — reply with another price.")
            return None
        return f"✅ Repriced ${candidate.price:.2f} → ${amount:.2f}"

    results = run_batch(
        channel,
        [format_review_prompt(c, cooldown_days=rules.cooldown_days) for c in candidates],
        on_reply,
        timeout_s=timeout_s,
    )

    repriced = sum(1 for v in results.values() if v.startswith("✅"))
    kept = sum(1 for v in results.values() if v.startswith("🔒"))
    skipped = sum(1 for v in results.values() if v.startswith("⏭️"))
    expired = len(candidates) - repriced - kept - skipped
    logger.info(
        "review-listings done: %d repriced, %d kept, %d skipped, %d expired",
        repriced,
        kept,
        skipped,
        expired,
    )
    post(
        channel,
        f"Done — {repriced} repriced, {kept} kept, {skipped} skipped, {expired} expired.",
        thread_ts=parent_ts,
    )
    return candidates


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Failed to run review-listings")
        raise
