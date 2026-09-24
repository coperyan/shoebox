"""GCS-backed saved-search state: sync, the lock, and the watcher on top of it.

Everything runs against an in-memory fake bucket that enforces the two GCS
behaviours the store depends on -- ``ifGenerationMatch`` preconditions and
fresh generations on every write -- so the lock semantics are tested for real
rather than mocked away. Each "run" builds a new store with its own scratch
directory, which is what a Cloud Run execution looks like: nothing survives on
disk between runs, only in the bucket.
"""

from __future__ import annotations

import importlib.util
import itertools
import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from google.api_core.exceptions import NotFound, PreconditionFailed

from shoebox.clients import gcs
from shoebox.clients.search_state import (
    HITS_APPEND_FILENAME,
    STATE_FILENAME,
    SearchStateStore,
    open_search_state_store,
)
from shoebox.clients.search_state_gcs import GCSSearchStateStore
from shoebox.models.ebay.item_summary import ItemSummary
from shoebox.models.saved_search import load_searches_file
from shoebox.models.search_hit import SeenEntry
from shoebox.pipelines import watch_searches as ws
from shoebox.settings import PathSettings, get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
URI = "gs://bucket/searches/state"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------
# Fake GCS
# ----------------------------------------------------------------------
class FakeObject:
    def __init__(self, data: bytes, generation: int, time_created: datetime):
        self.data = data
        self.generation = generation
        self.time_created = time_created


class FakeBlob:
    def __init__(self, bucket: FakeBucket, name: str):
        self.bucket = bucket
        self.name = name
        self.generation: int | None = None
        self.time_created: datetime | None = None

    def _check(self, if_generation_match):
        current = self.bucket.objects.get(self.name)
        if if_generation_match is None:
            return
        if (current.generation if current else 0) != if_generation_match:
            raise PreconditionFailed(f"generation mismatch on {self.name}")

    def _write(self, data: bytes, if_generation_match=None):
        if self.bucket.fail_writes:
            raise RuntimeError("simulated GCS outage")
        self._check(if_generation_match)
        obj = FakeObject(data, next(self.bucket.generations), self.bucket.now())
        self.bucket.objects[self.name] = obj
        self.bucket.uploads.append(self.name)
        self.generation, self.time_created = obj.generation, obj.time_created

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        self._write(data.encode() if isinstance(data, str) else data, if_generation_match)

    def upload_from_filename(self, filename, content_type=None, if_generation_match=None):
        self._write(Path(filename).read_bytes(), if_generation_match)

    def _get(self) -> FakeObject:
        obj = self.bucket.objects.get(self.name)
        if obj is None:
            raise NotFound(self.name)
        return obj

    def download_to_filename(self, filename):
        if self.bucket.fail_reads:
            raise RuntimeError("simulated GCS outage")
        Path(filename).write_bytes(self._get().data)

    def download_as_text(self, encoding="utf-8"):
        return self._get().data.decode(encoding)

    def reload(self):
        obj = self._get()
        self.generation, self.time_created = obj.generation, obj.time_created

    def delete(self, if_generation_match=None):
        self._get()
        self._check(if_generation_match)
        del self.bucket.objects[self.name]
        self.bucket.deletes.append(self.name)


class FakeBucket:
    def __init__(self):
        self.objects: dict[str, FakeObject] = {}
        self.generations = itertools.count(1)
        self.uploads: list[str] = []
        self.deletes: list[str] = []
        self.fail_writes = False
        self.fail_reads = False
        self.clock = lambda: datetime.now(UTC)

    def now(self) -> datetime:
        return self.clock()

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)

    def list_blobs(self, prefix: str = ""):
        out = []
        for name in sorted(self.objects):
            if name.startswith(prefix):
                blob = FakeBlob(self, name)
                blob.reload()
                out.append(blob)
        return out

    def text(self, name: str) -> str:
        return self.objects[name].data.decode()

    def put(self, name: str, text: str, *, created: datetime | None = None) -> None:
        self.objects[name] = FakeObject(
            text.encode(), next(self.generations), created or self.now()
        )


@pytest.fixture
def bucket():
    return FakeBucket()


def make_store(bucket, tmp_path, **kw) -> GCSSearchStateStore:
    # A distinct scratch directory per store, like a fresh container.
    work = tmp_path / f"work{next(_work_ids)}"
    work.mkdir()
    return GCSSearchStateStore(URI, bucket=bucket, work_dir=work, **kw)


