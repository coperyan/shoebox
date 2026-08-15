from decimal import Decimal

import pytest

from shoebox.models.ebay.item_summary import ItemSummary
from shoebox.models.saved_search import SearchesFile
from shoebox.transforms.search_filters import (
    build_aspect_filter,
    build_browse_filter,
    cheapest_shipping,
    filter_items,
    passes_post_filters,
    query_terms,
    rejection_reason,
)


def resolve(**overrides):
    search = {"name": "s1", "query": "jordan"}
    search.update(overrides)
    return SearchesFile(version=1, searches=[search]).resolved()[0]


def item(**overrides) -> ItemSummary:
    data = {
        "item_id": "v1|123|0",
        "title": "1986 Fleer Michael Jordan Rookie",
        "price": {"value": "100.00", "currency": "USD"},
        "buying_options": ["FIXED_PRICE"],
        "item_web_url": "https://ebay.com/itm/123",
    }
    data.update(overrides)
    return ItemSummary(**data)


class TestItemSummaryNullLists:
    """eBay sends explicit nulls for array fields on some listings.

    A default_factory only covers the *omitted* case, so an explicit null used
    to raise and take down the entire search mid-page.
    """

    @pytest.mark.parametrize(
        "field",
        [
            "shipping_options",
            "buying_options",
            "leaf_category_ids",
            "categories",
            "thumbnail_images",
        ],
    )
    def test_explicit_null_becomes_empty_list(self, field):
        parsed = ItemSummary.from_api({"item_id": "v1|1|0", "title": "x", field: None})
        assert getattr(parsed, field) == []

    def test_null_shipping_does_not_break_helpers(self):
        parsed = ItemSummary.from_api({"item_id": "v1|1|0", "shipping_options": None})
        assert parsed.free_shipping is False
        assert cheapest_shipping(parsed) is None

    def test_null_buying_options_does_not_break_helpers(self):
        parsed = ItemSummary.from_api({"item_id": "v1|1|0", "buying_options": None})
        assert parsed.is_auction is False
        assert parsed.is_buy_it_now is False

    def test_omitted_fields_still_default(self):
        parsed = ItemSummary.from_api({"item_id": "v1|1|0"})
        assert parsed.shipping_options == []
        assert parsed.buying_options == []


class TestBuildBrowseFilter:
    def test_buying_options_always_emitted(self):
        # Omitting it makes eBay return FIXED_PRICE only -- silently.
        assert build_browse_filter(resolve()) is not None
        assert "buyingOptions:{AUCTION|FIXED_PRICE}" in build_browse_filter(resolve())

    def test_price_range_with_currency_autoinjected(self):
        f = build_browse_filter(resolve(price={"min": 25, "max": 200}))
        assert "price:[25..200]" in f
        assert "priceCurrency:USD" in f

    def test_price_min_only(self):
        assert "price:[25..]" in build_browse_filter(resolve(price={"min": 25}))

    def test_price_max_only(self):
        assert "price:[..200]" in build_browse_filter(resolve(price={"max": 200}))

    def test_price_decimals_preserved(self):
        assert "price:[..19.99]" in build_browse_filter(resolve(price={"max": "19.99"}))

    def test_set_values_sorted_for_determinism(self):
        f = build_browse_filter(resolve(conditions=["USED", "NEW"]))
        assert "conditions:{NEW|USED}" in f

    def test_single_location_country_goes_server_side(self):
        assert "itemLocationCountry:US" in build_browse_filter(
            resolve(item_location_countries=["US"])
        )

    def test_multi_location_country_omitted_from_filter(self):
        # eBay's itemLocationCountry takes one value; this falls to a post-filter.
        f = build_browse_filter(resolve(item_location_countries=["US", "CA"]))
        assert "itemLocationCountry" not in f

    def test_free_shipping_uses_max_delivery_cost(self):
        assert "maxDeliveryCost:0" in build_browse_filter(resolve(free_shipping_only=True))

    def test_exclude_sellers(self):
        f = build_browse_filter(resolve(exclude_sellers=["bad2", "bad1"]))
        assert "excludeSellers:{bad1|bad2}" in f

    def test_golden_full_string(self):
        f = build_browse_filter(
            resolve(
                price={"min": 25, "max": 200},
                buying_options=["FIXED_PRICE"],
                conditions=["USED"],
                exclude_sellers=["spammer"],
            )
        )
        assert f == (
            "price:[25..200],priceCurrency:USD,buyingOptions:{FIXED_PRICE},"
            "conditions:{USED},itemLocationCountry:US,deliveryCountry:US,"
            "excludeSellers:{spammer}"
        )

    def test_no_spaces_anywhere(self):
        # eBay rejects whitespace inside the filter string.
        assert " " not in build_browse_filter(resolve(price={"min": 1, "max": 2}))


