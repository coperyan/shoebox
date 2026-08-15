"""Run due saved searches and alert Slack about net-new eBay listings.

Driven by a single cron entry; each search's own ``interval`` decides whether it
actually fires, so adding a search means editing YAML and nothing else.

Two ordering decisions are load-bearing:

**Slack before state.** If state were committed first, a Slack failure would
mark an item seen and it would never be alerted — silently, undetectably. The
other way round, a crash re-alerts an item: visible and self-limiting. For an
alerting system a duplicate is a shrug and a miss is the whole feature failing.

**Commit per item, not per batch.** Combined with the above, this bounds a crash
mid-run to exactly one duplicate rather than the entire run's worth.
"""

from __future__ import annotations

import logging
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from slack_sdk.errors import SlackApiError

from ..clients.search_state import UNCHANGED, SearchStateStore
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

# (channel, text, thread_ts, unfurl_links, blocks) -> message ts
PostFn = Callable[[str, str, str | None, bool, list[dict] | None], str]
FetchFn = Callable[[ResolvedSearch, int], list[ItemSummary]]


class SearchRunResult:
    def __init__(
        self,
        name: str,
        *,
        seeded: bool = False,
        new_count: int = 0,
        fetched: int = 0,
        deferred: int = 0,
    ):
        self.name = name
        self.seeded = seeded
        self.new_count = new_count
        self.fetched = fetched
        # Image-less new listings held for the next run (defer_missing_image).
        self.deferred = deferred


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


def _default_post(
    channel: str,
    text: str,
    thread_ts: str | None,
    unfurl_links: bool,
    blocks: list[dict] | None = None,
) -> str:
    return notify(channel, text, thread_ts=thread_ts, unfurl_links=unfurl_links, blocks=blocks)


def _image_download_failed(exc: Exception) -> bool:
    """True when Slack rejected the message because *it* couldn't fetch the
    image-block URL (``invalid_blocks`` / "downloading image failed").

    Slack downloads image URLs server-side while validating blocks, so a CDN
    hiccup or a fetch eBay refuses fails the whole post -- a property of that
    one photo, not of the message. Anything else stays fatal: another
    ``invalid_blocks`` reason means a bug in our block building, and silently
    swallowing it would hide that.
    """
    response = getattr(exc, "response", None)
    if not response or response.get("error") != "invalid_blocks":
        return False
    return any("downloading image failed" in str(e) for e in response.get("errors") or [])


