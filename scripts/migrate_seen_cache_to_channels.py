"""One-off: fold per-search seen-caches into the per-channel caches.

Dedup used to be keyed by search name (``<name>_seen.jsonl``); it is now keyed
by Slack channel (``<channel>_seen.jsonl``) so several searches posting to the
same channel report a listing once between them.

Without this, every existing cache is orphaned. That is *safe* -- each search
would notice via ``cache_was_lost`` and reseed silently rather than alerting --
but it throws away the first_seen_at and price history those files hold, and
makes every search refetch its full seed window.

    python scripts/migrate_seen_cache_to_channels.py            # report only
    python scripts/migrate_seen_cache_to_channels.py --apply

Merge rule on a collision: the entry with the later ``last_seen_at`` wins, but
``notified`` is ORed across both. A card one search already posted must not
re-alert because the other search's copy happened to be newer and unnotified.

The old files are left in place (renamed with a ``.premigration`` suffix under
--apply) so the change can be backed out by hand.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic_core import to_jsonable_python

from shoebox.clients.search_state import SearchStateStore
from shoebox.models.saved_search import load_searches_file
from shoebox.models.search_hit import SeenEntry
from shoebox.pipelines.watch_searches import _resolve_channel, seen_scope
from shoebox.settings import get_settings


def _load(path: Path) -> dict[str, SeenEntry]:
    entries: dict[str, SeenEntry] = {}
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = SeenEntry(**json.loads(line))
        except Exception:
            continue
        entries[entry.item_id] = entry
    return entries


def _merge(into: dict[str, SeenEntry], new: dict[str, SeenEntry]) -> None:
    for item_id, entry in new.items():
        prior = into.get(item_id)
        if prior is None:
            # Carried over from a cache that predates the migration, so any
            # deferral it recorded is long stale. Left set, a deferred+unnotified
            # entry fires on the next run that sees the listing -- a burst of
            # alerts for old cards, which is the opposite of the point.
            into[item_id] = entry.model_copy(update={"deferred": False})
            continue
        winner = entry if entry.last_seen_at >= prior.last_seen_at else prior
        into[item_id] = winner.model_copy(
            update={
                "notified": prior.notified or entry.notified,
                "first_seen_at": min(prior.first_seen_at, entry.first_seen_at),
            }
        )


def _migrate(args, store: SearchStateStore) -> None:
    settings = get_settings()
    base = store.base_dir
    searches = load_searches_file(settings.paths.searches_file).resolved()

    by_scope: dict[str, dict[str, SeenEntry]] = {}
    sources: dict[str, list[Path]] = {}

    for search in searches:
        old = base / f"{search.name}_seen.jsonl"
        if not old.exists():
            continue
        scope = seen_scope(_resolve_channel(search))
        target = by_scope.setdefault(scope, _load(base / f"{scope}_seen.jsonl"))
        before = len(target)
        entries = _load(old)
        _merge(target, entries)
        sources.setdefault(scope, []).append(old)
        print(
            f"{search.name:<28} -> {scope:<14} "
            f"{len(entries):>6} entries, +{len(target) - before} new"
        )

    if not by_scope:
        print("Nothing to migrate.")
        return

    print()
    for scope, entries in by_scope.items():
        print(f"{scope}_seen.jsonl would hold {len(entries)} unique listing(s)")

    if not args.apply:
        print("\nReport only — re-run with --apply to write.")
        return

    for scope, entries in by_scope.items():
        path = base / f"{scope}_seen.jsonl"
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for entry in entries.values():
                handle.write(json.dumps(to_jsonable_python(entry)) + "\n")
        tmp.replace(path)
        print(f"Wrote {path} ({len(entries)} entries)")

    for paths in sources.values():
        for old in paths:
            old.rename(old.with_suffix(".jsonl.premigration"))
    print(f"Renamed {sum(len(p) for p in sources.values())} old cache file(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the merged caches")
    args = parser.parse_args()

    # watch-searches runs from Task Scheduler every 5 minutes and appends to
    # these same files. Rewriting them underneath a live run would lose whatever
    # it appended between our read and our write, so take the same lock it does.
    store = SearchStateStore()
    with store.lock() as acquired:
        if not acquired:
            raise SystemExit(
                "A watch-searches run holds the lock. Re-run in a moment, or "
                "disable the \\shoebox\\watch-searches task first."
            )
        _migrate(args, store)


if __name__ == "__main__":
    main()
