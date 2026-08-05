"""Local state for the saved-search watcher, plus the durable BigQuery log.

Modelled on ``clients/image_log.py`` (local cache for dedup + append log that
flushes GCS → BigQuery → clear), with two deliberate differences:

1. ``ImageLogClient`` rewrites its entire cache file on every single upsert.
   That is O(n) per item and O(n²) per run, which is fine for a few hundred
   image uploads and not fine here. This appends one line per item and compacts
   once, at the end of a run.
2. ``ImageLogClient`` builds ``storage.Client``/``bigquery.Client`` eagerly in
   ``__init__``, which is why it has no test coverage. Here the cloud clients
   are built lazily inside ``flush_append_log`` so everything else is testable
   with nothing but a ``tmp_path``.

Layout under ``<exports_dir>/jsonl/searches/``::

    search_state.json          per-search last_run_at / seeded_at / status
    <name>_seen.jsonl          append-only SeenEntry log, last line wins
    search_hits_append.jsonl   shared buffer -> one GCS object + one BQ load
    .lock                      advisory guard against overlapping scheduled runs
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO

from pydantic_core import to_jsonable_python

from ..models.saved_search import ResolvedSearch
from ..models.search_hit import SearchHit, SeenEntry
from ..settings import Settings, get_settings

logger = logging.getLogger(__name__)

STATE_FILENAME = "search_state.json"
HITS_APPEND_FILENAME = "search_hits_append.jsonl"
LOCK_FILENAME = ".lock"

BQ_TABLE = "search_hits"
GCS_PREFIX = "logs/search_hits"

# Compact a seen file once it holds this many times more lines than unique items.
_COMPACT_RATIO = 2

# A search whose last_run_at is this many intervals stale (asleep machine,
# long-disabled search) re-seeds silently instead of alerting: those listings
# are hours or days old and no longer actionable.
STALE_INTERVAL_MULTIPLIER = 6


# ----------------------------------------------------------------------
# Cross-platform file locking
# ----------------------------------------------------------------------
# ``fcntl`` is Unix-only and does not exist on Windows, so importing it at
# module scope would break every search command on a Windows host -- including
# --list and preview-search, which never take the lock at all.
#
# Both backends give the same guarantee the watcher needs: a non-blocking,
# handle-owned exclusive lock, released when the handle closes (so a killed run
# cannot wedge the scheduler). Locks are advisory on both, which is fine -- the
# only writers are watch-searches runs, and they all go through here.
# The inactive branch is still exercised on the other platform: TestWindowsLockBackend
# re-imports this module with sys.platform patched and fcntl made unimportable.
if sys.platform == "win32":
    import msvcrt

    # msvcrt locks a byte range from the *current* file position, so both calls
    # seek to 0 and lock the same single byte. The file's contents are
    # irrelevant; only the lock on byte 0 matters.
    def _acquire(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _release(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _acquire(handle: IO[str]) -> None:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(handle: IO[str]) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)


def _schema_path() -> Path:
    """Resolve the BQ schema relative to the repo, not the CWD.

    Every other pipeline hardcodes ``Path("configs/bigquery/schemas/...")``,
    which only works when invoked from the repo root. This one runs from cron,
    where that is not a safe assumption.
    """
    return (
        Path(__file__).resolve().parents[2]
        / "configs"
        / "bigquery"
        / "schemas"
        / (f"{BQ_TABLE}.json")
    )


@dataclass(frozen=True)
class SearchRunState:
    """Bookkeeping for one search between runs."""

    last_run_at: datetime | None = None
    seeded_at: datetime | None = None
    last_status: str = "unknown"
    last_error: str | None = None
    last_new_count: int = 0
    # How many items the seed recorded. Compared against the live cache size to
    # detect a lost seen-file -- see SearchStateStore.cache_was_lost.
    seed_count: int = 0

    def to_json(self) -> dict:
        return {
            "last_run_at": _iso(self.last_run_at),
            "seeded_at": _iso(self.seeded_at),
            "last_status": self.last_status,
            "last_error": self.last_error,
            "last_new_count": self.last_new_count,
            "seed_count": self.seed_count,
        }

    @classmethod
    def from_json(cls, raw: dict) -> SearchRunState:
        return cls(
            last_run_at=_parse_dt(raw.get("last_run_at")),
            seeded_at=_parse_dt(raw.get("seeded_at")),
            last_status=raw.get("last_status") or "unknown",
            last_error=raw.get("last_error"),
            last_new_count=int(raw.get("last_new_count") or 0),
            seed_count=int(raw.get("seed_count") or 0),
        )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_dt(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Everything is stored UTC-aware; tolerate a naive value from a hand edit.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class SearchStateStore:
    """Seen-cache, run state, and the hits append log."""

    def __init__(
        self,
        *,
        base_dir: Path | None = None,
        settings: Settings | None = None,
    ) -> None:
        if base_dir is None:
            settings = settings or get_settings()
            base_dir = Path(settings.paths.exports_dir) / "jsonl" / "searches"
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._settings = settings

        self.state_path = self.base_dir / STATE_FILENAME
        self.hits_path = self.base_dir / HITS_APPEND_FILENAME
        self.lock_path = self.base_dir / LOCK_FILENAME

    # ------------------------------------------------------------------
    # Locking
    # ------------------------------------------------------------------
    @contextmanager
    def lock(self) -> Iterator[bool]:
        """Non-blocking exclusive lock. Yields False if another run holds it.

        A run posting to Slack at ~1 message/sec can outlast the scheduler's
        period. Without this, two processes would both see a search as due, both
        alert, and their state writes would clobber each other.
        """
        # "a" rather than "w": on Windows the truncation "w" performs is a write
        # to a byte range a running holder has locked, which fails outright --
        # the second run would raise instead of yielding False.
        handle = self.lock_path.open("a")
        try:
            try:
                _acquire(handle)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                _release(handle)
        finally:
            handle.close()

    # ------------------------------------------------------------------
    # Run state
    # ------------------------------------------------------------------
    def load_state(self) -> dict[str, SearchRunState]:
        if not self.state_path.exists():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt state file must not be fatal: treat it as "no state",
            # which re-seeds silently rather than alerting on everything.
            logger.warning("Unreadable %s; treating every search as unseeded", self.state_path)
            return {}
        return {name: SearchRunState.from_json(v) for name, v in raw.items()}

    def save_state(self, state: dict[str, SearchRunState]) -> None:
        payload = {name: s.to_json() for name, s in state.items()}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_path)

    def mark(
        self,
        name: str,
        *,
        last_run_at: datetime,
        status: str,
        error: str | None = None,
        new_count: int = 0,
        seeded_at: datetime | None = None,
        seed_count: int | None = None,
    ) -> None:
        state = self.load_state()
        previous = state.get(name, SearchRunState())
        state[name] = SearchRunState(
            last_run_at=last_run_at,
            seeded_at=seeded_at or previous.seeded_at,
            last_status=status,
            last_error=(error[:500] if error else None),
            last_new_count=new_count,
            seed_count=previous.seed_count if seed_count is None else seed_count,
        )
        self.save_state(state)

    def cache_was_lost(self, name: str) -> bool:
        """True when a search believes it is seeded but its cache is gone.

        ``search_state.json`` and the ``*_seen.jsonl`` caches are separate files,
        so a stray ``rm exports/jsonl/*.jsonl`` wipes the caches while leaving
        the state behind — and every listing would then look brand new. The
        seed count distinguishes that from a search that legitimately seeded
        zero matches and is now seeing its first genuine hit.
        """
        state = self.load_state().get(name)
        if state is None or state.seeded_at is None or state.seed_count == 0:
            return False
        return not self.seen_path(name).exists() or not self.load_seen(name)

    # ------------------------------------------------------------------
    # Due check
    # ------------------------------------------------------------------
    def is_due(
        self,
        search: ResolvedSearch,
        now: datetime,
        *,
        state: dict[str, SearchRunState] | None = None,
        force: bool = False,
    ) -> bool:
        if force:
            return True

        run_state = (state if state is not None else self.load_state()).get(search.name)
        if run_state is None or run_state.last_run_at is None:
            return True

        elapsed = now - run_state.last_run_at
        if elapsed < timedelta(0):
            # Clock moved backwards (NTP correction, restored VM). Without this
            # the search would never run again.
            logger.warning(
                "%s: last_run_at is in the future (%s > %s); running now",
                search.name,
                run_state.last_run_at,
                now,
            )
            return True

        # Cron fires on a fixed tick, so requiring the full interval to elapse
        # rounds every search up to the next tick and the drift compounds.
        # Allow a half-tick of slack.
        return elapsed >= search.interval * 0.9

    def is_stale(self, search: ResolvedSearch, now: datetime) -> bool:
        """True when last_run_at is so old that alerting would be noise."""
        run_state = self.load_state().get(search.name)
        if run_state is None or run_state.last_run_at is None:
            return False
        return (now - run_state.last_run_at) > search.interval * STALE_INTERVAL_MULTIPLIER

    # ------------------------------------------------------------------
    # Seen cache
    # ------------------------------------------------------------------
    def seen_path(self, name: str) -> Path:
        return self.base_dir / f"{name}_seen.jsonl"

    def load_seen(self, name: str) -> dict[str, SeenEntry]:
        """Item id -> entry. Later lines win, so appends act as updates."""
        path = self.seen_path(name)
        if not path.exists():
            return {}

        entries: dict[str, SeenEntry] = {}
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = SeenEntry(**json.loads(line))
                except Exception:
                    # One malformed line shouldn't cost the whole cache -- that
                    # would re-alert everything this search has ever matched.
                    continue
                entries[entry.item_id] = entry
        return entries

    def append_seen(self, name: str, entries: Iterable[SeenEntry]) -> None:
        entries = list(entries)
        if not entries:
            return
        with self.seen_path(name).open("a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(to_jsonable_python(entry)) + "\n")

    def compact_seen(self, name: str, *, prune_before: datetime | None = None) -> int:
        """Rewrite the seen file to one line per item. Returns lines written.

        Called once at the end of a run rather than per item.
        """
        path = self.seen_path(name)
        if not path.exists():
            return 0

        line_count = sum(
            1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        )
        entries = self.load_seen(name)

        if prune_before is not None:
            entries = {
                item_id: entry
                for item_id, entry in entries.items()
                if entry.last_seen_at >= prune_before
            }
            pruned = len(self.load_seen(name)) - len(entries)
            if pruned:
                logger.info("%s: pruned %d seen entries older than %s", name, pruned, prune_before)
        elif line_count <= len(entries) * _COMPACT_RATIO:
            return line_count

        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for entry in entries.values():
                handle.write(json.dumps(to_jsonable_python(entry)) + "\n")
        tmp.replace(path)
        return len(entries)

    def clear_seen(self, name: str) -> None:
        self.seen_path(name).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Hits append log -> GCS -> BigQuery
    # ------------------------------------------------------------------
    def append_hits(self, hits: Iterable[SearchHit]) -> None:
        hits = list(hits)
        if not hits:
            return
        with self.hits_path.open("a", encoding="utf-8") as handle:
            for hit in hits:
                handle.write(json.dumps(hit.to_bq_json()) + "\n")

    def clear_hits(self) -> None:
        self.hits_path.unlink(missing_ok=True)

    def flush_append_log(
        self, *, gcs=None, bq=None, clear_after_success: bool = True
    ) -> str | None:
        """Ship buffered hits to GCS then BigQuery. Returns the object name.

        Returns None when there is nothing buffered. The buffer survives a
        failure on purpose: it retries on the next run, and because dedup runs
        off the local seen cache, a BigQuery outage can never cause a duplicate
        or a missed Slack alert.
        """
        if not self.hits_path.exists() or self.hits_path.stat().st_size == 0:
            return None

        # Imported and constructed lazily so the rest of this class needs no
        # GCP credentials -- that is what makes it testable.
        from .bigquery import BigQueryClient
        from .gcs import GCSClient

        settings = self._settings or get_settings()
        gcs = gcs or GCSClient(settings)
        bq = bq or BigQueryClient(settings)

        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
        object_name = f"{GCS_PREFIX}/{BQ_TABLE}_{stamp}.jsonl"

        gcs.upload_text(
            bucket=settings.gcs.ebay_bucket,
            object_name=object_name,
            text=self.hits_path.read_text(encoding="utf-8"),
            content_type="application/json",
        )
        bq.load_jsonl_from_gcs(
            bucket=settings.gcs.ebay_bucket,
            object_name=object_name,
            dataset=settings.bigquery.ebay_dataset,
            table=BQ_TABLE,
            schema_path=_schema_path(),
            write_disposition="WRITE_APPEND",
        )

        if clear_after_success:
            self.clear_hits()
        return object_name
