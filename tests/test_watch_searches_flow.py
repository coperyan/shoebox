"""End-to-end orchestration tests with injected collaborators.

No mocking library: run_one_search / watch_searches take ``fetch`` and ``post``
callables, so a plain local function plus tmp_path covers the whole flow.
"""

import pytest
import yaml
from pydantic import ValidationError

from shoebox.clients.search_state import SearchStateStore
from shoebox.models.ebay.item_summary import ItemSummary
from shoebox.pipelines.watch_searches import watch_searches


def item(
    item_id: str,
    title: str = "Michael Jordan Rookie",
    price: str = "100.00",
    image: str | None = None,
) -> ItemSummary:
    """``image`` is the eBay size segment (``s-l225``); None means no photo."""
    return ItemSummary(
        item_id=item_id,
        title=title,
        price={"value": price, "currency": "USD"},
        buying_options=["FIXED_PRICE"],
        item_web_url=f"https://ebay.com/itm/{item_id}",
        thumbnail_images=(
            [{"image_url": f"https://i.ebayimg.com/images/g/{item_id}/{image}.jpg"}]
            if image
            else []
        ),
    )


class Recorder:
    """Stand-in for Slack. Records every post and hands back fake ts values."""

    def __init__(self, fail_on: int | None = None):
        self.posts: list[tuple[str, str, str | None, bool, list[dict] | None]] = []
        self.fail_on = fail_on

    def __call__(self, channel, text, thread_ts, unfurl_links, blocks=None) -> str:
        if self.fail_on is not None and len(self.posts) == self.fail_on:
            raise RuntimeError("slack exploded")
        self.posts.append((channel, text, thread_ts, unfurl_links, blocks))
        return f"ts{len(self.posts)}"

    @property
    def parents(self):
        return [p for p in self.posts if p[2] is None]

    @property
    def replies(self):
        return [p for p in self.posts if p[2] is not None]


@pytest.fixture
def searches_yaml(tmp_path):
    def _write(**overrides):
        search = {"name": "s1", "query": "jordan", "interval": "15m"}
        search.update(overrides)
        path = tmp_path / "searches.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "searches": [search]}))
        return path

    return _write


@pytest.fixture
def store(tmp_path):
    return SearchStateStore(base_dir=tmp_path / "state")


def run(searches_path, store, items, post, **kwargs):
    return watch_searches(
        config_path=searches_path,
        store=store,
        fetch=lambda search, limit: list(items),
        post=post,
        flush=False,
        pacing_seconds=0,
        **kwargs,
    )


class TestSeeding:
    def test_first_run_seeds_without_alerting(self, searches_yaml, store):
        path = searches_yaml()
        post = Recorder()
        results = run(path, store, [item("a"), item("b"), item("c")], post)

        assert results[0].seeded
        # Exactly one line, and no per-item spam.
        assert len(post.posts) == 1
        assert "seeded with 3 existing listing(s)" in post.posts[0][1]
        assert post.replies == []
        assert set(store.load_seen("s1")) == {"a", "b", "c"}

    def test_seed_marks_seeded_at(self, searches_yaml, store):
        run(searches_yaml(), store, [item("a")], Recorder())
        assert store.load_state()["s1"].seeded_at is not None

    def test_second_identical_run_posts_nothing(self, searches_yaml, store):
        path = searches_yaml()
        items = [item("a"), item("b")]
        run(path, store, items, Recorder())

        post = Recorder()
        run(path, store, items, post, force=True)
        assert post.posts == []

    def test_third_run_with_one_new_item_posts_exactly_one(self, searches_yaml, store):
        path = searches_yaml()
        run(path, store, [item("a")], Recorder())

        post = Recorder()
        run(path, store, [item("a"), item("b")], post, force=True)

        assert len(post.parents) == 1
        assert "1 new listing" in post.parents[0][1]
        assert len(post.replies) == 1
        assert "itm/b" in post.replies[0][1]

    def test_wiped_cache_reseeds_silently(self, searches_yaml, store):
        """A deleted seen-file must not turn into a flood of false alerts.

        search_state.json and the *_seen.jsonl caches are separate files, so
        `rm exports/jsonl/*.jsonl` leaves the state claiming "already seeded".
        """
        path = searches_yaml()
        run(path, store, [item("a"), item("b")], Recorder())
        store.clear_seen("s1")

        post = Recorder()
        run(path, store, [item("a"), item("b")], post, force=True)

        assert post.replies == []
        assert "seeded with 2" in post.posts[0][1]
        assert set(store.load_seen("s1")) == {"a", "b"}

    def test_first_real_hit_on_an_empty_search_still_alerts(self, searches_yaml, store):
        """A search that seeded zero matches must alert on its first hit.

        This is the case the lost-cache guard must not swallow -- a narrow
        search for a rare card is exactly what the feature exists for.
        """
        path = searches_yaml()
        run(path, store, [], Recorder())  # seeds nothing

        post = Recorder()
        run(path, store, [item("a")], post, force=True)

        assert len(post.replies) == 1
        assert "itm/a" in post.replies[0][1]