class TestBuildAspectFilter:
    def test_none_when_no_aspects(self):
        assert build_aspect_filter(resolve()) is None

    def test_category_id_prefix_required(self):
        f = build_aspect_filter(resolve(category_ids=["261328"], aspects={"Grade": ["10"]}))
        assert f == "categoryId:261328,Grade:{10}"

    def test_multiple_values_and_keys_sorted(self):
        f = build_aspect_filter(
            resolve(
                category_ids=["261328"],
                aspects={"Grader": ["PSA", "BGS"], "Grade": ["10"]},
            )
        )
        assert f == "categoryId:261328,Grade:{10},Grader:{PSA|BGS}"

    def test_pipe_in_value_escaped(self):
        f = build_aspect_filter(resolve(category_ids=["261328"], aspects={"Brand": ["Bed|Stu"]}))
        assert r"Bed\|Stu" in f


class TestPostFilters:
    def test_title_exclude_is_case_insensitive(self):
        s = resolve(title_exclude=["REPRINT"])
        assert not passes_post_filters(item(title="Jordan Reprint Card"), s)
        assert passes_post_filters(item(title="Jordan Rookie"), s)

    def test_title_must_include_all(self):
        s = resolve(title_must_include_all=["jordan", "rookie"])
        assert passes_post_filters(item(title="Michael Jordan Rookie Card"), s)
        assert not passes_post_filters(item(title="Michael Jordan Card"), s)

    def test_title_must_include_any(self):
        s = resolve(title_must_include_any=["psa", "bgs"])
        assert passes_post_filters(item(title="Jordan PSA 10"), s)
        assert not passes_post_filters(item(title="Jordan Raw"), s)

    def test_missing_title_treated_as_empty(self):
        assert not passes_post_filters(item(title=None), resolve(title_must_include_all=["x"]))

    def test_seller_feedback_threshold(self):
        s = resolve(seller_min_feedback_score=50)
        assert passes_post_filters(item(seller={"username": "a", "feedback_score": 50}), s)
        assert not passes_post_filters(item(seller={"username": "a", "feedback_score": 49}), s)

    def test_unknown_feedback_fails_an_explicit_threshold(self):
        s = resolve(seller_min_feedback_score=50)
        assert not passes_post_filters(item(seller={"username": "a"}), s)
        assert not passes_post_filters(item(), s)

    def test_multi_country_location_post_filtered(self):
        s = resolve(item_location_countries=["US", "CA"])
        assert passes_post_filters(item(item_location={"country": "CA"}), s)
        assert not passes_post_filters(item(item_location={"country": "GB"}), s)

    def test_max_total_price_includes_shipping(self):
        s = resolve(max_total_price=110)
        cheap = item(shipping_options=[{"shipping_cost": {"value": "5.00", "currency": "USD"}}])
        pricey = item(shipping_options=[{"shipping_cost": {"value": "15.00", "currency": "USD"}}])
        assert passes_post_filters(cheap, s)
        assert not passes_post_filters(pricey, s)

    def test_max_total_price_with_no_shipping_quote(self):
        assert passes_post_filters(item(), resolve(max_total_price=110))

    def test_cheapest_shipping_picks_minimum(self):
        i = item(
            shipping_options=[
                {"shipping_cost": {"value": "9.99", "currency": "USD"}},
                {"shipping_cost": {"value": "4.50", "currency": "USD"}},
            ]
        )
        assert cheapest_shipping(i) == Decimal("4.50")

    def test_no_filters_passes_everything(self):
        assert passes_post_filters(item(), resolve(title_exclude=[]))


