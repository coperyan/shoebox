import pytest
import yaml

from shoebox.models.ebay.item_summary import ItemSummary
from shoebox.pipelines.preview_search import (
    COLUMNS,
    aspect_options,
    describe_request,
    preview_search,
    resolve_search,
)

# Shape of eBay's aspectDistributions, trimmed to what we read.
ASPECTS_FIXTURE = [
    {
        "localized_aspect_name": "Player/Athlete",
        "aspect_value_distributions": [
            {"localized_aspect_value": "Matt Cain", "match_count": 179},
            {"localized_aspect_value": "Adam Wainwright", "match_count": 2},
        ],
    },
    {
        "localized_aspect_name": "Parallel/Variety",
        "aspect_value_distributions": [
            {"localized_aspect_value": "Gold", "match_count": 12},
            {"localized_aspect_value": "[Base]", "match_count": 99},
        ],
    },
]


@pytest.fixture
def searches_yaml(tmp_path):
    def _write(**overrides):
        search = {"name": "s1", "query": "matt cain auto", "category_ids": ["261328"]}
        search.update(overrides)
        path = tmp_path / "searches.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "searches": [search]}))
        return path

    return _write


def item(item_id="v1|1|0", **overrides) -> ItemSummary:
    data = {
        "item_id": item_id,
        "title": "2012 Topps Matt Cain Auto /50",
        "price": {"value": "84.99", "currency": "USD"},
        "buying_options": ["FIXED_PRICE"],
        "item_web_url": f"https://ebay.com/itm/{item_id}",
        "condition": "Used",
        "seller": {"username": "giantscards", "feedback_score": 2140},
        "item_location": {"country": "US"},
    }
    data.update(overrides)
    return ItemSummary(**data)


def fetcher(items):
    return lambda search, limit: list(items)


class TestResolveSearch:
    def test_resolves_by_name(self, searches_yaml):
        s = resolve_search("s1", config_path=searches_yaml())
        assert s.query == "matt cain auto"

    def test_unknown_name_lists_what_is_defined(self, searches_yaml):
        with pytest.raises(KeyError, match="Defined: s1"):
            resolve_search("nope", config_path=searches_yaml())


class TestDescribeRequest:
    def test_reports_the_actual_ebay_request(self, searches_yaml):
        req = describe_request(resolve_search("s1", config_path=searches_yaml()))
        assert req["query"] == "matt cain auto"
        assert req["category_ids"] == "261328"
        assert req["sort"] == "newlyListed"
        assert "buyingOptions:{AUCTION|FIXED_PRICE}" in req["filter"]


class TestPreviewSearch:
    def test_returns_a_row_per_listing(self, searches_yaml):
        df = preview_search(
            "s1", config_path=searches_yaml(), fetch=fetcher([item("a"), item("b")])
        )
        assert len(df) == 2
        assert list(df.columns) == COLUMNS

    def test_passing_rows_have_no_reason(self, searches_yaml):
        df = preview_search("s1", config_path=searches_yaml(), fetch=fetcher([item()]))
        assert bool(df.loc[0, "passed"]) is True
        assert df.loc[0, "dropped_by"] is None

    def test_rejected_rows_are_kept_and_explained(self, searches_yaml):
        """The rejected rows are the whole point when tuning a filter."""
        path = searches_yaml(title_exclude=["reprint"])
        df = preview_search(
            "s1",
            config_path=path,
            fetch=fetcher([item("a"), item("b", title="Matt Cain REPRINT card")]),
        )
        assert len(df) == 2
        rejected = df[~df["passed"]]
        assert len(rejected) == 1
        assert rejected.iloc[0]["dropped_by"] == "title_exclude: 'reprint'"

    def test_passed_only_drops_them(self, searches_yaml):
        path = searches_yaml(title_exclude=["reprint"])
        df = preview_search(
            "s1",
            config_path=path,
            passed_only=True,
            fetch=fetcher([item("a"), item("b", title="Matt Cain REPRINT")]),
        )
        assert len(df) == 1

    def test_total_price_includes_shipping(self, searches_yaml):
        listing = item(shipping_options=[{"shipping_cost": {"value": "4.50", "currency": "USD"}}])
        df = preview_search("s1", config_path=searches_yaml(), fetch=fetcher([listing]))
        assert df.loc[0, "price"] == 84.99
        assert df.loc[0, "shipping"] == 4.50
        assert round(df.loc[0, "total_price"], 2) == 89.49

    def test_missing_price_does_not_crash(self, searches_yaml):
        df = preview_search("s1", config_path=searches_yaml(), fetch=fetcher([item(price=None)]))
        assert df.loc[0, "price"] is None
        assert df.loc[0, "total_price"] is None

    def test_empty_results_keep_the_shape(self, searches_yaml):
        df = preview_search("s1", config_path=searches_yaml(), fetch=fetcher([]))
        assert df.empty
        assert list(df.columns) == COLUMNS

    def test_defaults_to_seed_breadth(self, searches_yaml):
        """Preview should show what a seed would see, not one poll window."""
        seen = {}

        def capture(search, limit):
            seen["limit"] = limit
            return []

        preview_search("s1", config_path=searches_yaml(), fetch=capture)
        assert seen["limit"] == 2000

    def test_max_results_overrides(self, searches_yaml):
        seen = {}

        def capture(search, limit):
            seen["limit"] = limit
            return []

        preview_search("s1", config_path=searches_yaml(), max_results=25, fetch=capture)
        assert seen["limit"] == 25

    def test_reason_counts_are_groupable(self, searches_yaml):
        path = searches_yaml(title_exclude=["reprint", "lot"])
        df = preview_search(
            "s1",
            config_path=path,
            fetch=fetcher(
                [
                    item("a"),
                    item("b", title="Cain reprint"),
                    item("c", title="Cain reprint 2"),
                    item("d", title="Cain card lot"),
                ]
            ),
        )
        counts = df[~df["passed"]]["dropped_by"].value_counts().to_dict()
        assert counts["title_exclude: 'reprint'"] == 2
        assert counts["title_exclude: 'lot'"] == 1