class TestNotifyOnSeed:
    def test_seed_posts_the_initial_matches(self, searches_yaml, store):
        path = searches_yaml(notify_on_seed=True)
        post = Recorder()
        run(path, store, [item("a"), item("b")], post)

        assert len(post.parents) == 1
        assert "seeded with 2 existing listing(s), all shown below" in post.parents[0][1]
        assert len(post.replies) == 2
        posted = " ".join(r[1] for r in post.replies)
        assert "itm/a" in posted and "itm/b" in posted

    def test_seed_respects_max_notify(self, searches_yaml, store):
        # A seed fetches seed_max_results, so the cap is what stops it flooding.
        path = searches_yaml(notify_on_seed=True, max_notify=2)
        post = Recorder()
        run(path, store, [item(str(i)) for i in range(5)], post)

        assert len(post.replies) == 2
        assert "seeded with 5 existing listing(s), showing 2 below" in post.parents[0][1]
        # Every match still recorded, shown or not.
        assert len(store.load_seen("s1")) == 5

    def test_notified_seed_items_do_not_realert(self, searches_yaml, store):
        path = searches_yaml(notify_on_seed=True)
        items = [item("a"), item("b")]
        run(path, store, items, Recorder())

        post = Recorder()
        run(path, store, items, post, force=True)
        assert post.posts == []

    def test_shown_items_are_recorded_as_notified(self, searches_yaml, store):
        path = searches_yaml(notify_on_seed=True, max_notify=1)
        run(path, store, [item("a"), item("b")], Recorder())

        seen = store.load_seen("s1")
        assert seen["a"].notified is True
        assert seen["b"].notified is False

    def test_manual_reseed_posts_again(self, searches_yaml, store):
        path = searches_yaml(notify_on_seed=True)
        run(path, store, [item("a")], Recorder())

        post = Recorder()
        run(path, store, [item("a")], post, reseed=["s1"])
        assert len(post.replies) == 1

    def test_recovery_reseed_stays_silent(self, searches_yaml, store):
        """A lost cache must not replay the whole result set as alerts.

        notify_on_seed is about seeing a *new* search's inventory; the recovery
        seeds exist to suppress noise, and outrank the flag.
        """
        path = searches_yaml(notify_on_seed=True)
        run(path, store, [item("a"), item("b")], Recorder())
        store.clear_seen("s1")

        post = Recorder()
        run(path, store, [item("a"), item("b")], post, force=True)

        assert post.replies == []
        assert "seeded with 2 existing listing(s)." in post.parents[0][1]

    def test_off_by_default(self, searches_yaml, store):
        post = Recorder()
        run(searches_yaml(), store, [item("a")], post)
        assert post.replies == []


class TestNotifyCap:
    def test_cap_posts_overflow_line_and_records_everything(self, searches_yaml, store):
        path = searches_yaml(max_notify=2)
        run(path, store, [], Recorder())  # seed empty

        post = Recorder()
        items = [item(str(i)) for i in range(5)]
        run(path, store, items, post, force=True)

        assert len(post.replies) == 3  # 2 items + 1 overflow
        assert "+3 more new listings not shown" in post.replies[-1][1]
        # All five recorded, or the hidden ones would alert again forever.
        assert len(store.load_seen("s1")) == 5

    def test_capped_items_do_not_realert(self, searches_yaml, store):
        path = searches_yaml(max_notify=2)
        run(path, store, [], Recorder())
        items = [item(str(i)) for i in range(5)]
        run(path, store, items, Recorder(), force=True)

        post = Recorder()
        run(path, store, items, post, force=True)
        assert post.posts == []