def _post_item(
    post: PostFn,
    channel: str,
    item: ItemSummary,
    search: ResolvedSearch,
    now: datetime,
) -> None:
    """Post one listing straight into the channel, dropping to the unfurl
    fallback when Slack can't download the photo -- losing the alert over a
    flaky image would invert the priorities."""
    message = fmt.build_item_message(item, search, now=now)
    try:
        post(channel, message.text, None, message.unfurl_links, message.blocks)
    except SlackApiError as exc:
        if message.blocks is None or not _image_download_failed(exc):
            raise
        logger.warning(
            "%s: Slack couldn't download the image for %s; reposting without it",
            search.name,
            item.item_id,
        )
        fallback = fmt.build_unfurl_fallback(item, search, now=now)
        post(channel, fallback.text, None, fallback.unfurl_links, fallback.blocks)


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
    notify_seed: bool = False,
    dry_run: bool = False,
    pacing_seconds: float = SLACK_PACING_SECONDS,
) -> SearchRunResult:
    """Fetch, filter, diff against the seen cache, alert, and commit."""
    limit = search.seed_max_results if seed else search.max_results
    items = filter_items(fetch(search, limit), search)

    seen = store.load_seen(search.name)
    fresh = [i for i in items if i.item_id and i.item_id not in seen]

    def _entry(item: ItemSummary, *, notified: bool, deferred: bool = False) -> SeenEntry:
        # A prior entry exists when this item was deferred last run: its
        # first_seen_at is the truth, not this run's timestamp.
        prior = seen.get(item.item_id or "")
        return SeenEntry(
            search_name=search.name,
            item_id=item.item_id or "",
            first_seen_at=prior.first_seen_at if prior else now,
            last_seen_at=now,
            last_price=item.price_decimal,
            last_price_currency=item.price.currency if item.price else None,
            title=item.title,
            notified=notified,
            deferred=deferred,
        )

    def _hit(item: ItemSummary, **kw) -> SearchHit:
        prior = seen.get(item.item_id or "")
        if prior is not None:
            kw.setdefault("first_seen_at", prior.first_seen_at)
        return SearchHit.from_item(item, search_name=search.name, run_id=run_id, hit_at=now, **kw)

    def _refresh_entry(item: ItemSummary, prior: SeenEntry) -> SeenEntry:
        """Update last_seen_at/last_price while carrying the prior entry's
        first_seen_at, notified, and deferred through — the refresh exists to
        keep those histories current, not to erase them (a future price-drop
        alert reads first_seen_at, and `notified` records that an alert
        already went out)."""
        return SeenEntry(
            search_name=search.name,
            item_id=item.item_id or "",
            first_seen_at=prior.first_seen_at,
            last_seen_at=now,
            last_price=item.price_decimal,
            last_price_currency=item.price.currency if item.price else None,
            title=item.title,
            notified=prior.notified,
            deferred=prior.deferred,
        )

    # ---- Seed: record everything; alert nothing unless notify_on_seed ----
    if seed:
        # Only ever the first max_notify: a seed fetches seed_max_results (2000
        # by default) to build a complete cache, and posting that would be a
        # channel flood, not an alert.
        seed_shown = items[: search.max_notify] if notify_seed else []

        if dry_run:
            logger.info("[dry-run] would seed %s with %d items", search.name, len(items))
            if notify_seed:
                logger.info(
                    "[dry-run] %s", fmt.format_seed(search, len(items), shown=len(seed_shown))
                )
                for item in seed_shown:
                    logger.info(
                        "[dry-run] %s",
                        fmt.format_item(item, search, include_bare_url=False, now=now),
                    )
            else:
                # A silent seed posts nothing per-item, so without a sample here
                # --dry-run would print a bare count -- useless for the thing it
                # exists for, which is eyeballing whether the filters are right.
                _log_sample(items, search)
            return SearchRunResult(search.name, seeded=True, fetched=len(items))

        # One line even when nothing is shown, so a working config is
        # distinguishable from a broken one. Listings follow it in the channel
        # itself -- not a thread -- so they're readable without a click; its ts
        # is still recorded on each hit to tie the batch back to this run.
        header_ts = post(
            channel, fmt.format_seed(search, len(items), shown=len(seed_shown)), None, False, None
        )

        # Same Slack-before-state, commit-per-item rule as a normal run.
        for index, item in enumerate(seed_shown):
            if index:
                time.sleep(pacing_seconds)
            _post_item(post, channel, item, search, now)
            store.append_seen(search.name, [_entry(item, notified=True)])
            store.append_hits(
                [
                    _hit(
                        item,
                        is_seed=True,
                        notified=True,
                        notified_at=datetime.now(UTC),
                        slack_channel=channel,
                        slack_parent_ts=header_ts,
                    )
                ]
            )

        rest = items[len(seed_shown) :]
        store.append_seen(search.name, [_entry(i, notified=False) for i in rest])
        store.append_hits([_hit(i, is_seed=True) for i in rest])
        return SearchRunResult(search.name, seeded=True, fetched=len(items))

    # ---- Normal run -----------------------------------------------------
    # Alert whatever was deferred last run first: one held interval is the
    # deal, photo or no photo — an indefinite wait would trade a missing image
    # for a missing alert. (Checked regardless of defer_missing_image, so
    # flipping the flag off can't strand an already-deferred listing.)
    deferred_due = [
        i
        for i in items
        if i.item_id and (prior := seen.get(i.item_id)) and prior.deferred and not prior.notified
    ]

    # eBay's image CDN often lags a brand-new listing, so alerting the moment
    # it appears posts a photo-less card. Hold image-less finds one interval.
    defer_now: list[ItemSummary] = []
    if search.defer_missing_image:
        defer_now = [i for i in fresh if not i.thumbnail()]
        fresh = [i for i in fresh if i.thumbnail()]

    to_alert = deferred_due + fresh

    if dry_run:
        if defer_now:
            logger.info(
                "[dry-run] would defer %d image-less new listing(s) to the next run",
                len(defer_now),
            )
        if not to_alert:
            return SearchRunResult(search.name, fetched=len(items), deferred=len(defer_now))
        logger.info("[dry-run] %s", fmt.format_header(search, len(to_alert)))
        for item in to_alert[: search.max_notify]:
            logger.info(
                "[dry-run] %s", fmt.format_item(item, search, include_bare_url=False, now=now)
            )
            if not item.thumbnail():
                # A deferred listing whose photo never appeared: it posts anyway.
                logger.info("[dry-run]   (no image — will fall back to link unfurl)")
        if len(to_alert) > search.max_notify:
            logger.info(
                "[dry-run] %s", fmt.format_overflow(len(to_alert), search.max_notify, search)
            )
        return SearchRunResult(
            search.name, new_count=len(to_alert), fetched=len(items), deferred=len(defer_now)
        )

    if defer_now:
        # Recorded before anything else can fail: the point of a deferral is
        # remembering the sighting, so the next run alerts instead of
        # re-deferring. No hit row yet — that is written when the alert goes
        # out, keeping BigQuery at one row per new listing.
        logger.info(
            "%s: deferring %d image-less new listing(s) to the next run",
            search.name,
            len(defer_now),
        )
        store.append_seen(
            search.name, [_entry(i, notified=False, deferred=True) for i in defer_now]
        )

    if not to_alert:
        # No header message: a "0 new" post every interval would drown the channel.
        store.append_seen(
            search.name,
            [_refresh_entry(i, seen[i.item_id]) for i in seen_refresh(items, seen)],
        )
        return SearchRunResult(search.name, fetched=len(items), deferred=len(defer_now))

    # Header plus listings straight in the channel -- no thread, so hits are
    # readable at a glance. The header's ts is still stamped on each hit to tie
    # the batch back to this run.
    header_ts = post(channel, fmt.format_header(search, len(to_alert)), None, False, None)

    shown = to_alert[: search.max_notify]
    for index, item in enumerate(shown):
        if index:
            time.sleep(pacing_seconds)
        _post_item(post, channel, item, search, now)
        # Committed immediately, so a crash costs at most this one duplicate.
        store.append_seen(search.name, [_entry(item, notified=True)])
        store.append_hits(
            [
                _hit(
                    item,
                    notified=True,
                    notified_at=datetime.now(UTC),
                    slack_channel=channel,
                    slack_parent_ts=header_ts,
                )
            ]
        )

    overflow = to_alert[search.max_notify :]
    if overflow:
        time.sleep(pacing_seconds)
        post(channel, fmt.format_overflow(len(to_alert), len(shown), search), None, False, None)
        # Recorded as un-notified (deferred cleared): without this they would
        # re-alert forever.
        store.append_seen(search.name, [_entry(i, notified=False) for i in overflow])
        store.append_hits(
            [
                _hit(i, notified=False, slack_channel=channel, slack_parent_ts=header_ts)
                for i in overflow
            ]
        )

    # Refresh already-seen items so last_price/last_seen_at stay current -- the
    # seam a future price-drop alert builds on. Items handled above are
    # excluded: a refresh carries the *loaded* entry forward, and for a
    # just-alerted deferral that stale entry would resurrect deferred=True and
    # re-alert it next run.
    handled = {i.item_id for i in to_alert}
    store.append_seen(
        search.name,
        [
            _refresh_entry(i, seen[i.item_id])
            for i in seen_refresh(items, seen)
            if i.item_id not in handled
        ],
    )

    return SearchRunResult(
        search.name, new_count=len(to_alert), fetched=len(items), deferred=len(defer_now)
    )


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


