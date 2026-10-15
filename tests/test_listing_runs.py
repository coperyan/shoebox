"""Run records in GCS: ids, store, claim race, reporter."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from google.api_core.exceptions import NotFound, PreconditionFailed

from shoebox.services.listing_runs import (
    ACTIVE_STALE_AFTER,
    ListingRun,
    ListingRunStore,
    RunRecordReporter,
    new_run_id,
    runs_prefix_for,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name
        self.generation = None

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        current = self.bucket.objects.get(self.name)
        if if_generation_match is not None:
            if (current[1] if current else 0) != if_generation_match:
                raise PreconditionFailed("generation mismatch")
        gen = next(self.bucket.generations)
        self.bucket.objects[self.name] = (data if isinstance(data, str) else data.decode(), gen)
        self.generation = gen
        self.bucket.writes.append(self.name)

    def download_as_text(self, encoding="utf-8"):
        if self.name not in self.bucket.objects:
            raise NotFound(self.name)
        return self.bucket.objects[self.name][0]

    def reload(self):
        if self.name not in self.bucket.objects:
            raise NotFound(self.name)
        self.generation = self.bucket.objects[self.name][1]


class FakeBucket:
    name = "ebay-bucket"

    def __init__(self):
        self.objects: dict[str, tuple[str, int]] = {}
        self.generations = itertools.count(1)
        self.writes: list[str] = []

    def blob(self, name):
        return FakeBlob(self, name)

    def list_blobs(self, prefix=""):
        return [SimpleNamespace(name=n) for n in self.objects if n.startswith(prefix)]


def _run(run_id="20261015T120000Z-aaaa0000", **over) -> ListingRun:
    base = dict(
        run_id=run_id,
        requested_at=NOW,
        publish=True,
        schedule=False,
        queue_uri="gs://ebay-bucket/queue/listing_queue_enriched.jsonl",
        rows_total=2,
    )
    base.update(over)
    return ListingRun(**base)


def test_run_ids_sort_by_time():
    a = new_run_id(NOW)
    b = new_run_id(NOW + timedelta(seconds=1))
    assert a.startswith("20261015T120000Z-") and len(a.split("-")[1]) == 8
    assert a < b


def test_runs_prefix_for():
    assert runs_prefix_for("gs://b/queue/listing_queue_enriched.jsonl") == "gs://b/queue/runs/"
    assert runs_prefix_for("gs://b/q.jsonl") == "gs://b/runs/"


def test_from_settings_requires_queue_uri():
    import pytest

    from shoebox.settings import Settings

    settings = Settings.model_construct(paths=SimpleNamespace(queue_uri=None))
    with pytest.raises(RuntimeError, match="queue_uri"):
        ListingRunStore.from_settings(settings, gcs=SimpleNamespace())


def test_write_read_list_latest_active():
    bucket = FakeBucket()
    store = ListingRunStore(bucket, "queue/runs/")
    assert store.uri == "gs://ebay-bucket/queue/runs/"
    assert store.read("nope") is None
    assert store.latest() is None and store.active() is None

    older = _run("20261015T110000Z-00000001", state="done")
    newer = _run("20261015T120000Z-00000002")
    store.write(older)
    gen = store.write(newer)
    assert gen == 2

    found = store.read(newer.run_id)
    assert found is not None and found[0] == newer and found[1] == 2
    assert [r.run_id for r in store.list()] == [newer.run_id, older.run_id]
    assert store.latest().run_id == newer.run_id
    assert store.active(now=NOW + timedelta(minutes=5)).run_id == newer.run_id
    # Stale request is not active any more.
    assert store.active(now=NOW + ACTIVE_STALE_AFTER + timedelta(seconds=1)) is None
    # A finished latest run is not active.
    newer.state = "failed"
    store.write(newer)
    assert store.active(now=NOW) is None


def test_claim_is_generation_matched(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "create-listings-abc12")
    bucket = FakeBucket()
    store = ListingRunStore(bucket, "queue/runs/")
    run = _run()
    gen = store.write(run)

    first, gen1 = store.read(run.run_id)
    second, gen2 = store.read(run.run_id)
    assert gen1 == gen2 == gen
    assert store.claim(first, gen1, now=NOW) is True
    assert first.state == "running" and first.started_at == NOW
    assert first.execution == "create-listings-abc12" and first.host
    assert store.claim(second, gen2, now=NOW) is False  # lost the race
    assert store.read(run.run_id)[0].state == "running"
    # Only requested runs can be claimed.
    done, g = store.read(run.run_id)
    assert store.claim(done, g) is False


def test_reporter_updates_record():
    bucket = FakeBucket()
    store = ListingRunStore(bucket, "queue/runs/")
    run = _run()
    clock = lambda: NOW + timedelta(minutes=3)  # noqa: E731
    rep = RunRecordReporter(store, run, clock=clock)
    rep.start(2)
    assert store.read(run.run_id)[0].rows_total == 2
    rep.row(sku="S1", title="t1", success=True, listing_id="123", offer_id="o1")
    rep.row(sku="S2", title="t2", success=False, error="boom")
    rec = store.read(run.run_id)[0]
    assert (rec.rows_done, rec.listed, rec.failed) == (2, 1, 1)
    assert rec.results[0].listing_id == "123" and rec.results[1].error == "boom"
    rep.finish()
    rec = store.read(run.run_id)[0]
    assert rec.state == "done" and rec.finished_at == NOW + timedelta(minutes=3)

    rep2 = RunRecordReporter(store, _run("20261015T130000Z-ffffffff"), clock=clock)
    rep2.fail("RuntimeError: nope")
    assert store.read("20261015T130000Z-ffffffff")[0].error == "RuntimeError: nope"
    assert store.read("20261015T130000Z-ffffffff")[0].state == "failed"


def test_summary_drops_results():
    run = _run()
    run.results.append(
        __import__("shoebox.services.listing_runs", fromlist=["ListingRunResult"]).ListingRunResult(
            sku="S", title="t", success=True
        )
    )
    s = run.summary()
    assert "results" not in s and s["run_id"] == run.run_id and s["requested_at"].startswith("2026")