class TestOrdering:
    def test_slack_failure_leaves_item_unseen(self, searches_yaml, store):
        """Slack-before-state: a failed post must not mark the item seen."""
        path = searches_yaml()
        run(path, store, [], Recorder())

        items = [item("a"), item("b"), item("c")]
        # posts: 0 = parent, 1 = item a, 2 = item b -> fail on item c.
        post = Recorder(fail_on=3)
        run(path, store, items, post, force=True)

        seen = store.load_seen("s1")
        assert set(seen) == {"a", "b"}
        assert "c" not in seen

    def test_failed_item_alerts_on_the_next_run(self, searches_yaml, store):
        path = searches_yaml()
        run(path, store, [], Recorder())
        items = [item("a"), item("b"), item("c")]
        run(path, store, items, Recorder(fail_on=3), force=True)

        post = Recorder()
        run(path, store, items, post, force=True)
        assert len(post.replies) == 1
        assert "itm/c" in post.replies[0][1]

    def test_imageless_item_falls_back_to_unfurling(self, searches_yaml, store):
        path = searches_yaml()
        run(path, store, [], Recorder())
        post = Recorder()
        run(path, store, [item("a")], post, force=True)
        channel, text, thread_ts, unfurl, blocks = post.replies[0]
        assert unfurl is True
        assert blocks is None
        assert text.splitlines()[-1] == "https://ebay.com/itm/a"
        assert post.parents[0][3] is False

    def test_item_with_photo_posts_an_image_block(self, searches_yaml, store):
        path = searches_yaml()
        run(path, store, [], Recorder())
        post = Recorder()
        run(path, store, [item("a", image="s-l225")], post, force=True)

        channel, text, thread_ts, unfurl, blocks = post.replies[0]
        # The photo is explicit, so nothing is left to Slack's crawler.
        assert unfurl is False
        assert [b["type"] for b in blocks] == ["section", "image"]
        assert blocks[1]["image_url"].endswith("/s-l500.jpg")
        assert blocks[1]["alt_text"] == "Michael Jordan Rookie"
        # Fallback text stays a complete summary, minus the now-redundant URL.
        assert "itm/a" in text
        assert text.splitlines()[-1] != "https://ebay.com/itm/a"


class TestFailureIsolation:
    def test_one_failing_search_does_not_stop_the_others(self, tmp_path, store):
        path = tmp_path / "searches.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "searches": [
                        {"name": "bad", "query": "x", "interval": "15m"},
                        {"name": "good", "query": "y", "interval": "15m"},
                    ],
                }
            )
        )

        def fetch(search, limit):
            if search.name == "bad":
                raise RuntimeError("ebay down")
            return [item("a")]

        post = Recorder()
        watch_searches(
            config_path=path, store=store, fetch=fetch, post=post, flush=False, pacing_seconds=0
        )

        state = store.load_state()
        assert state["bad"].last_status == "error"
        assert "ebay down" in state["bad"].last_error
        assert state["good"].last_status == "ok"

    def test_failed_search_still_advances_last_run_at(self, searches_yaml, store):
        """Otherwise a broken search retries every tick and burns API quota."""
        path = searches_yaml()

        def boom(search, limit):
            raise RuntimeError("nope")

        watch_searches(
            config_path=path,
            store=store,
            fetch=boom,
            post=Recorder(),
            flush=False,
            pacing_seconds=0,
        )
        assert store.load_state()["s1"].last_run_at is not None

    def test_failure_summary_posted(self, searches_yaml, store):
        path = searches_yaml()

        def boom(search, limit):
            raise RuntimeError("nope")

        post = Recorder()
        watch_searches(
            config_path=path, store=store, fetch=boom, post=post, flush=False, pacing_seconds=0
        )
        assert any("search(es) failed" in p[1] for p in post.posts)


class TestScheduling:
    def test_not_due_search_is_skipped(self, searches_yaml, store):
        path = searches_yaml()
        run(path, store, [item("a")], Recorder())

        post = Recorder()
        results = run(path, store, [item("a"), item("b")], post)
        assert results == []
        assert post.posts == []

    def test_only_filter(self, tmp_path, store):
        path = tmp_path / "searches.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "searches": [
                        {"name": "one", "query": "x"},
                        {"name": "two", "query": "y"},
                    ],
                }
            )
        )
        results = watch_searches(
            config_path=path,
            store=store,
            fetch=lambda s, limit: [],
            post=Recorder(),
            flush=False,
            pacing_seconds=0,
            only=["one"],
        )
        assert [r.name for r in results] == ["one"]

    def test_disabled_search_never_runs(self, searches_yaml, store):
        path = searches_yaml(enabled=False)
        assert run(path, store, [item("a")], Recorder()) == []

    def test_reseed_clears_and_reseeds(self, searches_yaml, store):
        path = searches_yaml()
        run(path, store, [item("a")], Recorder())

        post = Recorder()
        run(path, store, [item("a"), item("b")], post, reseed=["s1"])
        # Seeded again: one line, no item alerts.
        assert len(post.posts) == 1
        assert "seeded with 2" in post.posts[0][1]

    def test_lock_prevents_a_concurrent_run(self, searches_yaml, store):
        path = searches_yaml()
        post = Recorder()
        with store.lock():
            other = SearchStateStore(base_dir=store.base_dir)
            results = run(path, other, [item("a")], post)
        assert results == []
        assert post.posts == []


