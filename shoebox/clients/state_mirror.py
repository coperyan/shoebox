"""Mirror the saved-search state directory to GCS so it survives a stateless host.

``SearchStateStore`` keeps its seen-cache, run state, and hit buffer as files
under ``exports/jsonl/searches/`` and guards overlapping runs with an advisory
file lock. Both assumptions hold on a long-lived host and break on a container
scheduler, where every execution gets an empty filesystem and no two executions
share one:

* **Lost state.** A fresh container has no seen-cache, so every search looks
  unseeded. ``SearchStateStore.cache_was_lost`` notices and re-seeds *silently*
  rather than alerting, which turns the watcher into a no-op that reports
  success -- the worst possible failure mode for an alerting system.
* **A meaningless lock.** ``flock``/``msvcrt`` coordinate processes sharing a
  filesystem. Two container executions share nothing, so the local lock cannot
  see the other run at all.

This module fixes both without touching ``SearchStateStore``: it syncs the
directory around a run and holds a lock that lives in the same bucket as the
state, so it is visible to every execution regardless of host.

The local store stays the single implementation of the state format. That is
deliberate -- it is the part with the subtle semantics (last-line-wins seen
entries, compaction ratios, seed-count loss detection) and it is covered by
tests that run against a plain ``tmp_path``. Mirroring a directory is a much
smaller thing to get right than reimplementing that logic against blobs.

Nothing here runs unless ``state_sync.enabled`` is set, so a Mac or Windows
checkout keeps using the filesystem exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from google.api_core.exceptions import NotFound, PreconditionFailed

logger = logging.getLogger(__name__)

LOCK_OBJECT = ".lock.json"

# Never mirrored: the advisory lock is host-local by definition, and .tmp files
# are the in-flight half of SearchStateStore's atomic replaces.
_EXCLUDED_NAMES = {".lock"}
_EXCLUDED_SUFFIXES = {".tmp"}


def _mirrorable(path: Path) -> bool:
    return (
        path.is_file()
        and path.name not in _EXCLUDED_NAMES
        and path.suffix not in _EXCLUDED_SUFFIXES
    )


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _owner_id() -> str:
    """Identify the holder well enough to debug a stuck lock from the console."""
    return f"{socket.gethostname()}/{os.getpid()}/{uuid.uuid4().hex[:8]}"


class GcsStateMirror:
    """Sync a state directory to a GCS prefix, with a cross-host lock.

    The lock is a single object created with ``if_generation_match=0`` -- GCS
    fails the write if the object already exists, which makes creation an atomic
    test-and-set. A crashed run would otherwise wedge the schedule forever, so
    the object carries an expiry and a later run may steal it once it lapses.
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        base_dir: Path,
        lock_ttl_seconds: int = 900,
        client=None,
    ) -> None:
        self.bucket_name = bucket
        self.prefix = prefix.strip("/")
        self.base_dir = Path(base_dir)
        self.lock_ttl = timedelta(seconds=lock_ttl_seconds)
        self._client = client
        self._lock_generation: int | None = None

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            # Imported lazily so importing this module needs no GCP credentials,
            # matching how SearchStateStore defers its own cloud clients.
            from .gcs import GCSClient

            self._client = GCSClient().client
        return self._client

    def _bucket(self):
        return self.client.bucket(self.bucket_name)

    def _object_name(self, filename: str) -> str:
        return f"{self.prefix}/{filename}" if self.prefix else filename

    def _remote_names(self) -> dict[str, object]:
        """Mirrored blobs by bare filename, excluding the lock object."""
        listed = self.client.list_blobs(self.bucket_name, prefix=f"{self.prefix}/")
        found: dict[str, object] = {}
        for blob in listed:
            name = blob.name.rsplit("/", 1)[-1]
            if name == LOCK_OBJECT or not name:
                continue
            found[name] = blob
        return found

    # ------------------------------------------------------------------
    # Lock
    # ------------------------------------------------------------------
    def _read_lock(self):
        blob = self._bucket().blob(self._object_name(LOCK_OBJECT))
        try:
            raw = blob.download_as_bytes()
        except NotFound:
            return None, None
        try:
            return json.loads(raw.decode("utf-8")), blob
        except (ValueError, UnicodeDecodeError):
            # A truncated or hand-edited lock is treated as expired rather than
            # fatal: refusing to run forever is worse than stealing it.
            logger.warning("Unreadable state lock object; treating it as stale")
            return {}, blob

    def _expired(self, payload: dict | None) -> bool:
        if not payload:
            return True
        raw = payload.get("expires_at")
        if not isinstance(raw, str):
            return True
        try:
            expires = datetime.fromisoformat(raw)
        except ValueError:
            return True
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return datetime.now(UTC) >= expires

    def _try_create_lock(self, owner: str) -> bool:
        payload = json.dumps(
            {
                "owner": owner,
                "acquired_at": datetime.now(UTC).isoformat(),
                "expires_at": (datetime.now(UTC) + self.lock_ttl).isoformat(),
            }
        )
        blob = self._bucket().blob(self._object_name(LOCK_OBJECT))
        try:
            blob.upload_from_string(payload, content_type="application/json", if_generation_match=0)
        except PreconditionFailed:
            return False
        self._lock_generation = getattr(blob, "generation", None)
        return True

    def acquire(self, owner: str | None = None) -> bool:
        """Take the lock, stealing it once if the holder's lease has expired."""
        owner = owner or _owner_id()
        if self._try_create_lock(owner):
            return True

        payload, blob = self._read_lock()
        if blob is None:
            # Vanished between the failed create and the read; one more attempt.
            return self._try_create_lock(owner)
        if not self._expired(payload):
            logger.warning(
                "Another run holds the state lock (owner=%s, expires=%s); skipping",
                (payload or {}).get("owner", "?"),
                (payload or {}).get("expires_at", "?"),
            )
            return False

        logger.warning(
            "Stealing expired state lock (owner=%s, expired=%s)",
            (payload or {}).get("owner", "?"),
            (payload or {}).get("expires_at", "?"),
        )
        try:
            blob.delete(if_generation_match=getattr(blob, "generation", None))
        except (NotFound, PreconditionFailed):
            # Someone else stole it first; whoever won now owns the lease.
            pass
        return self._try_create_lock(owner)

    def release(self) -> None:
        """Drop the lock, but only if this run still owns that generation.

        The generation check is what stops a run whose lease expired mid-flight
        from deleting the lock a *different* execution has since taken.
        """
        blob = self._bucket().blob(self._object_name(LOCK_OBJECT))
        try:
            blob.delete(if_generation_match=self._lock_generation)
        except (NotFound, PreconditionFailed):
            logger.warning("State lock was already released or taken over; leaving it alone")
        finally:
            self._lock_generation = None

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------
    def download(self) -> int:
        """Replace the local state directory with the mirrored copy.

        Local-only files are deleted: the remote copy is authoritative, and a
        reused warm container must not resurrect state the bucket no longer has.
        """
        self.base_dir.mkdir(parents=True, exist_ok=True)
        remote = self._remote_names()

        for path in self.base_dir.iterdir():
            if _mirrorable(path) and path.name not in remote:
                logger.info("Dropping local state file with no remote copy: %s", path.name)
                path.unlink()

        for name, blob in remote.items():
            blob.download_to_filename(str(self.base_dir / name))
        logger.info(
            "Pulled %d state file(s) from gs://%s/%s",
            len(remote),
            self.bucket_name,
            self.prefix,
        )
        return len(remote)

    def upload(self) -> int:
        """Push local state back, skipping files whose content is unchanged.

        Unchanged files are the common case -- a tick where nothing was due
        rewrites nothing -- and a seen-cache can reach a few MB, so re-uploading
        every file on every five-minute run would be pure waste.
        """
        if not self.base_dir.exists():
            return 0

        remote = self._remote_names()
        written = 0
        local_names = set()

        for path in sorted(self.base_dir.iterdir()):
            if not _mirrorable(path):
                continue
            local_names.add(path.name)
            data = path.read_bytes()
            existing = remote.get(path.name)
            if existing is not None and self._unchanged(existing, data):
                continue
            blob = self._bucket().blob(self._object_name(path.name))
            blob.metadata = {"sha256": _digest(data)}
            blob.upload_from_string(data, content_type="application/json")
            written += 1

        for name in remote:
            if name not in local_names:
                try:
                    self._bucket().blob(self._object_name(name)).delete()
                except NotFound:
                    pass

        logger.info(
            "Pushed %d changed state file(s) to gs://%s/%s",
            written,
            self.bucket_name,
            self.prefix,
        )
        return written

    def _unchanged(self, blob, data: bytes) -> bool:
        metadata = getattr(blob, "metadata", None) or {}
        return metadata.get("sha256") == _digest(data)

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------
    @contextmanager
    def session(self) -> Iterator[bool]:
        """Lock, pull state, run, push state, unlock.

        Yields False when another execution holds the lock, matching
        ``SearchStateStore.lock`` so the caller handles both the same way.

        State is pushed even when the body raises. That is the same bargain the
        watcher already makes locally, where state is committed to disk per item
        as it goes: a crash mid-run must not roll back the items it already
        alerted on, or they alert again next tick.
        """
        if not self.acquire():
            yield False
            return
        try:
            self.download()
            yield True
        finally:
            try:
                self.upload()
            except Exception:
                logger.exception(
                    "Failed to push search state back to GCS; the next run will "
                    "re-use the last mirrored copy and may re-alert recent items"
                )
            self.release()