_work_ids = itertools.count()


def entry(item_id: str, *, name: str = "s1") -> SeenEntry:
    return SeenEntry(search_name=name, item_id=item_id, first_seen_at=NOW, last_seen_at=NOW)


# ----------------------------------------------------------------------
# URIs and config loading
# ----------------------------------------------------------------------
class TestGsUri:
    @pytest.mark.parametrize(
        "uri,expected",
        [
            ("gs://b/a/b.yaml", ("b", "a/b.yaml")),
            ("gs://b/prefix/", ("b", "prefix")),
            ("gs://b", ("b", "")),
        ],
    )
    def test_parse(self, uri, expected):
        assert gcs.parse_gs_uri(uri) == expected

    @pytest.mark.parametrize("uri", ["/local/path", "gs://", "gs:///x"])
    def test_rejects(self, uri):
        with pytest.raises(ValueError):
            gcs.parse_gs_uri(uri)

    def test_read_missing_object_is_file_not_found(self, bucket):
        client = type("C", (), {"client": type("S", (), {"bucket": lambda self, n: bucket})()})()
        with pytest.raises(FileNotFoundError, match="gs://bucket/nope.yaml"):
            gcs.read_gs_text("gs://bucket/nope.yaml", client=client)

    def test_read_object(self, bucket):
        bucket.put("cfg/searches.yaml", "version: 1\n")
        client = type("C", (), {"client": type("S", (), {"bucket": lambda self, n: bucket})()})()
        assert gcs.read_gs_text("gs://bucket/cfg/searches.yaml", client=client) == "version: 1\n"


class TestSearchesFileFromGcs:
    def test_loads_gs_uri(self, monkeypatch):
        doc = {"version": 1, "searches": [{"name": "s1", "query": "jordan"}]}
        seen = []

        def fake_read(uri, client=None):
            seen.append(uri)
            return yaml.safe_dump(doc)

        monkeypatch.setattr(gcs, "read_gs_text", fake_read)
        searches = load_searches_file("gs://bucket/config/searches.yaml").resolved()
        assert [s.name for s in searches] == ["s1"]
        assert seen == ["gs://bucket/config/searches.yaml"]

    def test_invalid_document_names_the_uri(self, monkeypatch):
        monkeypatch.setattr(gcs, "read_gs_text", lambda uri, client=None: "- a list\n")
        with pytest.raises(ValueError, match="gs://bucket/x.yaml"):
            load_searches_file("gs://bucket/x.yaml")


class TestSettings:
    def test_state_uri_defaults_to_local(self):
        assert get_settings().paths.searches_state_uri is None

    @pytest.mark.parametrize("value", ["exports/state", "gs://", "s3://bucket/x"])
    def test_non_gs_state_uri_rejected(self, value):
        base = get_settings().paths.model_dump()
        with pytest.raises(ValueError, match="searches_state_uri"):
            PathSettings(**{**base, "searches_state_uri": value})

    def test_empty_state_uri_means_local(self):
        base = get_settings().paths.model_dump()
        assert PathSettings(**{**base, "searches_state_uri": ""}).searches_state_uri is None

    def test_factory_picks_backend(self, monkeypatch, tmp_path):
        paths = get_settings().paths
        monkeypatch.setattr(paths, "exports_dir", str(tmp_path))
        assert type(open_search_state_store()) is SearchStateStore
        monkeypatch.setattr(paths, "searches_state_uri", URI)
        store = open_search_state_store()
        assert isinstance(store, GCSSearchStateStore)
        assert (store.bucket_name, store.prefix) == ("bucket", "searches/state")


