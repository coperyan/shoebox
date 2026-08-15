import importlib.util
import sys
import types
from datetime import UTC, datetime, timedelta

from shoebox.clients import search_state
from shoebox.clients.search_state import SearchRunState, SearchStateStore
from shoebox.models.saved_search import SearchesFile
from shoebox.models.search_hit import SearchHit, SeenEntry, build_cache_key

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def resolve(**overrides):
    search = {"name": "s1", "query": "jordan"}
    search.update(overrides)
    return SearchesFile(version=1, searches=[search]).resolved()[0]


def store(tmp_path) -> SearchStateStore:
    return SearchStateStore(base_dir=tmp_path / "searches")


def entry(item_id: str, *, name: str = "s1", last_seen_at: datetime = NOW, **kw) -> SeenEntry:
    return SeenEntry(
        search_name=name, item_id=item_id, first_seen_at=NOW, last_seen_at=last_seen_at, **kw
    )


class TestCacheKey:
    def test_uses_unit_separator_not_pipe(self):
        # eBay item ids are themselves pipe-delimited, so '|' would be ambiguous.
        key = build_cache_key("s1", "v1|123|0")
        assert key == "s1\x1fv1|123|0"
        assert key.split("\x1f") == ["s1", "v1|123|0"]


class TestSeenCache:
    def test_missing_file_is_empty(self, tmp_path):
        assert store(tmp_path).load_seen("s1") == {}

    def test_append_then_reload(self, tmp_path):
        s = store(tmp_path)
        s.append_seen("s1", [entry("a"), entry("b")])
        assert set(s.load_seen("s1")) == {"a", "b"}

    def test_appends_accumulate_across_calls(self, tmp_path):
        s = store(tmp_path)
        s.append_seen("s1", [entry("a")])
        s.append_seen("s1", [entry("b")])
        assert set(s.load_seen("s1")) == {"a", "b"}

    def test_later_line_wins(self, tmp_path):
        s = store(tmp_path)
        s.append_seen("s1", [entry("a", last_price=10)])
        s.append_seen("s1", [entry("a", last_price=20)])
        seen = s.load_seen("s1")
        assert len(seen) == 1
        assert seen["a"].last_price == 20

    def test_corrupt_line_is_skipped_not_fatal(self, tmp_path):
        s = store(tmp_path)
        s.append_seen("s1", [entry("a")])
        with s.seen_path("s1").open("a") as fh:
            fh.write("{not json\n")
        s.append_seen("s1", [entry("b")])
        # Losing the whole cache would re-alert everything this search matched.
        assert set(s.load_seen("s1")) == {"a", "b"}

    def test_empty_append_is_a_noop(self, tmp_path):
        s = store(tmp_path)
        s.append_seen("s1", [])
        assert not s.seen_path("s1").exists()

    def test_searches_are_isolated(self, tmp_path):
        s = store(tmp_path)
        s.append_seen("s1", [entry("a")])
        s.append_seen("s2", [entry("a", name="s2")])
        assert set(s.load_seen("s1")) == {"a"}
        assert set(s.load_seen("s2")) == {"a"}

    def test_clear_seen(self, tmp_path):
        s = store(tmp_path)
        s.append_seen("s1", [entry("a")])
        s.clear_seen("s1")
        assert s.load_seen("s1") == {}


class TestCompaction:
    def test_compaction_collapses_duplicates(self, tmp_path):
        s = store(tmp_path)
        for _ in range(6):
            s.append_seen("s1", [entry("a")])
        assert s.compact_seen("s1") == 1
        assert len(s.seen_path("s1").read_text().splitlines()) == 1
        assert set(s.load_seen("s1")) == {"a"}

    def test_no_rewrite_when_ratio_is_fine(self, tmp_path):
        s = store(tmp_path)
        s.append_seen("s1", [entry("a"), entry("b")])
        assert s.compact_seen("s1") == 2

    def test_prune_drops_stale_entries(self, tmp_path):
        s = store(tmp_path)
        s.append_seen("s1", [entry("old", last_seen_at=NOW - timedelta(days=120))])
        s.append_seen("s1", [entry("fresh")])
        s.compact_seen("s1", prune_before=NOW - timedelta(days=90))
        assert set(s.load_seen("s1")) == {"fresh"}

    def test_tmp_file_cleaned_up(self, tmp_path):
        s = store(tmp_path)
        for _ in range(6):
            s.append_seen("s1", [entry("a")])
        s.compact_seen("s1")
        assert not s.seen_path("s1").with_suffix(".tmp").exists()

    def test_compact_missing_file(self, tmp_path):
        assert store(tmp_path).compact_seen("nope") == 0