class TestAspectOptions:
    def test_flattens_to_one_row_per_value(self, searches_yaml):
        df = aspect_options("s1", config_path=searches_yaml(), fetch=lambda s: ASPECTS_FIXTURE)
        assert len(df) == 4
        assert list(df.columns) == ["aspect", "value", "count", "values_in_aspect"]

    def test_sorted_by_aspect_then_count_desc(self, searches_yaml):
        df = aspect_options("s1", config_path=searches_yaml(), fetch=lambda s: ASPECTS_FIXTURE)
        parallel = df[df["aspect"] == "Parallel/Variety"]
        # Most common value first, so the useful ones are at the top.
        assert list(parallel["value"]) == ["[Base]", "Gold"]
        assert list(df["aspect"])[0] == "Parallel/Variety"

    def test_values_in_aspect_counts_the_whole_group(self, searches_yaml):
        df = aspect_options("s1", config_path=searches_yaml(), fetch=lambda s: ASPECTS_FIXTURE)
        assert set(df["values_in_aspect"]) == {2}

    def test_empty_response_keeps_shape(self, searches_yaml):
        df = aspect_options("s1", config_path=searches_yaml(), fetch=lambda s: [])
        assert df.empty
        assert list(df.columns) == ["aspect", "value", "count", "values_in_aspect"]

    def test_aspect_with_no_values_is_skipped(self, searches_yaml):
        df = aspect_options(
            "s1",
            config_path=searches_yaml(),
            fetch=lambda s: [{"localized_aspect_name": "Empty", "aspect_value_distributions": []}],
        )
        assert df.empty

    def test_missing_distribution_key_does_not_crash(self, searches_yaml):
        df = aspect_options(
            "s1",
            config_path=searches_yaml(),
            fetch=lambda s: [{"localized_aspect_name": "Odd"}],
        )
        assert df.empty

    def test_scoped_to_the_named_search(self, searches_yaml):
        seen = {}

        def capture(search):
            seen["query"] = search.query
            return ASPECTS_FIXTURE

        aspect_options("s1", config_path=searches_yaml(), fetch=capture)
        assert seen["query"] == "matt cain auto"


class TestGroupingReasons:
    def test_reasons_group_by_config_key(self, searches_yaml):
        """Reasons embed the offending value, so the summary groups on the key.

        Grouping on the full reason would print one line per listing for any
        numeric filter and say nothing about which filter is expensive.
        """
        path = searches_yaml(seller_min_feedback_score=99999)
        df = preview_search(
            "s1",
            config_path=path,
            fetch=fetcher(
                [
                    item("a", seller={"username": "x", "feedback_score": 10}),
                    item("b", seller={"username": "y", "feedback_score": 20}),
                ]
            ),
        )
        rejected = df[~df["passed"]]
        # Distinct full reasons ...
        assert rejected["dropped_by"].nunique() == 2
        # ... but one group.
        keys = rejected["dropped_by"].str.split(":").str[0].value_counts()
        assert keys.to_dict() == {"seller_min_feedback_score": 2}


class TestRejectionReasons:
    """Each reason names the config key and the offending value."""

    def test_feedback_reason_shows_both_numbers(self, searches_yaml):
        path = searches_yaml(seller_min_feedback_score=5000)
        df = preview_search("s1", config_path=path, fetch=fetcher([item()]))
        assert df.loc[0, "dropped_by"] == "seller_min_feedback_score: 2140 < 5000"

    def test_unknown_feedback_is_distinguished(self, searches_yaml):
        path = searches_yaml(seller_min_feedback_score=10)
        df = preview_search("s1", config_path=path, fetch=fetcher([item(seller=None)]))
        assert "feedback unknown" in df.loc[0, "dropped_by"]

    def test_total_price_reason_shows_the_total(self, searches_yaml):
        path = searches_yaml(max_total_price=50)
        listing = item(shipping_options=[{"shipping_cost": {"value": "5.00", "currency": "USD"}}])
        df = preview_search("s1", config_path=path, fetch=fetcher([listing]))
        assert df.loc[0, "dropped_by"] == "max_total_price: 89.99 > 50"

    def test_must_include_all_names_the_missing_terms(self, searches_yaml):
        path = searches_yaml(title_must_include_all=["psa", "gem"])
        df = preview_search("s1", config_path=path, fetch=fetcher([item()]))
        assert df.loc[0, "dropped_by"] == "title_must_include_all: missing 'psa', 'gem'"

    def test_country_reason(self, searches_yaml):
        path = searches_yaml(item_location_countries=["US", "CA"])
        df = preview_search(
            "s1", config_path=path, fetch=fetcher([item(item_location={"country": "GB"})])
        )
        assert df.loc[0, "dropped_by"] == "item_location_countries: GB not allowed"

    def test_first_failing_filter_wins(self, searches_yaml):
        path = searches_yaml(title_exclude=["topps"], seller_min_feedback_score=99999)
        df = preview_search("s1", config_path=path, fetch=fetcher([item()]))
        assert df.loc[0, "dropped_by"].startswith("title_exclude")