# ----------------------------------------------------------------------
# Sync
# ----------------------------------------------------------------------
class TestSync:
    def test_state_survives_between_stores(self, bucket, tmp_path):
        first = make_store(bucket, tmp_path)
        with first.lock() as ok:
            assert ok
            first.append_seen("C1", [entry("a"), entry("b")])
            first.mark("s1", last_run_at=NOW, status="ok", seeded_at=NOW, seed_count=2)

        second = make_store(bucket, tmp_path)
        with second.lock() as ok:
            assert ok
            assert set(second.load_seen("C1")) == {"a", "b"}
            assert second.load_state()["s1"].seed_count == 2

    def test_objects_land_under_the_prefix(self, bucket, tmp_path):
        store = make_store(bucket, tmp_path)
        with store.lock():
            store.append_seen("C1", [entry("a")])
        assert set(bucket.objects) == {"searches/state/C1_seen.jsonl"}

    def test_unchanged_files_are_not_reuploaded(self, bucket, tmp_path):
        store = make_store(bucket, tmp_path)
        with store.lock():
            store.append_seen("C1", [entry("a")])
            store.checkpoint()
            bucket.uploads.clear()
            store.checkpoint()
        # Only the lock object was written after the first checkpoint.
        assert bucket.uploads == []

    def test_state_file_is_uploaded_after_the_caches(self, bucket, tmp_path):
        store = make_store(bucket, tmp_path)
        with store.lock():
            store.mark("s1", last_run_at=NOW, status="ok")
            store.append_seen("C1", [entry("a")])
            bucket.uploads.clear()
        uploads = [u for u in bucket.uploads if not u.endswith(".lock")]
        assert uploads == ["searches/state/C1_seen.jsonl", "searches/state/" + STATE_FILENAME]

    def test_removed_file_is_deleted_remotely(self, bucket, tmp_path):
        bucket.put(f"searches/state/{HITS_APPEND_FILENAME}", '{"x": 1}\n')
        store = make_store(bucket, tmp_path)
        with store.lock():
            assert store.hits_path.exists()
            store.clear_hits()  # what a successful flush does
        assert f"searches/state/{HITS_APPEND_FILENAME}" not in bucket.objects

    def test_foreign_objects_are_left_alone(self, bucket, tmp_path):
        bucket.put("searches/state/backup/C1_seen.jsonl", "old\n")
        bucket.put("searches/other.txt", "not ours\n")
        store = make_store(bucket, tmp_path)
        with store.lock():
            assert not (store.base_dir / "backup").exists()
            store.append_seen("C1", [entry("a")])
        assert "searches/state/backup/C1_seen.jsonl" in bucket.objects
        assert "searches/other.txt" in bucket.objects

    def test_pull_replaces_stale_scratch_files(self, bucket, tmp_path):
        store = make_store(bucket, tmp_path)
        (store.base_dir / "C9_seen.jsonl").write_text("left over\n")
        with store.lock():
            assert not (store.base_dir / "C9_seen.jsonl").exists()
        assert bucket.objects == {}

    def test_pushed_even_when_the_run_raises(self, bucket, tmp_path):
        store = make_store(bucket, tmp_path)
        with pytest.raises(RuntimeError), store.lock():
            store.append_seen("C1", [entry("a")])
            raise RuntimeError("boom")
        assert "searches/state/C1_seen.jsonl" in bucket.objects
        assert "searches/state/.lock" not in bucket.objects

    def test_failed_pull_never_pushes(self, bucket, tmp_path):
        bucket.put("searches/state/C1_seen.jsonl", "x\n")
        bucket.fail_reads = True
        store = make_store(bucket, tmp_path)
        with pytest.raises(RuntimeError), store.lock():
            pytest.fail("body must not run after a failed pull")
        # A push after a partial pull would delete what it failed to download.
        assert "searches/state/C1_seen.jsonl" in bucket.objects
        assert "searches/state/.lock" not in bucket.objects

    def test_checkpoint_failure_is_not_fatal(self, bucket, tmp_path):
        store = make_store(bucket, tmp_path)
        with store.lock():
            store.append_seen("C1", [entry("a")])
            bucket.fail_writes = True
            store.checkpoint()  # logged, not raised
            bucket.fail_writes = False
        assert "searches/state/C1_seen.jsonl" in bucket.objects

    def test_final_push_failure_is_raised_and_lock_released(self, bucket, tmp_path):
        store = make_store(bucket, tmp_path)
        with pytest.raises(RuntimeError, match="outage"), store.lock():
            store.append_seen("C1", [entry("a")])
            bucket.fail_writes = True
        bucket.fail_writes = False
        assert "searches/state/.lock" not in bucket.objects


