"""Tests for the GCS state mirror.

Everything runs against an in-memory fake that reproduces the two GCS
behaviours the mirror actually leans on: ``if_generation_match=0`` failing when
an object already exists (which is what makes lock acquisition atomic), and
generation-matched deletes failing once someone else has replaced the object.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed

from shoebox.clients.state_mirror import LOCK_OBJECT, GcsStateMirror

BUCKET = "test-bucket"
PREFIX = "state/searches"


# ----------------------------------------------------------------------
# Fake GCS
# ----------------------------------------------------------------------
class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.counter = 0


class FakeBlob:
    def __init__(self, store: FakeStore, name: str, metadata: dict | None = None) -> None:
        self._store = store
        self.name = name
        self.metadata = metadata

    @property
    def _record(self) -> dict | None:
        return self._store.objects.get(self.name)

    @property
    def generation(self) -> int | None:
        record = self._record
        return record["generation"] if record else None

    def upload_from_string(self, data, content_type=None, if_generation_match=None) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        record = self._record
        if if_generation_match is not None:
            current = record["generation"] if record else 0
            if current != if_generation_match:
                raise PreconditionFailed(f"generation {current} != {if_generation_match}")
        self._store.counter += 1
        self._store.objects[self.name] = {
            "data": data,
            "generation": self._store.counter,
            "metadata": dict(self.metadata or {}),
        }

    def download_as_bytes(self) -> bytes:
        record = self._record
        if record is None:
            raise NotFound(self.name)
        return record["data"]

    def download_to_filename(self, path) -> None:
        record = self._record
        if record is None:
            raise NotFound(self.name)
        Path(path).write_bytes(record["data"])

    def delete(self, if_generation_match=None) -> None:
        record = self._record
        if record is None:
            raise NotFound(self.name)
        if if_generation_match is not None and record["generation"] != if_generation_match:
            raise PreconditionFailed("generation mismatch")
        del self._store.objects[self.name]


class FakeBucket:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self._store, name)


class FakeClient:
    def __init__(self, store: FakeStore | None = None) -> None:
        self.store = store or FakeStore()

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.store)

    def list_blobs(self, bucket, prefix=""):
        return [
            FakeBlob(self.store, name, metadata=record["metadata"])
            for name, record in sorted(self.store.objects.items())
            if name.startswith(prefix)
        ]


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


def make_mirror(tmp_path: Path, store: FakeStore, **kwargs) -> GcsStateMirror:
    base = kwargs.pop("base_dir", tmp_path / "searches")
    return GcsStateMirror(
        bucket=BUCKET,
        prefix=PREFIX,
        base_dir=base,
        client=FakeClient(store),
        **kwargs,
    )


def put(store: FakeStore, filename: str, body: str) -> None:
    store.counter += 1
    store.objects[f"{PREFIX}/{filename}"] = {
        "data": body.encode("utf-8"),
        "generation": store.counter,
        "metadata": {},
    }


# ----------------------------------------------------------------------
# Download
# ----------------------------------------------------------------------
class TestDownload:
    def test_creates_dir_and_pulls_files(self, tmp_path, store):
        put(store, "search_state.json", '{"a": 1}')
        put(store, "C123_seen.jsonl", '{"item_id": "1"}\n')
        mirror = make_mirror(tmp_path, store)

        assert mirror.download() == 2
        assert (mirror.base_dir / "search_state.json").read_text() == '{"a": 1}'
        assert (mirror.base_dir / "C123_seen.jsonl").read_text() == '{"item_id": "1"}\n'

    def test_empty_remote_is_a_clean_first_run(self, tmp_path, store):
        mirror = make_mirror(tmp_path, store)
        assert mirror.download() == 0
        assert mirror.base_dir.is_dir()

    def test_local_only_files_are_dropped(self, tmp_path, store):
        """A reused warm container must not resurrect state the bucket lost."""
        mirror = make_mirror(tmp_path, store)
        mirror.base_dir.mkdir(parents=True)
        (mirror.base_dir / "stale_seen.jsonl").write_text("old")
        put(store, "search_state.json", "{}")

        mirror.download()
        assert not (mirror.base_dir / "stale_seen.jsonl").exists()
        assert (mirror.base_dir / "search_state.json").exists()

    def test_lock_object_is_never_mirrored_down(self, tmp_path, store):
        put(store, LOCK_OBJECT, '{"owner": "someone"}')
        put(store, "search_state.json", "{}")
        mirror = make_mirror(tmp_path, store)

        assert mirror.download() == 1
        assert not (mirror.base_dir / LOCK_OBJECT).exists()

    def test_local_lock_file_is_left_alone(self, tmp_path, store):
        """The advisory lock is host-local, so it is neither pushed nor pruned."""
        mirror = make_mirror(tmp_path, store)
        mirror.base_dir.mkdir(parents=True)
        (mirror.base_dir / ".lock").write_text("")

        mirror.download()
        assert (mirror.base_dir / ".lock").exists()


# ----------------------------------------------------------------------
# Upload
# ----------------------------------------------------------------------
class TestUpload:
    def test_pushes_state_files(self, tmp_path, store):
        mirror = make_mirror(tmp_path, store)
        mirror.base_dir.mkdir(parents=True)
        (mirror.base_dir / "search_state.json").write_text('{"a": 1}')

        assert mirror.upload() == 1
        assert store.objects[f"{PREFIX}/search_state.json"]["data"] == b'{"a": 1}'

    def test_skips_lock_and_tmp_files(self, tmp_path, store):
        mirror = make_mirror(tmp_path, store)
        mirror.base_dir.mkdir(parents=True)
        (mirror.base_dir / ".lock").write_text("")
        (mirror.base_dir / "search_state.tmp").write_text("half-written")
        (mirror.base_dir / "search_state.json").write_text("{}")

        assert mirror.upload() == 1
        assert set(store.objects) == {f"{PREFIX}/search_state.json"}

    def test_unchanged_files_are_not_rewritten(self, tmp_path, store):
        """A tick where nothing was due must not rewrite a multi-MB seen cache."""
        mirror = make_mirror(tmp_path, store)
        mirror.base_dir.mkdir(parents=True)
        (mirror.base_dir / "search_state.json").write_text("{}")

        assert mirror.upload() == 1
        generation = store.objects[f"{PREFIX}/search_state.json"]["generation"]
        assert mirror.upload() == 0
        assert store.objects[f"{PREFIX}/search_state.json"]["generation"] == generation

    def test_changed_file_is_rewritten(self, tmp_path, store):
        mirror = make_mirror(tmp_path, store)
        mirror.base_dir.mkdir(parents=True)
        path = mirror.base_dir / "search_state.json"
        path.write_text("{}")
        mirror.upload()

        path.write_text('{"changed": true}')
        assert mirror.upload() == 1
        assert store.objects[f"{PREFIX}/search_state.json"]["data"] == b'{"changed": true}'

    def test_remote_file_deleted_locally_is_removed(self, tmp_path, store):
        """clear_seen unlinks a cache; the mirror must propagate that."""
        put(store, "gone_seen.jsonl", "stale")
        mirror = make_mirror(tmp_path, store)
        mirror.base_dir.mkdir(parents=True)
        (mirror.base_dir / "search_state.json").write_text("{}")

        mirror.upload()
        assert f"{PREFIX}/gone_seen.jsonl" not in store.objects

    def test_missing_dir_is_a_noop(self, tmp_path, store):
        assert make_mirror(tmp_path, store).upload() == 0


# ----------------------------------------------------------------------
# Lock
# ----------------------------------------------------------------------
class TestLock:
    def test_acquire_on_free_lock(self, tmp_path, store):
        assert make_mirror(tmp_path, store).acquire() is True
        assert f"{PREFIX}/{LOCK_OBJECT}" in store.objects

    def test_second_holder_is_refused(self, tmp_path, store):
        first = make_mirror(tmp_path, store)
        second = make_mirror(tmp_path, store, base_dir=tmp_path / "other")
        assert first.acquire() is True
        assert second.acquire() is False

    def test_release_frees_it(self, tmp_path, store):
        first = make_mirror(tmp_path, store)
        second = make_mirror(tmp_path, store, base_dir=tmp_path / "other")
        first.acquire()
        first.release()
        assert second.acquire() is True

    def test_expired_lock_is_stolen(self, tmp_path, store):
        stale = json.dumps(
            {
                "owner": "dead-host/1/abcd",
                "acquired_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                "expires_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            }
        )
        put(store, LOCK_OBJECT, stale)

        assert make_mirror(tmp_path, store).acquire() is True

    def test_unparseable_lock_is_stolen(self, tmp_path, store):
        put(store, LOCK_OBJECT, "not json at all")
        assert make_mirror(tmp_path, store).acquire() is True

    def test_lock_without_expiry_is_stolen(self, tmp_path, store):
        put(store, LOCK_OBJECT, json.dumps({"owner": "x"}))
        assert make_mirror(tmp_path, store).acquire() is True

    def test_release_leaves_a_stolen_lock_alone(self, tmp_path, store):
        """A run whose lease lapsed must not delete its successor's lock."""
        first = make_mirror(tmp_path, store, lock_ttl_seconds=-1)
        first.acquire()
        second = make_mirror(tmp_path, store, base_dir=tmp_path / "other")
        assert second.acquire() is True

        held = store.objects[f"{PREFIX}/{LOCK_OBJECT}"]["generation"]
        first.release()
        assert store.objects[f"{PREFIX}/{LOCK_OBJECT}"]["generation"] == held

    def test_release_without_a_lock_is_not_fatal(self, tmp_path, store):
        make_mirror(tmp_path, store).release()


