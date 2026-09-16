"""End-to-end review-listings tests with injected collaborators.

No mocking library: main() takes ``ebay``, ``state``, ``post`` and ``run_batch``,
so plain local fakes cover the whole flow (same convention as
test_send_offers_flow.py). The reprice path runs the real ``ListingService``
against a fake inventory client, so the offer body it sends is covered too.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from shoebox.clients.review_state import ReviewStateStore
from shoebox.models.ebay.offer import Offer
from shoebox.models.listing_review import OUTCOME_KEPT, OUTCOME_REPRICED, ReviewCandidate
from shoebox.pipelines.review_listings import (
    decide_reply,
    fetch_listing_rows,
    format_review_prompt,
    main,
)
from shoebox.utils.slack import EXPIRED_STATUS

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def active_listing(
    item_id: str = "a",
    *,
    title: str = "2026 Topps Chrome Ohtani",
    price: str = "4.29",
    age_days: int = 30,
    watchers=None,
    sku: str | None = "SKU-1",
) -> dict:
    return {
        "item_id": item_id,
        "title": title,
        "sku": sku,
        "price": price,
        "quantity": "1",
        "start_time": (NOW - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "watchers": watchers,
        "view_item_url": f"https://ebay.com/itm/{item_id}",
    }


def offer_payload(sku: str = "SKU-1", price: str = "4.29") -> dict:
    return {
        "offer_id": f"offer-{sku}",
        "sku": sku,
        "marketplace_id": "EBAY_US",
        "format": "FIXED_PRICE",
        "available_quantity": 1,
        "category_id": "261328",
        "listing_description": "<p>card</p>",
        "listing_duration": "GTC",
        "merchant_location_key": "LOC",
        "listing_policies": {
            "fulfillment_policy_id": "FULFILL-LOW",
            "payment_policy_id": "PAY",
            "return_policy_id": "RETURN",
        },
        "store_category_names": ["/Baseball/Rookies"],
        "pricing_summary": {"price": {"value": price, "currency": "USD"}},
    }


class FakeTrading:
    def __init__(
        self,
        listings: list[dict],
        *,
        detail_error: bool = False,
        revise_fail_ids: set[str] | None = None,
    ):
        self.listings = listings
        self.detail_error = detail_error
        self.revise_fail_ids = revise_fail_ids or set()
        self.detail_calls: list[str] = []
        self.revisions: list[tuple[str, float]] = []

    def get_active_listings(self) -> list[dict]:
        return list(self.listings)

    def get_item_details(self, item_id: str) -> dict:
        self.detail_calls.append(item_id)
        if self.detail_error:
            raise RuntimeError("GetItem exploded")
        return {"item_id": item_id, "picture_urls": [f"https://img/{item_id}.jpg"]}

    def revise_listing_price(self, item_id: str, price: float, **kwargs) -> dict:
        if item_id in self.revise_fail_ids:
            raise RuntimeError("Trading says no either")
        self.revisions.append((item_id, price))
        return {"ack": "Success"}


class FakeAnalytics:
    def __init__(self, traffic: dict[str, tuple[int, int]], *, error: Exception | None = None):
        self.traffic = traffic
        self.error = error
        self.calls = 0

    def get_traffic_report(self, *, date_from, date_to, listing_ids, **kwargs) -> list[dict]:
        self.calls += 1
        if self.error:
            raise self.error
        return [
            {
                "LISTING_ID": item_id,
                "LISTING_VIEWS_TOTAL": views,
                "LISTING_IMPRESSION_TOTAL": impressions,
            }
            for item_id, (views, impressions) in self.traffic.items()
            if item_id in listing_ids
        ]


class FakeInventory:
    def __init__(self, offers: dict[str, dict]):
        self.offers = offers
        self.updates: list[tuple[str, dict]] = []

    def find_offer(self, sku: str) -> Offer | None:
        payload = self.offers.get(sku)
        return Offer.from_api(payload) if payload else None

    def update_offer(self, offer_id: str, body: dict) -> None:
        self.updates.append((offer_id, body))


class FakeEbay:
    def __init__(
        self,
        listings,
        traffic,
        *,
        traffic_error=None,
        detail_error=False,
        revise_fail_ids=None,
    ):
        self.trading = FakeTrading(
            listings, detail_error=detail_error, revise_fail_ids=revise_fail_ids
        )
        self.analytics = FakeAnalytics(traffic, error=traffic_error)
        self.inventory = FakeInventory(
            {x["sku"]: offer_payload(x["sku"], x["price"]) for x in listings if x.get("sku")}
        )


class Recorder:
    """Stand-in for notify(). Records posts and hands back fake ts values."""

    def __init__(self):
        self.posts: list[tuple[str, str, str | None]] = []

    def __call__(self, channel, message, thread_ts=None, **kwargs) -> str:
        self.posts.append((channel, message, thread_ts))
        return f"ts{len(self.posts)}"


class ScriptedBatch:
    """Stand-in for notify_batch_and_wait: feeds each prompt its scripted
    replies until one resolves it; prompts with no resolving reply expire."""

    def __init__(self, replies: dict[str, list[str]]):
        self.replies = replies
        self.prompts = None
        self.channel = None
        self.thread_posts: dict[str, list[str]] = {}

    def __call__(self, channel, prompts, on_reply, timeout_s=900):
        self.channel = channel
        self.prompts = prompts
        results = {}
        for prompt in prompts:
            for reply in self.replies.get(prompt.key, []):

                async def post_thread(msg, key=prompt.key):
                    self.thread_posts.setdefault(key, []).append(msg)

                outcome = asyncio.run(on_reply(prompt.key, reply, post_thread))
                if outcome is not None:
                    results[prompt.key] = outcome
                    break
            else:
                results[prompt.key] = EXPIRED_STATUS
        return results


def run(tmp_path, listings, traffic, replies=None, **kwargs):
    ebay = kwargs.pop("ebay", None) or FakeEbay(listings, traffic)
    state = ReviewStateStore(base_dir=tmp_path)
    post = Recorder()
    batch = ScriptedBatch(replies or {})
    main(ebay=ebay, state=state, post=post, run_batch=batch, now=NOW, **kwargs)
    return ebay, state, post, batch


def one_candidate(**kwargs):
    return [active_listing(**kwargs)], {"a": (20, 400)}


class TestInteractiveFlow:
    def test_a_price_reply_updates_the_live_listing(self, tmp_path):
        listings, traffic = one_candidate()
        ebay, state, post, _ = run(tmp_path, listings, traffic, {"a": ["3.49"]})

        offer_id, body = ebay.inventory.updates[0]
        assert offer_id == "offer-SKU-1"
        assert body["pricingSummary"]["price"]["value"] == "3.49"

        record = state.load()["a"]
        assert (record.outcome, record.price, record.new_price) == (OUTCOME_REPRICED, 4.29, 3.49)
        assert "1 repriced, 0 kept, 0 skipped, 0 expired" in post.posts[-1][1]

    def test_ok_takes_the_suggested_markdown(self, tmp_path):
        listings, traffic = one_candidate()
        ebay, state, _, _ = run(tmp_path, listings, traffic, {"a": ["ok"]})

        assert ebay.inventory.updates[0][1]["pricingSummary"]["price"]["value"] == "3.79"
        assert state.load()["a"].new_price == 3.79

    def test_keep_leaves_the_price_and_starts_the_cooldown(self, tmp_path):
        listings, traffic = one_candidate()
        ebay, state, post, _ = run(tmp_path, listings, traffic, {"a": ["keep"]})

        assert ebay.inventory.updates == []
        assert state.load()["a"].outcome == OUTCOME_KEPT
        assert "0 repriced, 1 kept" in post.posts[-1][1]

    def test_skip_leaves_no_record_so_it_comes_back(self, tmp_path):
        listings, traffic = one_candidate()
        ebay, state, post, _ = run(tmp_path, listings, traffic, {"a": ["skip"]})

        assert ebay.inventory.updates == []
        assert state.load() == {}
        assert "1 skipped" in post.posts[-1][1]

    def test_an_unanswered_prompt_expires_and_leaves_no_record(self, tmp_path):
        listings, traffic = one_candidate()
        _, state, post, _ = run(tmp_path, listings, traffic, {})

        assert state.load() == {}
        assert "1 expired" in post.posts[-1][1]

    def test_an_unparseable_reply_hints_then_the_next_one_lands(self, tmp_path):
        listings, traffic = one_candidate()
        ebay, _, _, batch = run(tmp_path, listings, traffic, {"a": ["huh?", "3.29"]})

        assert any("Couldn't parse" in m for m in batch.thread_posts["a"])
        assert ebay.inventory.updates[0][1]["pricingSummary"]["price"]["value"] == "3.29"

    def test_a_price_at_or_above_the_current_one_is_refused(self, tmp_path):
        listings, traffic = one_candidate()
        ebay, _, _, batch = run(tmp_path, listings, traffic, {"a": ["5.99"]})

        assert ebay.inventory.updates == []
        assert any("markdown review" in m for m in batch.thread_posts["a"])

    def test_an_ebay_rejection_is_reported_in_thread(self, tmp_path):
        # Both routes refuse: the offer update fails and the Trading fallback
        # fails too, which is what a listing eBay won't let you revise looks like.
        listings, traffic = one_candidate()
        ebay = FakeEbay(listings, traffic, revise_fail_ids={"a"})

        def boom(offer_id, body):
            raise RuntimeError("offer is not editable")

        ebay.inventory.update_offer = boom
        _, state, _, batch = run(tmp_path, listings, traffic, {"a": ["3.49"]}, ebay=ebay)

        assert any("eBay rejected" in m for m in batch.thread_posts["a"])
        assert state.load() == {}

    def test_an_offer_update_failure_falls_back_to_trading(self, tmp_path):
        listings, traffic = one_candidate()
        ebay = FakeEbay(listings, traffic)

        def boom(offer_id, body):
            raise RuntimeError("offer is not editable")

        ebay.inventory.update_offer = boom
        _, state, _, _ = run(tmp_path, listings, traffic, {"a": ["3.49"]}, ebay=ebay)

        assert ebay.trading.revisions == [("a", 3.49)]
        assert state.load()["a"].new_price == 3.49

    def test_a_sku_less_listing_is_repriced_through_trading(self, tmp_path):
        listings, traffic = one_candidate(sku=None)
        ebay, _, _, _ = run(tmp_path, listings, traffic, {"a": ["3.49"]})

        assert ebay.trading.revisions == [("a", 3.49)]

    def test_nothing_to_review_posts_nothing(self, tmp_path):
        listings, traffic = one_candidate(age_days=3)  # too young
        ebay, _, post, batch = run(tmp_path, listings, traffic, {"a": ["3.49"]})

        assert post.posts == []
        assert batch.prompts is None
        assert ebay.trading.detail_calls == []

    def test_prompts_carry_the_photo_but_only_for_the_shortlist(self, tmp_path):
        listings = [active_listing("a"), active_listing("b", age_days=2)]
        traffic = {"a": (20, 400), "b": (30, 500)}
        ebay, _, _, batch = run(tmp_path, listings, traffic, {"a": ["skip"]})

        # "b" was rejected before the photo sweep, so it cost no GetItem call.
        assert ebay.trading.detail_calls == ["a"]
        assert [b["type"] for b in batch.prompts[0].blocks] == ["section", "image", "context"]

    def test_a_missing_photo_does_not_stop_the_prompt(self, tmp_path):
        listings, traffic = one_candidate()
        ebay = FakeEbay(listings, traffic, detail_error=True)
        _, _, _, batch = run(tmp_path, listings, traffic, {"a": ["skip"]}, ebay=ebay)

        assert [b["type"] for b in batch.prompts[0].blocks] == ["section", "context"]


class TestCooldownAcrossRuns:
    def test_a_decided_listing_is_not_raised_again(self, tmp_path):
        listings, traffic = one_candidate()
        run(tmp_path, listings, traffic, {"a": ["keep"]})
        _, _, post, batch = run(tmp_path, listings, traffic, {"a": ["3.49"]})

        assert batch.prompts is None
        assert post.posts == []

    def test_force_raises_it_anyway(self, tmp_path):
        listings, traffic = one_candidate()
        run(tmp_path, listings, traffic, {"a": ["keep"]})
        ebay, _, _, batch = run(tmp_path, listings, traffic, {"a": ["3.49"]}, force=True)

        assert [p.key for p in batch.prompts] == ["a"]
        assert ebay.inventory.updates

    def test_a_listing_that_is_gone_is_forgotten(self, tmp_path):
        listings, traffic = one_candidate()
        _, state, _, _ = run(tmp_path, listings, traffic, {"a": ["keep"]})
        assert list(state.load()) == ["a"]

        # Next run, the card has sold: it is no longer in the active list.
        run(tmp_path, [active_listing("z", age_days=2)], {"z": (1, 1)})
        assert state.load() == {}


class TestDryRunAndAuto:
    def test_dry_run_touches_nothing(self, tmp_path):
        listings, traffic = one_candidate()
        ebay, state, post, batch = run(tmp_path, listings, traffic, {"a": ["3.49"]}, dry_run=True)

        assert ebay.inventory.updates == []
        assert post.posts == []
        assert batch.prompts is None
        assert state.load() == {}

    def test_auto_applies_the_suggestion_without_prompting(self, tmp_path):
        listings, traffic = one_candidate()
        ebay, state, post, batch = run(tmp_path, listings, traffic, auto=True)

        assert ebay.inventory.updates[0][1]["pricingSummary"]["price"]["value"] == "3.79"
        assert state.load()["a"].outcome == OUTCOME_REPRICED
        assert batch.prompts is None
        assert "auto-repriced 1 listing(s)" in post.posts[0][1]

    def test_auto_keeps_going_when_one_listing_fails(self, tmp_path):
        listings = [active_listing("a"), active_listing("b", sku="SKU-2")]
        traffic = {"a": (20, 400), "b": (30, 500)}
        ebay = FakeEbay(listings, traffic, revise_fail_ids={"b"})
        failed = []

        real_update = ebay.inventory.update_offer

        def flaky(offer_id, body):
            if offer_id == "offer-SKU-2":
                failed.append(offer_id)
                raise RuntimeError("offer is not editable")
            real_update(offer_id, body)

        ebay.inventory.update_offer = flaky
        _, state, post, _ = run(tmp_path, listings, traffic, auto=True, ebay=ebay)

        assert failed == ["offer-SKU-2"]
        assert list(state.load()) == ["a"]
        assert "1 failed" in post.posts[0][1]


class TestOverrides:
    def test_thresholds_can_be_overridden_per_run(self, tmp_path):
        listings, traffic = one_candidate(age_days=5)
        _, _, _, batch = run(
            tmp_path, listings, traffic, {"a": ["skip"]}, overrides={"min_age_days": 3}
        )
        assert [p.key for p in batch.prompts] == ["a"]

    def test_unset_overrides_are_ignored(self, tmp_path):
        listings, traffic = one_candidate()
        _, _, _, batch = run(
            tmp_path,
            listings,
            traffic,
            {"a": ["skip"]},
            overrides={"min_age_days": None, "min_views": None},
        )
        assert [p.key for p in batch.prompts] == ["a"]


class TestFetchListingRows:
    def test_traffic_is_joined_onto_each_listing(self):
        ebay = FakeEbay([active_listing("a")], {"a": (20, 400)})
        rows = fetch_listing_rows(ebay)
        assert rows[0]["views"] == 20
        assert rows[0]["impressions"] == 400

    def test_a_listing_with_no_traffic_row_still_comes_back(self):
        ebay = FakeEbay([active_listing("a")], {})
        assert fetch_listing_rows(ebay)[0]["views"] is None

    def test_a_traffic_failure_stops_the_run(self):
        # Silently reviewing nothing is the most misleading possible answer.
        ebay = FakeEbay([active_listing("a")], {}, traffic_error=RuntimeError("Too Many Requests"))
        with pytest.raises(RuntimeError, match="traffic report"):
            fetch_listing_rows(ebay)

    def test_no_listings_means_no_traffic_call(self):
        ebay = FakeEbay([], {})
        assert fetch_listing_rows(ebay) == []
        assert ebay.analytics.calls == 0


def candidate(**overrides) -> ReviewCandidate:
    payload = {
        "item_id": "a",
        "title": "Card",
        "sku": "SKU-1",
        "price": 4.29,
        "suggested_price": 3.79,
        "views": 20,
        "impressions": 400,
        "watchers": 0,
        "age_days": 30,
        "view_item_url": "https://ebay.com/itm/a",
    }
    payload.update(overrides)
    return ReviewCandidate(**payload)


class TestReplyGrammar:
    def test_accept_keywords(self):
        for word in ("ok", "OK", " yes ", "y"):
            assert decide_reply(candidate(), word) == ("apply", 3.79, None)

    def test_keep_and_skip(self):
        assert decide_reply(candidate(), "keep")[0] == "keep"
        assert decide_reply(candidate(), "SKIP")[0] == "skip"

    def test_a_bare_price(self):
        assert decide_reply(candidate(), "$3.29") == ("apply", 3.29, None)

    def test_below_the_floor_is_refused(self):
        action, _, msg = decide_reply(candidate(), "0.50", floor=0.99)
        assert action == "retry" and "Floor" in msg


class TestPromptRendering:
    def test_the_prompt_shows_the_numbers_that_justify_it(self):
        prompt = format_review_prompt(candidate(), cooldown_days=21)
        section = prompt.blocks[0]["text"]["text"]

        assert prompt.key == "a"
        assert "<https://ebay.com/itm/a|Card>" in section
        assert "$4.29 · 20 views" in section
        assert "0 watchers" in section
        assert "listed 30d ago" in section
        assert "$3.79" in section and "12% off" in section
        assert "21d" in prompt.blocks[-1]["elements"][0]["text"]

    def test_a_repeat_visit_says_so(self):
        prompt = format_review_prompt(candidate(times_reviewed=2), cooldown_days=21)
        assert "Reviewed 2× before" in prompt.blocks[0]["text"]["text"]

    def test_a_listing_without_a_url_still_renders(self):
        prompt = format_review_prompt(candidate(view_item_url=None), cooldown_days=21)
        assert prompt.blocks[0]["text"]["text"].startswith("*Card*")