# ----------------------------------------------------------------------
# Lock
# ----------------------------------------------------------------------
class TestLock:
    def test_second_holder_is_refused_without_pulling(self, bucket, tmp_path):
        bucket.put("searches/state/C1_seen.jsonl", "")
        a, b = make_store(bucket, tmp_path), make_store(bucket, tmp_path)
        with a.lock() as got_a:
            with b.lock() as got_b:
                assert got_a and not got_b
                assert list(b.base_dir.iterdir()) == []

    def test_released_lock_can_be_retaken(self, bucket, tmp_path):
        a, b = make_store(bucket, tmp_path), make_store(bucket, tmp_path)
        with a.lock():
            pass
        with b.lock() as ok:
            assert ok

    def test_stale_lock_is_broken(self, bucket, tmp_path):
        bucket.put(
            "searches/state/.lock",
            '{"host": "dead"}',
            created=datetime.now(UTC) - timedelta(hours=1),
        )
        with make_store(bucket, tmp_path, stale_after=timedelta(minutes=20)).lock() as ok:
            assert ok

    def test_fresh_lock_is_respected(self, bucket, tmp_path):
        bucket.put("searches/state/.lock", "{}", created=datetime.now(UTC) - timedelta(minutes=5))
        with make_store(bucket, tmp_path, stale_after=timedelta(minutes=20)).lock() as ok:
            assert not ok
        assert "searches/state/.lock" in bucket.objects

    def test_release_never_deletes_another_runs_lock(self, bucket, tmp_path):
        a = make_store(bucket, tmp_path)
        with a.lock():
            # A's lock is broken as stale and B takes a new one mid-run.
            del bucket.objects["searches/state/.lock"]
            bucket.put("searches/state/.lock", '{"host": "b"}')
        assert bucket.text("searches/state/.lock") == '{"host": "b"}'

    def test_lock_records_its_holder(self, bucket, tmp_path, monkeypatch):
        monkeypatch.setenv("CLOUD_RUN_EXECUTION", "watch-searches-abc12")
        store = make_store(bucket, tmp_path)
        with store.lock():
            assert "watch-searches-abc12" in bucket.text("searches/state/.lock")

    def test_sigterm_still_saves_and_releases(self, bucket, tmp_path):
        store = make_store(bucket, tmp_path)
        before = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit) as exc, store.lock():
            store.append_seen("C1", [entry("a")])
            os.kill(os.getpid(), signal.SIGTERM)
        assert exc.value.code == 128 + signal.SIGTERM
        assert "searches/state/C1_seen.jsonl" in bucket.objects
        assert "searches/state/.lock" not in bucket.objects
        assert signal.getsignal(signal.SIGTERM) is before


# ----------------------------------------------------------------------
# The watcher on GCS state
# ----------------------------------------------------------------------
def item(item_id: str, price: str = "25.00") -> ItemSummary:
    return ItemSummary(
        item_id=item_id,
        title=f"Michael Jordan PSA 10 #{item_id}",
        price={"value": price, "currency": "USD"},
        buying_options=["FIXED_PRICE"],
        item_web_url=f"https://ebay.com/itm/{item_id}",
        thumbnail_images=[{"image_url": f"https://i.ebayimg.com/images/g/{item_id}/s-l225.jpg"}],
    )


class Recorder:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, channel, text, thread_ts, unfurl_links, blocks=None) -> str:
        self.calls.append(text)
        return f"{len(self.calls)}.0"


@pytest.fixture
def searches_path(tmp_path):
    def _write(*names):
        doc = {
            "version": 1,
            "defaults": {"channel": "C0123456789", "interval": "15m"},
            "searches": [{"name": n, "query": "jordan"} for n in names],
        }
        path = tmp_path / "searches.yaml"
        path.write_text(yaml.safe_dump(doc))
        return path

    return _write


def run(bucket, tmp_path, searches, items, post, fetch=None, **kw):
    """One scheduled tick: a new container, so a new store."""
    return ws.watch_searches(
        config_path=searches,
        store=make_store(bucket, tmp_path),
        fetch=fetch or (lambda search, limit: list(items)),
        post=post,
        verify=lambda item_id: {},
        flush=False,
        force=True,
        pacing_seconds=0,
        **kw,
    )