class TestRequireQueryInTitle:
    """eBay pads thin result sets with looser matches ("results matching fewer
    words") — a 'tim lincecum auto' search intermittently returns every
    Lincecum listing. This re-check is what stops the flood."""

    def test_backfill_result_is_rejected(self):
        search = resolve(query="tim lincecum auto")
        padded = item(title="2010 Topps Tim Lincecum Base Card")
        assert passes_post_filters(padded, search) is False
        assert "require_query_in_title" in rejection_reason(padded, search)
        assert "'auto'" in rejection_reason(padded, search)

    def test_full_match_passes(self):
        search = resolve(query="tim lincecum auto")
        assert passes_post_filters(item(title="2010 Tim Lincecum AUTO /25"), search)

    def test_substring_accepts_autograph_for_auto(self):
        search = resolve(query="tim lincecum auto")
        assert passes_post_filters(item(title="Tim Lincecum Autograph SP"), search)

    def test_or_group_matches_any_alternative(self):
        search = resolve(query="lincecum (auto, autograph)")
        assert passes_post_filters(item(title="Lincecum Autograph"), search)
        assert passes_post_filters(item(title="Lincecum Auto"), search)
        assert not passes_post_filters(item(title="Lincecum Base"), search)

    def test_quoted_phrase_must_appear_whole(self):
        search = resolve(query='"tim lincecum" auto')
        assert passes_post_filters(item(title="Tim Lincecum Auto"), search)
        assert not passes_post_filters(item(title="Tim Wakefield Lincecum-style Auto"), search)

    def test_matching_is_case_insensitive(self):
        search = resolve(query="LINCECUM AUTO")
        assert passes_post_filters(item(title="tim lincecum auto /99"), search)

    def test_flag_off_lets_backfill_through(self):
        search = resolve(query="tim lincecum auto", require_query_in_title=False)
        assert passes_post_filters(item(title="2010 Topps Tim Lincecum Base"), search)

    def test_category_only_search_is_unaffected(self):
        search = resolve(query=None, category_ids=["261328"])
        assert passes_post_filters(item(title="anything at all"), search)

    def test_untitled_listing_is_rejected(self):
        search = resolve(query="jordan")
        assert not passes_post_filters(item(title=None), search)


class TestQueryTerms:
    def test_plain_terms_are_anded(self):
        assert query_terms("tim lincecum auto") == [["tim"], ["lincecum"], ["auto"]]

    def test_or_group(self):
        assert query_terms("lincecum (auto, autograph)") == [
            ["auto", "autograph"],
            ["lincecum"],
        ]

    def test_quoted_phrase_kept_whole(self):
        assert query_terms('"tim lincecum" auto') == [["tim lincecum"], ["auto"]]

    def test_empty_group_and_extra_spaces(self):
        assert query_terms("  jordan   ()  ") == [["jordan"]]


class TestFilterItems:
    def test_drops_and_keeps(self, caplog):
        s = resolve(title_exclude=["lot"])
        items = [item(title="Jordan Rookie"), item(title="Jordan Card Lot")]
        assert len(filter_items(items, s)) == 1

    def test_empty_input(self):
        assert filter_items([], resolve()) == []


@pytest.mark.parametrize("value,expected", [(Decimal("25"), "25"), (Decimal("19.99"), "19.99")])
def test_price_formatting_has_no_trailing_zeros(value, expected):
    f = build_browse_filter(resolve(price={"max": value}))
    assert f"price:[..{expected}]" in f
