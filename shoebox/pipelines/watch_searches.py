"""Run due saved searches and alert Slack about net-new eBay listings.

Driven by a single cron entry; each search's own ``interval`` decides whether it
actually fires, so adding a search means editing YAML and nothing else.

Two ordering decisions are load-bearing:

**Slack before state.** If state were committed first, a Slack failure would
mark an item seen and it would never be alerted — silently, undetectably. The
other way round, a crash re-alerts an item: visible and self-limiting. For an
alerting system a duplicate is a shrug and a miss is the whole feature failing.

**Commit per item, not per batch.** Combined with the above, this bounds a crash
mid-thread to exactly one duplicate rather than the entire run's worth.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..clients.search_state import SearchStateStore
from ..models.ebay.item_summary import ItemSummary
from ..models.saved_search import ResolvedSearch, load_searches_file
from ..models.search_hit import SearchHit, SeenEntry
from ..settings import get_settings
from ..transforms.search_filters import build_aspect_filter, build_browse_filter, filter_items
from ..utils import search_formatting as fmt
from ..utils.slack import notify

logger = logging.getLogger(__name__)

# Slack permits roughly one chat.postMessage per second per channel. The SDK's
# retry handler only reacts *after* a 429 and gives up after three tries, which
# would abort a run mid-thread -- so pace proactively instead.
SLACK_PACING_SECONDS = 1.0

# How many matched listings --dry-run prints. Enough to judge a filter without
# dumping a whole seed window into the terminal.
DRY_RUN_SAMPLE = 15

# (channel, text, thread_ts, unfurl_links) -> message ts
PostFn = Callable[[str, str, str | None, bool], str]
FetchFn = Callable[[ResolvedSearch, int], list[ItemSummary]]


class SearchRunResult:
    def __init__(self, name: str, *, seeded: bool = False, new_count: int = 0, fetched: int = 0):
        self.name = name
        self.seeded = seeded
        self.new_count = new_count
        self.fetched = fetched


def _default_fetch(search: ResolvedSearch, max_results: int) -> list[ItemSummary]:
    from ..clients.ebay_rest.client import get_client

    browse_filter = build_browse_filter(search)
    aspect_filter = build_aspect_filter(search)
    # Logged so a dry run shows exactly what eBay was asked, not just what came
    # back -- most "why didn't this match?" questions are answered here.
    logger.info(
        "%s: q=%r category=%s sort=%s filter=%s%s",
        search.name,
        search.query,
        ",".join(search.category_ids) or "-",
        search.sort,
        browse_filter,
        f" aspect_filter={aspect_filter}" if aspect_filter else "",
    )

    browse = get_client().browse
    return browse.search(
        q=search.query,
        category_ids=",".join(search.category_ids) or None,
        filter=browse_filter,
        aspect_filter=aspect_filter,
        sort=search.sort,
        max_results=max_results,
    )


def _default_post(channel: str, text: str, thread_ts: str | None, unfurl_links: bool) -> str:
    return notify(channel, text, thread_ts=thread_ts, unfurl_links=unfurl_links)


def _resolve_channel(search: ResolvedSearch) -> str:
    slack = get_settings().slack
    channel = search.channel or slack.search_channel or slack.notify_channel
    if not channel:
        raise ValueError(
            f"search {search.name!r}: no Slack channel. Set slack.search_channel "
            "in app.yaml or a per-search 'channel'."
        )
    return channel


def run_one_search(
    search: ResolvedSearch,
    *,
    store: SearchStateStore,
    run_id: str,
    now: datetime,
    fetch: FetchFn,
    post: PostFn,
    channel: str,
    seed: bool,
    dry_run: bool = False,
    pacing_seconds: float = SLACK_PACING_SECONDS,
) -> SearchRunResult:
    """Fetch, filter, diff against the seen cache, alert, and commit."""
    limit = search.seed_max_results if seed else search.max_results
    items = filter_items(fetch(search, limit), search)

    seen = store.load_seen(search.name)
    fresh = [i for i in items if i.item_id and i.item_id not in seen]

    def _entry(item: ItemSummary, *, notified: bool) -> SeenEntry:
        return SeenEntry(
            search_name=search.name,
            item_id=item.item_id or "",
            first_seen_at=now,
            last_seen_at=now,
            last_price=item.price_decimal,
            last_price_currency=item.price.currency if item.price else None,
            title=item.title,
            notified=notified,
        )

    def _hit(item: ItemSummary, **kw) -> SearchHit:
        return SearchHit.from_item(item, search_name=search.name, run_id=run_id, hit_at=now, **kw)

    # ---- Seed: record everything, alert nothing -------------------------
    if seed:
        if not dry_run:
            store.append_seen(search.name, [_entry(i, notified=False) for i in items])
            store.append_hits([_hit(i, is_seed=True) for i in items])
            # One line, so a working config is distinguishable from a broken one.
            post(channel, fmt.format_seed(search, len(items)), None, False)
        else:
            # A seed posts nothing per-item, so without a sample here --dry-run
            # would print a bare count -- useless for the thing it exists for,
            # which is eyeballing whether the filters are actually right.
            logger.info("[dry-run] would seed %s with %d items", search.name, len(items))
            _log_sample(items, search)
        return SearchRunResult(search.name, seeded=True, fetched=len(items))

    # ---- Normal run -----------------------------------------------------
    if not fresh:
        # No parent message: a "0 new" post every interval would drown the channel.
        if not dry_run:
            store.append_seen(
                search.name, [_entry(i, notified=False) for i in seen_refresh(items, seen)]
            )
        return SearchRunResult(search.name, fetched=len(items))

    if dry_run:
        logger.info("[dry-run] %s", fmt.format_parent(search, len(fresh)))
        for item in fresh[: search.max_notify]:
            logger.info("[dry-run] %s", fmt.format_item(item, search))
        if len(fresh) > search.max_notify:
            logger.info("[dry-run] %s", fmt.format_overflow(len(fresh), search.max_notify, search))
        return SearchRunResult(search.name, new_count=len(fresh), fetched=len(items))

    parent_ts = post(channel, fmt.format_parent(search, len(fresh)), None, False)

    shown = fresh[: search.max_notify]
    for index, item in enumerate(shown):
        if index:
            time.sleep(pacing_seconds)
        post(channel, fmt.format_item(item, search), parent_ts, True)
        # Committed immediately, so a crash costs at most this one duplicate.
        store.append_seen(search.name, [_entry(item, notified=True)])
        store.append_hits(
            [
                _hit(
                    item,
                    notified=True,
                    notified_at=datetime.now(UTC),
                    slack_channel=channel,
                    slack_parent_ts=parent_ts,
                )
            ]
        )

    overflow = fresh[search.max_notify :]
    if overflow:
        time.sleep(pacing_seconds)
        post(channel, fmt.format_overflow(len(fresh), len(shown), search), parent_ts, False)
        # Recorded as un-notified: without this they would re-alert forever.
        store.append_seen(search.name, [_entry(i, notified=False) for i in overflow])
        store.append_hits(
            [
                _hit(i, notified=False, slack_channel=channel, slack_parent_ts=parent_ts)
                for i in overflow
            ]
        )

    # Refresh already-seen items so last_price/last_seen_at stay current -- the
    # seam a future price-drop alert builds on.
    store.append_seen(search.name, [_entry(i, notified=False) for i in seen_refresh(items, seen)])

    return SearchRunResult(search.name, new_count=len(fresh), fetched=len(items))


def _log_sample(items: list[ItemSummary], search: ResolvedSearch) -> None:
    """Print a readable sample of matched listings for --dry-run inspection."""
    if not items:
        logger.info("[dry-run] nothing matched -- loosen the filters or check the query")
        return

    sample = items[:DRY_RUN_SAMPLE]
    logger.info("[dry-run] showing %d of %d matches (newest first):", len(sample), len(items))
    for item in sample:
        price = item.price_decimal
        logger.info(
            "[dry-run]   %-9s %-70s %s",
            f"${price:,.2f}" if price is not None else "?",
            (item.title or "")[:70],
            item.item_web_url or "",
        )
    if len(items) > len(sample):
        logger.info("[dry-run]   ... and %d more", len(items) - len(sample))


def seen_refresh(items: list[ItemSummary], seen: dict[str, SeenEntry]) -> list[ItemSummary]:
    """Items we already knew about, for a last_seen_at/last_price refresh."""
    return [i for i in items if i.item_id and i.item_id in seen]


def watch_searches(
    *,
    force: bool = False,
    dry_run: bool = False,
    only: list[str] | None = None,
    reseed: list[str] | None = None,
    config_path: str | Path | None = None,
    flush: bool = True,
    list_only: bool = False,
    store: SearchStateStore | None = None,
    fetch: FetchFn | None = None,
    post: PostFn | None = None,
    pacing_seconds: float = SLACK_PACING_SECONDS,
) -> list[SearchRunResult]:
    settings = get_settings()
    path = config_path or settings.paths.searches_file

    # A config error is global, not per-search: fail the whole run rather than
    # half-running a broken file. Under cron nobody reads the log, so the error
    # goes to Slack too before it propagates.
    try:
        searches_file = load_searches_file(path)
        all_searches = searches_file.resolved()
    except Exception as exc:
        logger.error("Could not load searches config %s: %s", path, exc)
        if not (dry_run or list_only):
            try:
                (post or _default_post)(
                    _failure_channel(),
                    f"*⚠️ watch-searches: bad config*\n`{path}`\n```{exc}```",
                    None,
                    False,
                )
            except Exception:
                logger.warning("Could not post the config error to Slack", exc_info=True)
        raise

    if list_only:
        for search in all_searches:
            state = "enabled" if search.enabled else "disabled"
            logger.info(
                "%-28s %-8s every %s -> %s",
                search.name,
                state,
                fmt.format_interval(search.interval),
                search.channel or "(default channel)",
            )
        return []

    store = store or SearchStateStore(settings=settings)
    fetch = fetch or _default_fetch
    post = post or _default_post

    with store.lock() as acquired:
        if not acquired:
            logger.warning("Another watch-searches run holds the lock; skipping this tick.")
            return []
        return _run_locked(
            searches=all_searches,
            store=store,
            fetch=fetch,
            post=post,
            force=force,
            dry_run=dry_run,
            only=only,
            reseed=reseed or [],
            flush=flush,
            pacing_seconds=pacing_seconds,
        )


def _run_locked(
    *,
    searches: list[ResolvedSearch],
    store: SearchStateStore,
    fetch: FetchFn,
    post: PostFn,
    force: bool,
    dry_run: bool,
    only: list[str] | None,
    reseed: list[str],
    flush: bool,
    pacing_seconds: float = SLACK_PACING_SECONDS,
) -> list[SearchRunResult]:
    now = datetime.now(UTC)
    run_id = uuid.uuid4().hex
    state = store.load_state()

    for name in reseed:
        store.clear_seen(name)
        if name in state:
            store.mark(name, last_run_at=now, status="reseed", seeded_at=None)
    if reseed:
        state = store.load_state()

    candidates = [s for s in searches if s.enabled]
    if only:
        candidates = [s for s in candidates if s.name in set(only)]
    due = [
        s
        for s in candidates
        if s.name in set(reseed) or store.is_due(s, now, state=state, force=force)
    ]

    if not due:
        logger.info("No searches due (%d enabled).", len(candidates))
        return []

    results: list[SearchRunResult] = []
    failures: list[tuple[str, str]] = []
    any_new = False

    for search in due:
        run_state = state.get(search.name)
        seeded = run_state is not None and run_state.seeded_at is not None
        # Seed silently when: explicitly asked; never seeded; the seen-cache was
        # lost behind our back; or the last run is so stale that these listings
        # are hours old and no longer actionable. Each case would otherwise dump
        # a whole result page into Slack as "new".
        seed = (
            search.name in set(reseed)
            or not seeded
            or store.cache_was_lost(search.name)
            or store.is_stale(search, now)
        )

        try:
            channel = _resolve_channel(search)
            result = run_one_search(
                search,
                store=store,
                run_id=run_id,
                now=now,
                fetch=fetch,
                post=post,
                channel=channel,
                seed=seed,
                dry_run=dry_run,
                pacing_seconds=pacing_seconds,
            )
            results.append(result)
            any_new = any_new or bool(result.new_count) or result.seeded
            if not dry_run:
                store.mark(
                    search.name,
                    last_run_at=now,
                    status="ok",
                    new_count=result.new_count,
                    seeded_at=now if seed else None,
                    seed_count=result.fetched if seed else None,
                )
                store.compact_seen(
                    search.name,
                    prune_before=now - timedelta(days=search.prune_seen_after_days),
                )
            logger.info(
                "%s: fetched=%d new=%d%s",
                search.name,
                result.fetched,
                result.new_count,
                " (seeded)" if result.seeded else "",
            )
        except Exception as exc:  # noqa: BLE001 - one search must not kill the run
            logger.exception("Search %s failed", search.name)
            failures.append((search.name, str(exc)))
            if not dry_run:
                # Advance last_run_at anyway: a permanently broken search that
                # retried every tick would burn the Browse quota for nothing.
                store.mark(search.name, last_run_at=now, status="error", error=str(exc))

    if flush and any_new and not dry_run:
        try:
            obj = store.flush_append_log()
            if obj:
                logger.info("Flushed search hits to gs://.../%s", obj)
        except Exception:
            # The buffer survives and retries next run. Dedup never depended on
            # BigQuery, so this cannot cause a duplicate or a missed alert.
            logger.warning("Failed to flush search hits; will retry next run", exc_info=True)

    if failures and not dry_run:
        try:
            post(_failure_channel(), fmt.format_run_summary(failures), None, False)
        except Exception:
            logger.warning("Could not post failure summary to Slack", exc_info=True)

    return results


def _failure_channel() -> str:
    slack = get_settings().slack
    return slack.search_channel or slack.notify_channel