class TestRunState:
    def test_missing_state_is_empty(self, tmp_path):
        assert store(tmp_path).load_state() == {}

    def test_mark_roundtrip(self, tmp_path):
        s = store(tmp_path)
        s.mark("s1", last_run_at=NOW, status="ok", new_count=3)
        loaded = s.load_state()["s1"]
        assert loaded.last_run_at == NOW
        assert loaded.last_status == "ok"
        assert loaded.last_new_count == 3

    def test_mark_preserves_seeded_at(self, tmp_path):
        s = store(tmp_path)
        s.mark("s1", last_run_at=NOW, status="ok", seeded_at=NOW)
        s.mark("s1", last_run_at=NOW + timedelta(hours=1), status="ok")
        assert s.load_state()["s1"].seeded_at == NOW

    def test_mark_clears_seeded_at_on_explicit_none(self, tmp_path):
        """A reseed must be able to forget the old seed — otherwise a crash
        between clearing the cache and re-seeding leaves the state claiming
        "seeded" against an empty cache."""
        s = store(tmp_path)
        s.mark("s1", last_run_at=NOW, status="ok", seeded_at=NOW, seed_count=5)
        s.mark("s1", last_run_at=NOW, status="reseed", seeded_at=None, seed_count=None)
        loaded = s.load_state()["s1"]
        assert loaded.seeded_at is None
        # seed_count is a plain int; cleared reads back as 0 ("nothing seeded").
        assert loaded.seed_count == 0

    def test_error_is_truncated(self, tmp_path):
        s = store(tmp_path)
        s.mark("s1", last_run_at=NOW, status="error", error="x" * 900)
        assert len(s.load_state()["s1"].last_error) == 500

    def test_corrupt_state_file_is_not_fatal(self, tmp_path):
        s = store(tmp_path)
        s.state_path.write_text("{not json")
        # Empty state means "unseeded", which re-seeds silently. Alerting on
        # everything would be the dangerous failure here.
        assert s.load_state() == {}

    def test_state_survives_other_searches(self, tmp_path):
        s = store(tmp_path)
        s.mark("s1", last_run_at=NOW, status="ok")
        s.mark("s2", last_run_at=NOW, status="ok")
        assert set(s.load_state()) == {"s1", "s2"}


class TestIsDue:
    def test_never_run_is_due(self, tmp_path):
        assert store(tmp_path).is_due(resolve(interval="15m"), NOW)

    def test_just_ran_is_not_due(self, tmp_path):
        s = store(tmp_path)
        s.mark("s1", last_run_at=NOW, status="ok")
        assert not s.is_due(resolve(interval="15m"), NOW + timedelta(minutes=1))

    def test_after_interval_is_due(self, tmp_path):
        s = store(tmp_path)
        s.mark("s1", last_run_at=NOW, status="ok")
        assert s.is_due(resolve(interval="15m"), NOW + timedelta(minutes=15))

    def test_tolerance_prevents_cron_drift(self, tmp_path):
        # 14m into a 15m interval on a 5m tick: without slack this would wait
        # for the 20m tick and the delay would compound every cycle.
        s = store(tmp_path)
        s.mark("s1", last_run_at=NOW, status="ok")
        assert s.is_due(resolve(interval="15m"), NOW + timedelta(minutes=14))

    def test_well_short_is_not_due(self, tmp_path):
        s = store(tmp_path)
        s.mark("s1", last_run_at=NOW, status="ok")
        assert not s.is_due(resolve(interval="15m"), NOW + timedelta(minutes=5))

    def test_force_overrides(self, tmp_path):
        s = store(tmp_path)
        s.mark("s1", last_run_at=NOW, status="ok")
        assert s.is_due(resolve(interval="15m"), NOW, force=True)

    def test_clock_moved_backwards_still_runs(self, tmp_path):
        # Otherwise an NTP correction would freeze the search forever.
        s = store(tmp_path)
        s.mark("s1", last_run_at=NOW + timedelta(hours=5), status="ok")
        assert s.is_due(resolve(interval="15m"), NOW)

    def test_accepts_prefetched_state(self, tmp_path):
        s = store(tmp_path)
        state = {"s1": SearchRunState(last_run_at=NOW)}
        assert not s.is_due(resolve(interval="15m"), NOW, state=state)