class TestDryRun:
    def test_seed_dry_run_shows_the_listings(self, searches_yaml, store, caplog):
        """A seed posts nothing per-item, so --dry-run must still show a sample.

        Otherwise the first dry run of any new search prints a bare count and
        tells you nothing about whether the filters are right.
        """
        path = searches_yaml()
        with caplog.at_level("INFO"):
            run(path, store, [item("a", title="Matt Cain Auto /99")], Recorder(), dry_run=True)

        output = caplog.text
        assert "would seed" in output
        assert "Matt Cain Auto /99" in output
        assert "https://ebay.com/itm/a" in output

    def test_seed_dry_run_reports_when_nothing_matched(self, searches_yaml, store, caplog):
        path = searches_yaml()
        with caplog.at_level("INFO"):
            run(path, store, [], Recorder(), dry_run=True)
        assert "nothing matched" in caplog.text

    def test_seed_dry_run_truncates_a_large_sample(self, searches_yaml, store, caplog):
        from shoebox.pipelines.watch_searches import DRY_RUN_SAMPLE

        path = searches_yaml()
        items = [item(str(i)) for i in range(DRY_RUN_SAMPLE + 8)]
        with caplog.at_level("INFO"):
            run(path, store, items, Recorder(), dry_run=True)
        assert "and 8 more" in caplog.text

    def test_dry_run_writes_no_state_and_posts_nothing(self, searches_yaml, store):
        path = searches_yaml()
        post = Recorder()
        run(path, store, [item("a")], post, dry_run=True)

        assert post.posts == []
        assert store.load_seen("s1") == {}
        assert store.load_state() == {}

    def test_dry_run_reseed_keeps_the_seen_cache(self, searches_yaml, store, caplog):
        """Rehearsing a reseed must not perform one.

        Discarding the cache is the most destructive thing the command does, and
        --dry-run promises no state writes -- but the run must still take the
        seed path, or there would be nothing to rehearse.
        """
        path = searches_yaml()
        run(path, store, [item("a"), item("b")], Recorder())
        before = store.load_seen("s1")

        post = Recorder()
        with caplog.at_level("INFO"):
            run(path, store, [item("a"), item("b")], post, reseed=["s1"], dry_run=True)

        assert post.posts == []
        assert store.load_seen("s1").keys() == before.keys()
        assert store.load_state()["s1"].seeded_at is not None
        assert "would seed s1 with 2 items" in caplog.text


class TestListOnly:
    def test_list_validates_without_running(self, searches_yaml, store):
        post = Recorder()
        results = run(searches_yaml(), store, [item("a")], post, list_only=True)
        assert results == []
        assert post.posts == []

    def test_list_surfaces_config_errors(self, tmp_path, store):
        path = tmp_path / "searches.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "searches": [{"name": "Bad Name"}]}))
        with pytest.raises(ValidationError, match="invalid search name"):
            watch_searches(config_path=path, store=store, list_only=True)


class TestConfigErrors:
    def test_bad_config_alerts_slack_then_raises(self, tmp_path, store):
        """Under cron nobody reads the log, so a broken file must reach Slack."""
        path = tmp_path / "searches.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "searches": [{"name": "Bad Name"}]}))

        post = Recorder()
        with pytest.raises(ValidationError, match="invalid search name"):
            watch_searches(config_path=path, store=store, post=post, flush=False)

        assert len(post.posts) == 1
        assert "bad config" in post.posts[0][1]

    def test_list_mode_does_not_alert(self, tmp_path, store):
        path = tmp_path / "searches.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "searches": [{"name": "Bad Name"}]}))

        post = Recorder()
        with pytest.raises(ValidationError):
            watch_searches(config_path=path, store=store, post=post, list_only=True)
        assert post.posts == []

    def test_missing_file_is_reported(self, tmp_path, store):
        post = Recorder()
        with pytest.raises(FileNotFoundError, match="searches.example.yml"):
            watch_searches(config_path=tmp_path / "nope.yaml", store=store, post=post, flush=False)