# ----------------------------------------------------------------------
# Session
# ----------------------------------------------------------------------
class TestSession:
    def test_round_trip_across_hosts(self, tmp_path, store):
        """What one execution writes, the next execution sees."""
        first = make_mirror(tmp_path, store, base_dir=tmp_path / "run1")
        with first.session() as acquired:
            assert acquired
            (first.base_dir / "search_state.json").write_text('{"jordan": "seeded"}')

        second = make_mirror(tmp_path, store, base_dir=tmp_path / "run2")
        with second.session() as acquired:
            assert acquired
            assert (second.base_dir / "search_state.json").read_text() == '{"jordan": "seeded"}'

    def test_lock_is_released_after_the_body(self, tmp_path, store):
        mirror = make_mirror(tmp_path, store)
        with mirror.session():
            pass
        assert f"{PREFIX}/{LOCK_OBJECT}" not in store.objects

    def test_busy_yields_false_and_does_not_touch_state(self, tmp_path, store):
        holder = make_mirror(tmp_path, store)
        holder.acquire()
        put(store, "search_state.json", '{"remote": true}')

        blocked = make_mirror(tmp_path, store, base_dir=tmp_path / "blocked")
        with blocked.session() as acquired:
            assert acquired is False
        assert not (blocked.base_dir / "search_state.json").exists()
        assert store.objects[f"{PREFIX}/{LOCK_OBJECT}"]["generation"] is not None

    def test_state_is_pushed_even_when_the_body_raises(self, tmp_path, store):
        """Items already alerted on must not be rolled back by a later crash."""
        mirror = make_mirror(tmp_path, store)
        with pytest.raises(RuntimeError):
            with mirror.session():
                (mirror.base_dir / "C123_seen.jsonl").write_text('{"item_id": "1"}\n')
                raise RuntimeError("eBay blew up mid-run")

        assert store.objects[f"{PREFIX}/C123_seen.jsonl"]["data"] == b'{"item_id": "1"}\n'
        assert f"{PREFIX}/{LOCK_OBJECT}" not in store.objects

    def test_upload_failure_still_releases_the_lock(self, tmp_path, store, monkeypatch):
        """A wedged lock would stop every future tick, so releasing wins."""
        mirror = make_mirror(tmp_path, store)
        monkeypatch.setattr(
            mirror, "upload", lambda: (_ for _ in ()).throw(RuntimeError("GCS down"))
        )
        with mirror.session() as acquired:
            assert acquired
        assert f"{PREFIX}/{LOCK_OBJECT}" not in store.objects