class TestCacheWasLost:
    def test_unseeded_search_is_not_a_loss(self, tmp_path):
        assert not store(tmp_path).cache_was_lost("s1")

    def test_seeded_with_intact_cache_is_fine(self, tmp_path):
        s = store(tmp_path)
        s.append_seen("s1", [entry("a")])
        s.mark("s1", last_run_at=NOW, status="ok", seeded_at=NOW, seed_count=1)
        assert not s.cache_was_lost("s1")

    def test_seeded_then_wiped_is_detected(self, tmp_path):
        s = store(tmp_path)
        s.append_seen("s1", [entry("a")])
        s.mark("s1", last_run_at=NOW, status="ok", seeded_at=NOW, seed_count=1)
        s.clear_seen("s1")
        assert s.cache_was_lost("s1")

    def test_search_that_seeded_zero_is_not_a_loss(self, tmp_path):
        # Otherwise a narrow search's first genuine hit would be silently seeded
        # instead of alerted -- the exact case the feature exists for.
        s = store(tmp_path)
        s.mark("s1", last_run_at=NOW, status="ok", seeded_at=NOW, seed_count=0)
        assert not s.cache_was_lost("s1")

    def test_seed_count_survives_later_marks(self, tmp_path):
        s = store(tmp_path)
        s.mark("s1", last_run_at=NOW, status="ok", seeded_at=NOW, seed_count=7)
        s.mark("s1", last_run_at=NOW + timedelta(hours=1), status="ok")
        assert s.load_state()["s1"].seed_count == 7


class TestIsStale:
    def test_fresh_is_not_stale(self, tmp_path):
        s = store(tmp_path)
        s.mark("s1", last_run_at=NOW, status="ok")
        assert not s.is_stale(resolve(interval="15m"), NOW + timedelta(minutes=30))

    def test_very_old_is_stale(self, tmp_path):
        s = store(tmp_path)
        s.mark("s1", last_run_at=NOW, status="ok")
        assert s.is_stale(resolve(interval="15m"), NOW + timedelta(hours=4))

    def test_never_run_is_not_stale(self, tmp_path):
        assert not store(tmp_path).is_stale(resolve(interval="15m"), NOW)


