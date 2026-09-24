"""GCS-backed state for the saved-search watcher, so it can run in Cloud Run.

A Cloud Run execution starts on an empty filesystem and loses it when it ends,
so the files ``SearchStateStore`` keeps under ``exports/jsonl/searches/`` have
to live somewhere else between runs. Rather than teach every method to talk to
GCS, this store keeps the local implementation unchanged and works on a
scratch copy:

1. ``lock()`` takes a lock *object* in GCS, then downloads every state file
   under the prefix into a private temp directory.
2. The run reads and appends to those local files exactly as it would on a
   workstation -- the append-then-compact design, the dedup, the seed logic
   are all the same code.
3. ``checkpoint()`` (after each search) and the end of ``lock()`` upload the
   files whose contents changed and delete the objects whose file is gone
   (the hits buffer after a successful flush).

**Why not append in GCS directly.** GCS objects are immutable; an "append" is a
rewrite of the whole object. Per-item rewrites would be one upload per Slack
message for no gain over rewriting once per search.

**What a crash costs.** On a workstation, state is committed per item, so a
crash costs at most one duplicate alert. Here it is committed per search: a
run killed mid-search re-alerts that search's already-posted listings (at most
``max_notify``) on the next run. Still a duplicate, never a miss -- the
Slack-before-state rule holds, because nothing is uploaded that Slack has not
already seen. A SIGTERM (Cloud Run's timeout signal) is turned into a normal
exit so the final upload and the lock release still happen.

**The lock.** Created with ``ifGenerationMatch=0``, which GCS guarantees only
one writer can win, and released with a match on the generation this run
created, so a run can never delete a lock someone else took. A lock older than
``stale_after`` belongs to a run that died without releasing it (SIGKILL,
lost instance) and is broken. ``stale_after`` must exceed the job's timeout in
``deploy/jobs.yaml``, or a slow-but-alive run could have its lock stolen.

Any host whose ``paths.searches_state_uri`` names the same prefix shares this
state and this lock, so a workstation (or the Slack bot's ``watch-searches
--reseed``) and the cloud job can never both run at once or diverge.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import signal
import socket
import tempfile
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..settings import Settings
from .gcs import parse_gs_uri
from .search_state import LOCK_FILENAME, STATE_FILENAME, SearchStateStore

logger = logging.getLogger(__name__)

# Longer than the watch-searches job timeout in deploy/jobs.yaml (900s), with
# room to spare: breaking a live run's lock would let two runs alert at once.
LOCK_STALE_AFTER = timedelta(minutes=20)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _content_type(name: str) -> str:
    return "application/json" if name.endswith((".json", ".jsonl")) else "text/plain"


def _upload_order(name: str) -> tuple[int, str]:
    """Seen-caches first, run state last.

    If an upload fails partway, the state that reaches GCS should never claim
    more than the caches back up: a ``search_state.json`` saying "seeded" next
    to a missing cache is detected and reseeded silently, whereas the reverse
    order could leave a cache claiming items the state has never heard of.
    """
    if name.endswith("_seen.jsonl"):
        return (0, name)
    if name == STATE_FILENAME:
        return (2, name)
    return (1, name)


class GCSSearchStateStore(SearchStateStore):
    """``SearchStateStore`` whose files are mirrored to ``gs://bucket/prefix``."""

    def __init__(
        self,
        uri: str,
        *,
        settings: Settings | None = None,
        bucket: Any = None,
        work_dir: Path | None = None,
        stale_after: timedelta = LOCK_STALE_AFTER,
    ) -> None:
        self.uri = uri.rstrip("/")
        self.bucket_name, self.prefix = parse_gs_uri(self.uri)

        if work_dir is None:
            work_dir = Path(tempfile.mkdtemp(prefix="shoebox-searches-"))
            # Only a directory we created is ours to remove.
            weakref.finalize(self, shutil.rmtree, work_dir, ignore_errors=True)
        super().__init__(base_dir=work_dir, settings=settings)

        self._bucket = bucket
        self.stale_after = stale_after
        # name -> digest of what GCS holds, as of the last pull/push.
        self._remote: dict[str, str] = {}
        self._lock_generation: int | None = None

    # ------------------------------------------------------------------
    # GCS plumbing
    # ------------------------------------------------------------------
    @property
    def bucket(self):
        # Built lazily, like flush_append_log's clients, so constructing the
        # store (and everything a test does with it) needs no credentials.
        if self._bucket is None:
            from .gcs import GCSClient

            self._bucket = GCSClient(self._settings).client.bucket(self.bucket_name)
        return self._bucket

    def _object_name(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def _local_files(self) -> dict[str, Path]:
        """State files in the scratch directory, excluding the lock and the
        ``.tmp`` files atomic rewrites leave behind for an instant."""
        return {
            p.name: p
            for p in self.base_dir.iterdir()
            if p.is_file() and p.name != LOCK_FILENAME and not p.name.endswith(".tmp")
        }

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------
    def pull(self) -> int:
        """Replace the scratch directory's contents with what GCS holds.

        Returns the number of files downloaded.
        """
        for path in self._local_files().values():
            path.unlink()

        list_prefix = f"{self.prefix}/" if self.prefix else ""
        lock_name = self._object_name(LOCK_FILENAME)
        self._remote = {}
        for blob in self.bucket.list_blobs(prefix=list_prefix):
            name = blob.name[len(list_prefix) :]
            # Flat layout: anything in a "subdirectory" is not ours (a manual
            # backup, say) and must not be deleted by the next push.
            if blob.name == lock_name or not name or "/" in name:
                continue
            target = self.base_dir / name
            blob.download_to_filename(str(target))
            self._remote[name] = _digest(target)

        logger.info("Loaded %d search state file(s) from %s", len(self._remote), self.uri)
        return len(self._remote)

    def push(self) -> tuple[int, int]:
        """Upload changed files and delete objects whose file is gone.

        Returns ``(uploaded, deleted)``. Raises on a GCS failure; whatever did
        upload is remembered, so a retry only sends what is still pending.
        """
        local = self._local_files()
        uploaded = deleted = 0

        for name in sorted(local, key=_upload_order):
            digest = _digest(local[name])
            if self._remote.get(name) == digest:
                continue
            self.bucket.blob(self._object_name(name)).upload_from_filename(
                str(local[name]), content_type=_content_type(name)
            )
            self._remote[name] = digest
            uploaded += 1

        for name in sorted(set(self._remote) - set(local)):
            self._delete_object(self._object_name(name))
            del self._remote[name]
            deleted += 1

        if uploaded or deleted:
            logger.info(
                "Saved search state to %s (%d uploaded, %d deleted)", self.uri, uploaded, deleted
            )
        return uploaded, deleted

    def _delete_object(self, object_name: str) -> None:
        from google.api_core.exceptions import NotFound

        try:
            self.bucket.blob(object_name).delete()
        except NotFound:
            pass

    def checkpoint(self) -> None:
        """Upload now. A failure is logged, not raised: the end of the run
        uploads again, and that one is allowed to fail loudly."""
        try:
            self.push()
        except Exception:
            logger.warning(
                "Could not checkpoint search state to %s; will retry at the end of the run",
                self.uri,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Locking
    # ------------------------------------------------------------------
    @contextmanager
    def lock(self) -> Iterator[bool]:
        """Take the GCS lock, pull, yield, push, release.

        Yields False without pulling when another run holds the lock.
        """
        if not self._acquire_lock():
            yield False
            return

        try:
            with _sigterm_as_exit():
                self.pull()
                try:
                    yield True
                finally:
                    # Uploaded even when the run raised: what is on disk is
                    # exactly what Slack has already been sent, so saving it is
                    # what prevents those alerts repeating.
                    self.push()
        finally:
            self._release_lock()

    def _lock_blob(self):
        return self.bucket.blob(self._object_name(LOCK_FILENAME))

    def _acquire_lock(self) -> bool:
        from google.api_core.exceptions import PreconditionFailed

        if self._try_create_lock():
            return True

        blob = self._lock_blob()
        try:
            blob.reload()
        except Exception:
            # Released between our create attempt and this read: one more try.
            return self._try_create_lock()

        held_for = datetime.now(UTC) - blob.time_created
        if held_for <= self.stale_after:
            return False

        logger.warning(
            "Breaking a search-state lock held for %s (> %s): %s",
            held_for,
            self.stale_after,
            _describe_lock(blob),
        )
        try:
            # Generation-matched, so if the holder released it and another run
            # took a fresh one in the meantime, that fresh lock survives.
            blob.delete(if_generation_match=blob.generation)
        except PreconditionFailed:
            return False
        except Exception:
            logger.warning("Could not break the stale lock", exc_info=True)
            return False
        return self._try_create_lock()

    def _try_create_lock(self) -> bool:
        from google.api_core.exceptions import PreconditionFailed

        holder = {
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "acquired_at": datetime.now(UTC).isoformat(),
            # Set by Cloud Run; empty elsewhere.
            "execution": os.environ.get("CLOUD_RUN_EXECUTION", ""),
        }
        blob = self._lock_blob()
        try:
            blob.upload_from_string(
                json.dumps(holder), content_type="application/json", if_generation_match=0
            )
        except PreconditionFailed:
            return False
        self._lock_generation = blob.generation
        return True

    def _release_lock(self) -> None:
        from google.api_core.exceptions import NotFound, PreconditionFailed

        generation, self._lock_generation = self._lock_generation, None
        try:
            self._lock_blob().delete(if_generation_match=generation)
        except (NotFound, PreconditionFailed):
            # Broken as stale by another run. Nothing of ours to remove, but it
            # means this run took longer than stale_after -- worth knowing.
            logger.warning("Search-state lock was no longer ours at release (%s)", self.uri)
        except Exception:
            logger.warning(
                "Could not release the search-state lock; it expires after %s",
                self.stale_after,
                exc_info=True,
            )


def _describe_lock(blob) -> str:
    try:
        return blob.download_as_text()
    except Exception:
        return "(holder unknown)"


@contextmanager
def _sigterm_as_exit() -> Iterator[None]:
    """Turn SIGTERM into SystemExit for the duration, so ``finally`` blocks run.

    Cloud Run sends SIGTERM when a task hits its timeout. Python's default is
    to die on the spot, which would skip the final upload and leave the lock
    held until it goes stale -- no searches at all for ``stale_after``. Signal
    handlers can only be installed from the main thread; anywhere else this is
    a no-op.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _handler(signum, frame):
        raise SystemExit(128 + signum)

    previous = signal.signal(signal.SIGTERM, _handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
