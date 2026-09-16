"""Price-review selection rules, review state, and the in-place reprice path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shoebox.clients.review_state import ReviewStateStore
from shoebox.models.ebay.offer import Offer
from shoebox.models.listing_review import OUTCOME_KEPT, OUTCOME_REPRICED, ReviewRecord
from shoebox.services.listings import ListingService
from shoebox.settings import ListingReviewSettings
from shoebox.transforms.listing_builder import offer_body_with_price
from shoebox.transforms.listing_review import (
    Rejection,
    select_candidates,
    suggest_price,
    summarize_rejections,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)
RULES = ListingReviewSettings()


def listing(
    item_id: str = "a",
    *,
    title: str = "2026 Topps Chrome Ohtani",
    price: float | str = 4.29,
    age_days: int = 30,
    views: int | None = 20,
    impressions: int = 400,
    watchers=None,
    sku: str | None = "SKU-1",
) -> dict:
    return {
        "item_id": item_id,
        "title": title,
        "sku": sku,
        "price": price,
        "start_time": (NOW - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "watchers": watchers,
        "views": views,
        "impressions": impressions,
        "view_item_url": f"https://ebay.com/itm/{item_id}",
    }


def select(rows, **kwargs):
    kwargs.setdefault("rules", RULES)
    kwargs.setdefault("now", NOW)
    return select_candidates(rows, **kwargs)


def reasons(rejections: list[Rejection]) -> list[str]:
    return [r.reason for r in rejections]


class TestSuggestPrice:
    def test_uses_the_store_discount_matrix(self):
        # 4.29 falls in the 5.49 tier -> $0.50 off, rounded to a .x9 point.
        assert suggest_price(4.29) == 3.79

    def test_above_the_matrix_falls_back_to_a_percentage(self):
        # calculate_new_price has no tier past ~$20 and raises there.
        assert suggest_price(40.00) == 37.99

    def test_never_proposes_the_current_price(self):
        for price in (0.99, 1.09, 1.19, 2.29, 3.49, 9.99, 25.00):
            assert suggest_price(price, floor=0.01) < price

    def test_respects_the_floor(self):
        assert suggest_price(1.09, floor=0.99) == 0.99


class TestSelection:
    def test_views_without_interest_is_a_candidate(self):
        candidates, _ = select([listing()])
        assert [c.item_id for c in candidates] == ["a"]
        candidate = candidates[0]
        assert candidate.price == 4.29
        assert candidate.suggested_price == 3.79
        assert candidate.markdown == 0.5
        assert candidate.age_days == 30
        assert candidate.sku == "SKU-1"

    def test_too_young_is_held_back(self):
        _, rejected = select([listing(age_days=5)])
        assert reasons(rejected) == ["listed 5d ago, under 14d"]

    def test_too_old_belongs_to_relist_listings(self):
        _, rejected = select([listing(age_days=120)])
        assert reasons(rejected) == ["listed 120d ago, over 75d"]

    def test_a_watcher_means_someone_wants_it(self):
        _, rejected = select([listing(watchers="2")])
        assert reasons(rejected) == ["2 watcher(s)"]

    def test_too_few_views_is_not_evidence(self):
        _, rejected = select([listing(views=3)])
        assert reasons(rejected) == ["3 views, under 10"]

    def test_missing_traffic_counts_as_no_views(self):
        _, rejected = select([listing(views=None)])
        assert reasons(rejected) == ["0 views, under 10"]

    def test_cheap_listings_are_left_to_the_relist_ladder(self):
        _, rejected = select([listing(price=1.49)])
        assert reasons(rejected) == ["price $1.49 under $2.00"]

    def test_variation_listings_are_skipped(self):
        _, rejected = select([listing(title="Complete Your Set - You Pick")])
        assert reasons(rejected) == ["variation listing"]

    def test_unparseable_start_time_is_skipped_not_crashed(self):
        row = listing()
        row["start_time"] = "not a date"
        _, rejected = select([row])
        assert reasons(rejected) == ["no start time"]

    def test_price_as_a_string_is_fine(self):
        candidates, _ = select([listing(price="4.29")])
        assert candidates[0].price == 4.29

    def test_a_listing_already_at_the_floor_is_not_worth_asking_about(self):
        rules = RULES.model_copy(update={"min_price": 0.99, "price_floor": 0.99})
        _, rejected = select([listing(price=0.99)], rules=rules)
        assert reasons(rejected) == ["already at the $0.99 floor"]


class TestCooldown:
    def history(self, days_ago: int, outcome: str = OUTCOME_REPRICED) -> dict[str, ReviewRecord]:
        return {
            "a": ReviewRecord(
                item_id="a",
                last_reviewed_at=NOW - timedelta(days=days_ago),
                outcome=outcome,
                times_reviewed=1,
            )
        }

    def test_a_recent_decision_is_left_alone(self):
        _, rejected = select([listing()], history=self.history(3))
        assert reasons(rejected) == ["repriced 3d ago (cooldown 21d)"]

    def test_an_old_decision_comes_back_around(self):
        candidates, _ = select([listing()], history=self.history(30))
        assert [c.item_id for c in candidates] == ["a"]
        assert candidates[0].times_reviewed == 1

    def test_keeping_a_price_also_starts_the_cooldown(self):
        _, rejected = select([listing()], history=self.history(2, OUTCOME_KEPT))
        assert reasons(rejected) == ["kept 2d ago (cooldown 21d)"]

    def test_force_ignores_the_cooldown(self):
        candidates, _ = select([listing()], history=self.history(1), ignore_cooldown=True)
        assert [c.item_id for c in candidates] == ["a"]


class TestRanking:
    def test_most_looked_at_first_then_most_expensive(self):
        rows = [
            listing("cheap-busy", price=3.29, views=40),
            listing("pricey-busy", price=12.99, views=40),
            listing("quiet", price=9.99, views=12),
        ]
        candidates, _ = select(rows)
        assert [c.item_id for c in candidates] == ["pricey-busy", "cheap-busy", "quiet"]

    def test_the_per_run_cap_holds_the_rest_over(self):
        rows = [listing(str(i), views=10 + i) for i in range(5)]
        candidates, rejected = select(rows, rules=RULES.model_copy(update={"max_per_run": 2}))
        assert [c.item_id for c in candidates] == ["4", "3"]
        assert reasons(rejected) == ["over the 2-per-run cap"] * 3

    def test_an_explicit_limit_wins_over_the_config_cap(self):
        rows = [listing(str(i), views=10 + i) for i in range(5)]
        candidates, _ = select(rows, limit=1)
        assert [c.item_id for c in candidates] == ["4"]


class TestRejectionSummary:
    def test_reasons_group_by_kind_not_by_number(self):
        rejections = [
            Rejection("a", "A", "listed 31d ago, over 75d"),
            Rejection("b", "B", "listed 200d ago, over 75d"),
            Rejection("c", "C", "2 watcher(s)"),
        ]
        assert summarize_rejections(rejections) == [
            ("listed #d ago, over #d", 2),
            ("# watcher(s)", 1),
        ]


class TestReviewStateStore:
    def store(self, tmp_path) -> ReviewStateStore:
        return ReviewStateStore(base_dir=tmp_path)

    def test_a_decision_round_trips(self, tmp_path):
        store = self.store(tmp_path)
        store.record("a", outcome=OUTCOME_REPRICED, price=4.29, new_price=3.79, at=NOW)

        record = store.load()["a"]
        assert record.outcome == OUTCOME_REPRICED
        assert record.new_price == 3.79
        assert record.last_reviewed_at == NOW
        assert record.times_reviewed == 1

    def test_repeat_reviews_are_counted(self, tmp_path):
        store = self.store(tmp_path)
        store.record("a", outcome=OUTCOME_REPRICED, price=4.29, new_price=3.79)
        store.record("a", outcome=OUTCOME_KEPT, price=3.79)
        assert store.load()["a"].times_reviewed == 2

    def test_only_real_outcomes_are_accepted(self, tmp_path):
        with pytest.raises(ValueError):
            self.store(tmp_path).record("a", outcome="skipped")

    def test_an_unreadable_state_file_does_not_stop_the_review(self, tmp_path):
        store = self.store(tmp_path)
        store.state_path.write_text("{ not json", encoding="utf-8")
        assert store.load() == {}

    def test_a_malformed_record_is_dropped_not_fatal(self, tmp_path):
        store = self.store(tmp_path)
        store.state_path.write_text(
            '{"a": {"outcome": "repriced"}, '
            '"b": {"outcome": "kept", "last_reviewed_at": "2026-09-01T00:00:00Z"}}',
            encoding="utf-8",
        )
        assert list(store.load()) == ["b"]

    def test_pruning_forgets_listings_that_are_gone(self, tmp_path):
        store = self.store(tmp_path)
        store.record("a", outcome=OUTCOME_REPRICED, price=4.29, new_price=3.79)
        store.record("b", outcome=OUTCOME_KEPT, price=2.99)

        assert store.prune(["b"]) == 1
        assert list(store.load()) == ["b"]


def offer_payload(**overrides) -> dict:
    payload = {
        "offer_id": "offer-1",
        "sku": "SKU-1",
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
            "best_offer_terms": {"best_offer_enabled": True},
        },
        "store_category_names": ["/Baseball/Rookies"],
        "pricing_summary": {"price": {"value": "4.29", "currency": "USD"}},
    }
    payload.update(overrides)
    return payload


class TestOfferBodyWithPrice:
    def test_only_the_price_changes(self):
        body = offer_body_with_price(offer_payload(), 3.79)
        assert body["pricingSummary"] == {"price": {"value": "3.79", "currency": "USD"}}
        assert body["sku"] == "SKU-1"
        assert body["availableQuantity"] == 1
        assert body["listingDescription"] == "<p>card</p>"
        assert body["storeCategoryNames"] == ["/Baseball/Rookies"]

    def test_the_shipping_policy_is_never_re_derived(self):
        # A markdown across the low/high threshold must not change how the
        # card ships -- that is rebuild_offer_body's job, not this one's.
        body = offer_body_with_price(offer_payload(), 3.79)
        assert body["listingPolicies"]["fulfillmentPolicyId"] == "FULFILL-LOW"
        assert body["listingPolicies"]["bestOfferTerms"] == {"bestOfferEnabled": True}

    def test_a_listing_with_no_store_categories_does_not_send_an_empty_list(self):
        body = offer_body_with_price(offer_payload(store_category_names=[]), 3.79)
        assert "storeCategoryNames" not in body

    def test_a_nonsense_price_is_refused(self):
        with pytest.raises(ValueError):
            offer_body_with_price(offer_payload(), 0)


class FakeInventory:
    def __init__(self, offer: Offer | None, *, fail: bool = False):
        self.offer = offer
        self.fail = fail
        self.updates: list[tuple[str, dict]] = []

    def find_offer(self, sku: str) -> Offer | None:
        return self.offer

    def update_offer(self, offer_id: str, body: dict) -> None:
        if self.fail:
            raise RuntimeError("eBay said no")
        self.updates.append((offer_id, body))


class FakeTrading:
    def __init__(self):
        self.revisions: list[tuple[str, float]] = []

    def revise_listing_price(self, item_id: str, price: float, **kwargs) -> dict:
        self.revisions.append((item_id, price))
        return {"ack": "Success", "item_id": item_id, "price": price}


class FakeEbay:
    def __init__(self, offer: Offer | None = None, *, inventory_fails: bool = False):
        self.inventory = FakeInventory(offer, fail=inventory_fails)
        self.trading = FakeTrading()


class TestUpdatePrice:
    def test_a_sku_listing_goes_through_the_offer(self):
        ebay = FakeEbay(Offer.from_api(offer_payload()))
        result = ListingService(ebay).update_price(new_price=3.79, sku="SKU-1", item_id="123")

        assert result["method"] == "inventory"
        assert result["previous_price"] == 4.29
        offer_id, body = ebay.inventory.updates[0]
        assert offer_id == "offer-1"
        assert body["pricingSummary"]["price"]["value"] == "3.79"
        assert ebay.trading.revisions == []

    def test_a_listing_already_at_the_price_is_skipped(self):
        ebay = FakeEbay(Offer.from_api(offer_payload()))
        result = ListingService(ebay).update_price(new_price=4.29, sku="SKU-1")

        assert result["skipped"] is True
        assert ebay.inventory.updates == []

    def test_a_listing_without_a_sku_goes_through_trading(self):
        ebay = FakeEbay()
        result = ListingService(ebay).update_price(new_price=3.79, item_id="123")

        assert result["method"] == "trading"
        assert ebay.trading.revisions == [("123", 3.79)]

    def test_an_inventory_failure_falls_back_to_trading(self):
        ebay = FakeEbay(Offer.from_api(offer_payload()), inventory_fails=True)
        result = ListingService(ebay).update_price(new_price=3.79, sku="SKU-1", item_id="123")

        assert result["method"] == "trading_fallback"
        assert ebay.trading.revisions == [("123", 3.79)]

    def test_an_inventory_failure_with_nothing_to_fall_back_to_raises(self):
        ebay = FakeEbay(Offer.from_api(offer_payload()), inventory_fails=True)
        with pytest.raises(RuntimeError):
            ListingService(ebay).update_price(new_price=3.79, sku="SKU-1")

    def test_it_needs_something_to_identify_the_listing(self):
        with pytest.raises(ValueError):
            ListingService(FakeEbay()).update_price(new_price=3.79)

    def test_a_nonsense_price_is_refused(self):
        with pytest.raises(ValueError):
            ListingService(FakeEbay()).update_price(new_price=0, item_id="123")