class TestHitsAppendLog:
    def _hit(self, item_id="v1|1|0") -> SearchHit:
        return SearchHit(
            run_id="run1",
            search_name="s1",
            item_id=item_id,
            cache_key=build_cache_key("s1", item_id),
            hit_at=NOW,
            first_seen_at=NOW,
        )

    def test_append_and_clear(self, tmp_path):
        s = store(tmp_path)
        s.append_hits([self._hit("a"), self._hit("b")])
        assert len(s.hits_path.read_text().splitlines()) == 2
        s.clear_hits()
        assert not s.hits_path.exists()

    def test_rows_are_bq_serializable(self, tmp_path):
        import json

        s = store(tmp_path)
        s.append_hits([self._hit()])
        row = json.loads(s.hits_path.read_text().splitlines()[0])
        assert row["hit_type"] == "NEW_LISTING"
        assert row["run_id"] == "run1"
        # exclude_none keeps unset optional columns out; BQ reads absence as NULL.
        assert "notified_at" not in row

    def test_flush_with_nothing_buffered_is_a_noop(self, tmp_path):
        assert store(tmp_path).flush_append_log() is None

    def test_flush_uploads_then_loads(self, tmp_path):
        calls = {}

        class FakeGCS:
            def upload_text(self, **kw):
                calls["gcs"] = kw

        class FakeBQ:
            def load_jsonl_from_gcs(self, **kw):
                calls["bq"] = kw

        s = store(tmp_path)
        s.append_hits([self._hit()])
        obj = s.flush_append_log(gcs=FakeGCS(), bq=FakeBQ())

        assert obj.startswith("logs/search_hits/search_hits_")
        assert calls["gcs"]["object_name"] == obj
        assert calls["bq"]["table"] == "search_hits"
        assert calls["bq"]["write_disposition"] == "WRITE_APPEND"
        # Buffer is cleared only after both succeed.
        assert not s.hits_path.exists()

    def test_schema_path_resolves_off_the_package_not_cwd(self, tmp_path):
        # This pipeline runs from cron, where CWD is not the repo root.
        from shoebox.clients.search_state import _schema_path

        assert _schema_path().is_absolute()
        assert _schema_path().exists()

    def test_buffer_survives_a_failed_flush(self, tmp_path):
        class BoomGCS:
            def upload_text(self, **kw):
                raise RuntimeError("network down")

        s = store(tmp_path)
        s.append_hits([self._hit()])
        try:
            s.flush_append_log(gcs=BoomGCS(), bq=None)
        except RuntimeError:
            pass
        # Retried next run; dedup never depended on BigQuery.
        assert s.hits_path.exists()


class TestLock:
    def test_lock_is_acquired(self, tmp_path):
        with store(tmp_path).lock() as acquired:
            assert acquired

    def test_second_holder_is_refused(self, tmp_path):
        s1 = store(tmp_path)
        s2 = SearchStateStore(base_dir=s1.base_dir)
        with s1.lock() as first:
            assert first
            with s2.lock() as second:
                assert not second

    def test_lock_is_released(self, tmp_path):
        s = store(tmp_path)
        with s.lock():
            pass
        with s.lock() as acquired:
            assert acquired

    def test_existing_lock_file_is_not_truncated(self, tmp_path):
        # The Windows backend locks a byte range, and truncating a range another
        # run holds fails outright -- so the file must be opened append-only.
        s = store(tmp_path)
        s.lock_path.write_text("x", encoding="utf-8")
        with s.lock() as acquired:
            assert acquired
        assert s.lock_path.read_text(encoding="utf-8") == "x"


class TestWindowsLockBackend:
    """The service host is Windows; ``fcntl`` does not exist there.

    Loading a second copy of the module with ``sys.platform`` patched is the
    only way to exercise the other branch from a Unix dev machine -- and the
    branch is at import time, which is exactly where the bug was.
    """

    def _load(self, monkeypatch, calls):
        fake_msvcrt = types.ModuleType("msvcrt")
        fake_msvcrt.LK_NBLCK = 1
        fake_msvcrt.LK_UNLCK = 0
        fake_msvcrt.locking = lambda fd, mode, nbytes: calls.append((mode, nbytes))

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
        # None makes ``import fcntl`` raise, proving the Windows path never
        # reaches for it.
        monkeypatch.setitem(sys.modules, "fcntl", None)

        name = "shoebox.clients._search_state_win"
        spec = importlib.util.spec_from_file_location(name, search_state.__file__)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    def test_imports_without_fcntl(self, monkeypatch):
        module = self._load(monkeypatch, [])
        assert module.SearchStateStore is not None

    def test_lock_uses_non_blocking_msvcrt_calls(self, monkeypatch, tmp_path):
        calls: list[tuple[int, int]] = []
        module = self._load(monkeypatch, calls)

        with module.SearchStateStore(base_dir=tmp_path / "searches").lock() as acquired:
            assert acquired

        # Non-blocking acquire then release; LK_LOCK would retry for 10s and
        # stall the scheduler instead of skipping the tick.
        assert calls == [(1, 1), (0, 1)]