# ----------------------------------------------------------------------
# The watch-searches seam
# ----------------------------------------------------------------------
class TestStateSession:
    """``_state_session`` is what decides whether a run is local or mirrored."""

    def _settings(self):
        from shoebox.settings import get_settings

        return get_settings()

    def test_disabled_uses_the_file_lock_only(self, tmp_path, monkeypatch):
        """The default path must behave exactly as it did before mirroring."""
        from shoebox.clients.search_state import SearchStateStore
        from shoebox.pipelines.watch_searches import _state_session

        settings = self._settings()
        monkeypatch.setattr(settings.state_sync, "enabled", False)
        store = SearchStateStore(base_dir=tmp_path / "searches")

        with _state_session(settings, store) as acquired:
            assert acquired is True
            # Second holder on the same host is refused by the advisory lock.
            other = SearchStateStore(base_dir=store.base_dir)
            with _state_session(settings, other) as second:
                assert second is False

    def test_disabled_never_builds_a_mirror(self, tmp_path, monkeypatch):
        import shoebox.clients.state_mirror as mirror_module
        from shoebox.clients.search_state import SearchStateStore
        from shoebox.pipelines.watch_searches import _state_session

        def explode(*args, **kwargs):
            raise AssertionError("local runs must not touch GCS")

        monkeypatch.setattr(mirror_module, "GcsStateMirror", explode)
        settings = self._settings()
        monkeypatch.setattr(settings.state_sync, "enabled", False)

        with _state_session(settings, SearchStateStore(base_dir=tmp_path / "s")) as acquired:
            assert acquired is True

    def test_enabled_mirrors_around_the_run(self, tmp_path, monkeypatch, store):
        import shoebox.clients.state_mirror as mirror_module
        from shoebox.clients.search_state import SearchStateStore
        from shoebox.pipelines.watch_searches import _state_session

        built: dict = {}
        real = mirror_module.GcsStateMirror

        def factory(**kwargs):
            built.update(kwargs)
            return real(client=FakeClient(store), **kwargs)

        monkeypatch.setattr(mirror_module, "GcsStateMirror", factory)
        settings = self._settings()
        monkeypatch.setattr(settings.state_sync, "enabled", True)
        monkeypatch.setattr(settings.state_sync, "bucket", "")

        put(store, "search_state.json", '{"seeded": true}')
        state_store = SearchStateStore(base_dir=tmp_path / "searches")

        with _state_session(settings, state_store) as acquired:
            assert acquired is True
            # State arrived from the bucket before the run started.
            assert (state_store.base_dir / "search_state.json").exists()
            (state_store.base_dir / "search_state.json").write_text('{"seeded": "again"}')

        # A blank bucket setting falls back to the watcher's existing log bucket.
        assert built["bucket"] == settings.gcs.ebay_bucket
        assert built["prefix"] == settings.state_sync.prefix
        assert store.objects[f"{PREFIX}/search_state.json"]["data"] == b'{"seeded": "again"}'

    def test_enabled_but_busy_yields_false(self, tmp_path, monkeypatch, store):
        import shoebox.clients.state_mirror as mirror_module
        from shoebox.clients.search_state import SearchStateStore
        from shoebox.pipelines.watch_searches import _state_session

        real = mirror_module.GcsStateMirror
        monkeypatch.setattr(
            mirror_module,
            "GcsStateMirror",
            lambda **kwargs: real(client=FakeClient(store), **kwargs),
        )
        settings = self._settings()
        monkeypatch.setattr(settings.state_sync, "enabled", True)

        holder = make_mirror(tmp_path, store, base_dir=tmp_path / "holder")
        holder.acquire()

        with _state_session(settings, SearchStateStore(base_dir=tmp_path / "s")) as acquired:
            assert acquired is False
