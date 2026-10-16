"""Listing runs started from the iOS app: the record in GCS that ties the
API, the Cloud Run job and the phone together.

``POST /queue/listings`` writes ``<queue dir>/runs/<run_id>.json`` in state
``requested`` and starts the ``create-listings`` job. The job
(``create-listings --run-request``) claims the newest requested record with a
generation-matched write (so a second execution cannot run the same queue
twice), reads publish/schedule (and the scans folder) from it, and updates it after every row. The
app polls ``GET /queue/listings/<run_id>``.

The run id sorts by time (``<UTC timestamp>-<8 hex>``), which is what makes
"the newest record" a prefix listing plus a sort.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from google.api_core.exceptions import NotFound, PreconditionFailed
from pydantic import BaseModel, Field

from shoebox.clients.gcs import parse_gs_uri
from shoebox.settings import Settings

logger = logging.getLogger(__name__)

RUNS_SUBDIR = "runs"
# A run still "active" after this long is a crash or a timeout the record
# never saw. Keep above the create-listings job timeout (3600s in jobs.yaml).
ACTIVE_STALE_AFTER = timedelta(minutes=70)
LIST_LIMIT = 20

RunState = Literal["requested", "running", "done", "failed"]


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_run_id(now: datetime | None = None) -> str:
    now = now or utc_now()
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def runs_prefix_for(queue_uri: str) -> str:
    """``gs://b/queue/listing_queue_enriched.jsonl`` -> ``gs://b/queue/runs/``."""
    bucket, name = parse_gs_uri(queue_uri)
    directory = name.rsplit("/", 1)[0] + "/" if "/" in name else ""
    return f"gs://{bucket}/{directory}{RUNS_SUBDIR}/"


class ListingRunResult(BaseModel):
    sku: str
    title: str
    listing_id: str | None = None
    offer_id: str | None = None
    success: bool
    error: str | None = None


class ListingRun(BaseModel):
    run_id: str
    state: RunState = "requested"
    requested_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    publish: bool = True
    schedule: bool = True
    queue_uri: str
    # OneDrive folder the app asked to read scans from; None is the
    # configured onedrive.scans_folder.
    scans_folder: str | None = None
    metadata_version: str | None = None
    rows_total: int = 0
    rows_done: int = 0
    listed: int = 0
    failed: int = 0
    results: list[ListingRunResult] = Field(default_factory=list)
    error: str | None = None
    execution: str | None = None
    host: str | None = None

    @property
    def is_active(self) -> bool:
        return self.state in ("requested", "running")

    def summary(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"results"})


class ListingRunStore:
    """Run records under ``prefix`` in one bucket.

    ``bucket`` is a ``google.cloud.storage.Bucket`` (or a fake with ``blob()``
    and ``list_blobs(prefix=)``). Writes that must not race use
    ``if_generation_match``.
    """

    def __init__(self, bucket: Any, prefix: str):
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    @classmethod
    def from_settings(cls, settings: Settings, *, gcs: Any) -> ListingRunStore:
        if not settings.paths.queue_uri:
            raise RuntimeError("paths.queue_uri is not set; listing runs need the queue in GCS")
        bucket_name, prefix = parse_gs_uri(runs_prefix_for(settings.paths.queue_uri))
        return cls(gcs.client.bucket(bucket_name), prefix)

    @property
    def uri(self) -> str:
        return f"gs://{self.bucket.name}/{self.prefix}"

    def _name(self, run_id: str) -> str:
        return f"{self.prefix}{run_id}.json"

    # ---- reads ----

    def read(self, run_id: str) -> tuple[ListingRun, int] | None:
        blob = self.bucket.blob(self._name(run_id))
        try:
            text = blob.download_as_text(encoding="utf-8")
            blob.reload()
        except NotFound:
            return None
        return ListingRun.model_validate_json(text), int(blob.generation or 0)

    def list(self, limit: int = LIST_LIMIT) -> list[ListingRun]:
        names = sorted(
            (
                b.name
                for b in self.bucket.list_blobs(prefix=self.prefix)
                if b.name.endswith(".json")
            ),
            reverse=True,
        )
        runs: list[ListingRun] = []
        for name in names[:limit]:
            run_id = name[len(self.prefix) : -len(".json")]
            found = self.read(run_id)
            if found:
                runs.append(found[0])
        return runs

    def latest(self) -> ListingRun | None:
        runs = self.list(limit=1)
        return runs[0] if runs else None

    def active(self, now: datetime | None = None) -> ListingRun | None:
        """The newest run that is still requested/running and not stale."""
        run = self.latest()
        if run is None or not run.is_active:
            return None
        now = now or utc_now()
        if now - run.requested_at > ACTIVE_STALE_AFTER:
            return None
        return run

    # ---- writes ----

    def write(self, run: ListingRun, *, if_generation_match: int | None = None) -> int:
        blob = self.bucket.blob(self._name(run.run_id))
        blob.upload_from_string(
            run.model_dump_json(),
            content_type="application/json",
            if_generation_match=if_generation_match,
        )
        return int(blob.generation or 0)

    def claim(self, run: ListingRun, generation: int, *, now: datetime | None = None) -> bool:
        """Move a requested run to running; False if someone else got there first."""
        if run.state != "requested":
            return False
        run.state = "running"
        run.started_at = now or utc_now()
        run.host = socket.gethostname()
        run.execution = os.environ.get("CLOUD_RUN_EXECUTION") or run.execution
        try:
            self.write(run, if_generation_match=generation)
        except PreconditionFailed:
            logger.warning("Run %s was claimed by another execution", run.run_id)
            return False
        return True


class RunRecordReporter:
    """``run_listings`` progress hook that keeps the record current."""

    def __init__(self, store: ListingRunStore, run: ListingRun, *, clock=utc_now):
        self.store = store
        self.run = run
        self.clock = clock

    def start(self, total: int) -> None:
        self.run.rows_total = total
        self.run.rows_done = 0
        self.store.write(self.run)

    def row(
        self,
        *,
        sku: str,
        title: str,
        success: bool,
        listing_id: str | None = None,
        offer_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self.run.results.append(
            ListingRunResult(
                sku=sku,
                title=title,
                listing_id=listing_id,
                offer_id=offer_id,
                success=success,
                error=error,
            )
        )
        self.run.rows_done += 1
        if success:
            self.run.listed += 1
        else:
            self.run.failed += 1
        self.store.write(self.run)

    def finish(self) -> None:
        self.run.state = "done"
        self.run.finished_at = self.clock()
        self.store.write(self.run)

    def fail(self, error: str) -> None:
        self.run.state = "failed"
        self.run.error = error
        self.run.finished_at = self.clock()
        self.store.write(self.run)


def loads_run(text: str) -> ListingRun:
    return ListingRun.model_validate(json.loads(text))