class TestWatcherOnGcsState:
    def test_seed_then_quiet_then_one_new(self, bucket, tmp_path, searches_path):
        path = searches_path("s1")
        post = Recorder()

        run(bucket, tmp_path, path, [item("1"), item("2")], post)
        assert len(post.calls) == 1 and "seeded" in post.calls[0]

        run(bucket, tmp_path, path, [item("1"), item("2")], post)
        assert len(post.calls) == 1

        run(bucket, tmp_path, path, [item("3"), item("1"), item("2")], post)
        # Header plus the one new listing.
        assert len(post.calls) == 3
        assert "#3" in post.calls[-1]

        run(bucket, tmp_path, path, [item("3"), item("1"), item("2")], post)
        assert len(post.calls) == 3

    def test_each_search_is_checkpointed_before_the_next(self, bucket, tmp_path, searches_path):
        path = searches_path("s1", "s2")
        state_object = f"searches/state/{STATE_FILENAME}"
        observed: dict[str, bool] = {}

        def fetch(search, limit):
            if search.name == "s2":
                # s1 has finished: its state must already be in GCS.
                observed["s1_saved"] = state_object in bucket.objects and (
                    '"s1"' in bucket.text(state_object)
                )
            return [item("1")]

        run(bucket, tmp_path, path, [], Recorder(), fetch=fetch)
        assert observed == {"s1_saved": True}

    def test_flushed_buffer_is_cleared_remotely_before_the_run_ends(
        self, bucket, tmp_path, searches_path
    ):
        hits_object = f"searches/state/{HITS_APPEND_FILENAME}"
        store = make_store(bucket, tmp_path)
        observed: dict[str, bool] = {}

        def flush_then_die(**kw):
            store.clear_hits()
            return "logs/search_hits/x.jsonl"

        real_checkpoint = store.checkpoint

        def checkpoint():
            real_checkpoint()
            observed["buffer_gone"] = hits_object not in bucket.objects

        store.flush_append_log = flush_then_die
        store.checkpoint = checkpoint
        ws.watch_searches(
            config_path=searches_path("s1"),
            store=store,
            fetch=lambda search, limit: [item("1")],
            post=Recorder(),
            verify=lambda item_id: {},
            force=True,
            pacing_seconds=0,
        )
        assert observed == {"buffer_gone": True}

    def test_held_lock_skips_the_tick(self, bucket, tmp_path, searches_path):
        bucket.put("searches/state/.lock", "{}")
        post = Recorder()
        assert run(bucket, tmp_path, searches_path("s1"), [item("1")], post) == []
        assert post.calls == []

    def test_dry_run_uploads_nothing(self, bucket, tmp_path, searches_path):
        run(bucket, tmp_path, searches_path("s1"), [item("1")], Recorder(), dry_run=True)
        assert all(u.endswith(".lock") for u in bucket.uploads)
        assert bucket.objects == {}


class TestCloudRunGuard:
    def test_refuses_local_state_in_cloud_run(self, monkeypatch, tmp_path, searches_path):
        monkeypatch.setenv("CLOUD_RUN_JOB", "watch-searches")
        with pytest.raises(RuntimeError, match="searches_state_uri"):
            ws.watch_searches(config_path=searches_path("s1"), post=Recorder(), flush=False)

    def test_list_and_dry_run_are_allowed(self, monkeypatch, tmp_path, searches_path):
        monkeypatch.setenv("CLOUD_RUN_JOB", "watch-searches")
        monkeypatch.setattr(get_settings().paths, "exports_dir", str(tmp_path / "exports"))
        path = searches_path("s1")
        ws.watch_searches(config_path=path, list_only=True)
        ws.watch_searches(
            config_path=path,
            dry_run=True,
            fetch=lambda search, limit: [item("1")],
            post=Recorder(),
            flush=False,
        )

    def test_gcs_state_passes(self, monkeypatch):
        monkeypatch.setenv("CLOUD_RUN_JOB", "watch-searches")
        ws._require_durable_state_in_cloud(URI)

    def test_workstation_unaffected(self, monkeypatch):
        monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
        ws._require_durable_state_in_cloud(None)

    def test_git_pull_skipped_for_gs_file(self, monkeypatch):
        monkeypatch.setattr(get_settings().paths, "searches_git_pull", True)
        pulls = []
        monkeypatch.setattr(ws, "pull_searches_repo", lambda path: pulls.append(path))
        monkeypatch.setattr(
            gcs,
            "read_gs_text",
            lambda uri, client=None: yaml.safe_dump({"version": 1, "searches": []}),
        )
        ws.watch_searches(config_path="gs://bucket/config/searches.yaml", list_only=True)
        assert pulls == []


# ----------------------------------------------------------------------
# Scripts
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def validate_script():
    return _load_script("validate_searches")


@pytest.fixture(scope="module")
def migrate_script():
    return _load_script("migrate_search_state_to_gcs")


class TestValidateScript:
    @pytest.fixture
    def script(self, validate_script):
        return validate_script

    def test_example_is_valid(self, script, capsys):
        assert script.main([str(REPO_ROOT / "configs" / "searches.example.yml")]) == 0
        assert capsys.readouterr().out.startswith("OK ")

    def test_typo_is_invalid(self, script, tmp_path, capsys):
        path = tmp_path / "searches.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "searches": [{"name": "x", "intervl": 1}]}))
        assert script.main([str(path)]) == 1
        assert "intervl" in capsys.readouterr().err

    def test_missing_file_is_invalid(self, script, tmp_path):
        assert script.main([str(tmp_path / "nope.yaml")]) == 1