def pull_searches_repo(path: str | Path, *, timeout: float = 60) -> bool:
    """``git pull --ff-only`` in the directory containing the searches file.

    Fail-open by design: a pull failure means running with a slightly stale
    (but previously valid) config, which beats not running at all -- and this
    fires on every cron tick, so posting each failure to Slack would spam a
    persistent outage (box offline, expired token) every interval. A stale
    file that is also *broken* still fails loudly through the load_searches_file
    path below.

    Returns True if the pull succeeded, False otherwise (logged, never raised).
    """
    repo_dir = Path(path).parent
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "pull", "--ff-only"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning("Could not pull searches repo %s: %s", repo_dir, exc)
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        logger.warning("git pull failed in %s: %s", repo_dir, detail)
        return False
    logger.info("Pulled searches repo %s: %s", repo_dir, (result.stdout or "").strip())
    return True


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

    # Pull runs for --list and --dry-run too, on purpose: "edit on my phone,
    # then ask the Slack bot for watch-searches --list" is the validation loop,
    # and it only works if listing sees the freshly pushed file.
    if settings.paths.searches_git_pull:
        pull_searches_repo(path)

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
                    None,
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

    candidates = [s for s in searches if s.enabled]
    if only:
        candidates = [s for s in candidates if s.name in set(only)]

    # Validate reseed names against the searches that will actually run,
    # *before* destroying anything: clearing the cache of a search that then
    # doesn't run would hand it to a later silent recovery seed (or, if its
    # recorded seed matched zero items, to a false alert storm) — and a typo'd
    # name would silently do nothing.
    if reseed:
        runnable = {s.name for s in candidates}
        unknown = sorted(set(reseed) - runnable)
        if unknown:
            raise ValueError(
                f"--reseed target(s) not in this run: {', '.join(unknown)}. "
                "A reseed target must be an enabled search, and must match "
                "--only when that filter is given."
            )

    # Discarding a seen-cache is the most destructive thing this command does,
    # and --dry-run promises no state writes -- so a dry run only *rehearses* a
    # reseed. The names are still forced due below and still take the seed path,
    # which is the whole point of rehearsing one.
    if reseed and not dry_run:
        for name in reseed:
            store.clear_seen(name)
            if name in state:
                store.mark(name, last_run_at=now, status="reseed", seeded_at=None, seed_count=None)
        state = store.load_state()
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

    for search in due:
        run_state = state.get(search.name)
        seeded = run_state is not None and run_state.seeded_at is not None
        # Seed when: explicitly asked; never seeded; the seen-cache was lost
        # behind our back; or the last run is so stale that these listings are
        # hours old and no longer actionable. Each case would otherwise dump a
        # whole result page into Slack as "new".
        seed_reason = _seed_reason(search, store=store, now=now, reseed=reseed, seeded=seeded)
        # notify_on_seed covers the seeds a human caused and is expecting output
        # from. The recovery seeds stay silent whatever the config says -- they
        # exist to *suppress* an alert storm over listings that are already old.
        notify_seed = search.notify_on_seed and seed_reason in ("first", "manual")

        try:
            channel = _resolve_channel(search)
            if seed_reason:
                logger.info("%s: seeding (%s), notify=%s", search.name, seed_reason, notify_seed)
            result = run_one_search(
                search,
                store=store,
                run_id=run_id,
                now=now,
                fetch=fetch,
                post=post,
                channel=channel,
                seed=bool(seed_reason),
                notify_seed=notify_seed,
                dry_run=dry_run,
                pacing_seconds=pacing_seconds,
            )
            results.append(result)
            if not dry_run:
                store.mark(
                    search.name,
                    last_run_at=now,
                    status="ok",
                    new_count=result.new_count,
                    seeded_at=now if seed_reason else UNCHANGED,
                    seed_count=result.fetched if seed_reason else UNCHANGED,
                )
                store.compact_seen(
                    search.name,
                    prune_before=now - timedelta(days=search.prune_seen_after_days),
                )
            logger.info(
                "%s: fetched=%d new=%d deferred=%d%s",
                search.name,
                result.fetched,
                result.new_count,
                result.deferred,
                " (seeded)" if result.seeded else "",
            )
        except Exception as exc:  # noqa: BLE001 - one search must not kill the run
            logger.exception("Search %s failed", search.name)
            failures.append((search.name, str(exc)))
            if not dry_run:
                # Advance last_run_at anyway: a permanently broken search that
                # retried every tick would burn the Browse quota for nothing.
                store.mark(search.name, last_run_at=now, status="error", error=str(exc))

    if flush and not dry_run:
        # Attempted every run, not just runs with new hits: flush_append_log
        # no-ops on an empty buffer, and gating on any_new would strand a
        # buffer left behind by a failed flush until the next hit came along.
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
            post(_failure_channel(), fmt.format_run_summary(failures), None, False, None)
        except Exception:
            logger.warning("Could not post failure summary to Slack", exc_info=True)

    return results


def _seed_reason(
    search: ResolvedSearch,
    *,
    store: SearchStateStore,
    now: datetime,
    reseed: list[str],
    seeded: bool,
) -> str | None:
    """Why this search is seeding, or None if it isn't.

    The reason is what ``notify_on_seed`` keys off: a first-ever or explicitly
    requested seed is something a human is waiting on, while ``recovered`` and
    ``stale`` are the watcher quietly repairing itself. Ordered so the two
    state-reading checks are only reached when the cheap ones don't decide it.
    """
    if search.name in set(reseed):
        return "manual"
    if not seeded:
        return "first"
    if store.cache_was_lost(search.name):
        return "recovered"
    if store.is_stale(search, now):
        return "stale"
    return None


def _failure_channel() -> str:
    slack = get_settings().slack
    return slack.search_channel or slack.notify_channel