class TestMigrationScript:
    @pytest.fixture
    def script(self, migrate_script):
        return migrate_script

    @pytest.fixture
    def local(self, tmp_path):
        store = SearchStateStore(base_dir=tmp_path / "local")
        store.append_seen("C1", [entry("a"), entry("b")])
        store.mark("s1", last_run_at=NOW, status="ok", seeded_at=NOW, seed_count=2)
        (store.base_dir / "C1_seen.jsonl.premigration").write_text("old\n")
        with store.lock():  # leaves a .lock file behind, as a real run does
            pass
        return store

    def test_report_only_uploads_nothing(self, script, local, bucket, tmp_path):
        remote = make_store(bucket, tmp_path)
        assert script.migrate(local=local, remote=remote, apply=False, force=False) == 0
        assert bucket.objects == {}

    def test_apply_uploads_state_files_only(self, script, local, bucket, tmp_path):
        remote = make_store(bucket, tmp_path)
        assert script.migrate(local=local, remote=remote, apply=True, force=False) == 0
        assert set(bucket.objects) == {
            "searches/state/C1_seen.jsonl",
            f"searches/state/{STATE_FILENAME}",
        }
        # And a cloud run then continues from it rather than seeding.
        after = make_store(bucket, tmp_path)
        with after.lock():
            assert set(after.load_seen("C1")) == {"a", "b"}
            assert after.load_state()["s1"].seeded_at == NOW

    def test_refuses_to_overwrite_existing_state(self, script, local, bucket, tmp_path):
        bucket.put("searches/state/C2_seen.jsonl", "cloud\n")
        remote = make_store(bucket, tmp_path)
        assert script.migrate(local=local, remote=remote, apply=True, force=False) == 1
        assert set(bucket.objects) == {"searches/state/C2_seen.jsonl"}

    def test_force_mirrors_local_exactly(self, script, local, bucket, tmp_path):
        bucket.put("searches/state/C2_seen.jsonl", "cloud\n")
        remote = make_store(bucket, tmp_path)
        assert script.migrate(local=local, remote=remote, apply=True, force=True) == 0
        assert "searches/state/C2_seen.jsonl" not in bucket.objects
        assert "searches/state/C1_seen.jsonl" in bucket.objects

    def test_refuses_while_a_cloud_run_holds_the_lock(self, script, local, bucket, tmp_path):
        bucket.put("searches/state/.lock", "{}")
        remote = make_store(bucket, tmp_path)
        assert script.migrate(local=local, remote=remote, apply=True, force=False) == 1


class TestDeployConfig:
    def test_watch_searches_job(self):
        from shoebox.clients.search_state_gcs import LOCK_STALE_AFTER

        jobs = yaml.safe_load((REPO_ROOT / "deploy" / "jobs.yaml").read_text())["jobs"]
        job = next(j for j in jobs if j["name"] == "watch-searches")
        assert job["run"] == "watch-searches"
        assert job["schedule"] == "*/5 * * * *"
        # A job that outlives the lock's stale window could have it stolen.
        assert timedelta(seconds=int(job["timeout"].rstrip("s"))) < LOCK_STALE_AFTER

    def test_every_windows_daily_sync_has_a_cloud_job(self):
        tasks = yaml.safe_load((REPO_ROOT / "scripts" / "tasks.yaml").read_text())["tasks"]
        jobs = yaml.safe_load((REPO_ROOT / "deploy" / "jobs.yaml").read_text())["jobs"]
        cloud_runs = {j["run"] for j in jobs}
        windows_syncs = {
            t["run"]
            for t in tasks
            if isinstance(t.get("run"), str) and t["run"].startswith("sync-")
        }
        assert windows_syncs <= cloud_runs

    def test_searches_build_validates_before_uploading(self):
        build = yaml.safe_load((REPO_ROOT / "deploy" / "searches-cloudbuild.yaml").read_text())
        steps = {s["id"]: s for s in build["steps"]}
        assert steps["upload"]["waitFor"] == ["validate"]
        assert "validate_searches.py" in steps["validate"]["args"][-1]
        assert build["substitutions"]["_DEST_URI"] == ""
