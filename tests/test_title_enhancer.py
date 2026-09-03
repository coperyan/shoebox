import numpy as np
import pandas as pd

from shoebox.pipelines.enhance_listing_titles import build_title_changes, extract_team
from shoebox.transforms.title_enhancer import (
    MAX_TITLE_LENGTH,
    apply_shorthand,
    enhance_title,
    has_illegal_characters,
    normalize_characters,
    strip_phrases,
    title_names_team,
)
from shoebox.utils.title_crosswalk import (
    known_teams,
    shorten_team_name,
    shorten_team_names,
    split_team_values,
)


def expand_shorthand(title: str) -> str:
    """The rewritten title only -- these tests don't assert on change labels."""
    return apply_shorthand(title)[0]


class TestCrosswalk:
    def test_shortens_a_current_team(self):
        assert shorten_team_name("Arizona Diamondbacks") == "DBacks"

    def test_lookup_tolerates_casing_and_spacing(self):
        assert shorten_team_name("  los angeles  DODGERS ") == "Dodgers"

    def test_unmapped_team_returns_none(self):
        assert shorten_team_name("Toledo Mud Hens") is None
        assert shorten_team_name(None) is None

    def test_every_league_is_loaded(self):
        teams = known_teams()
        assert teams["New York Yankees"] == "Yankees"
        assert teams["Green Bay Packers"] == "Packers"
        assert teams["Golden State Warriors"] == "Warriors"

    def test_splits_multi_team_values(self):
        assert split_team_values("Detroit Tigers | Houston Astros") == [
            "Detroit Tigers",
            "Houston Astros",
        ]
        assert split_team_values(["Chicago Cubs"]) == ["Chicago Cubs"]

    def test_two_names_for_one_franchise_collapse(self):
        assert shorten_team_names("Brooklyn Dodgers | Los Angeles Dodgers") == ["Dodgers"]


class TestExpandShorthand:
    def test_parenthesized_rookie(self):
        assert expand_shorthand("2025 Topps - Jones #12 (RC)") == "2025 Topps - Jones #12 Rookie"

    def test_bare_rookie(self):
        assert expand_shorthand("2025 Topps - Jones #12 RC") == "2025 Topps - Jones #12 Rookie"

    def test_grouped_tokens(self):
        assert expand_shorthand("2025 Topps - Jones (AU, RC)") == "2025 Topps - Jones Auto Rookie"

    def test_unexpanded_tokens_keep_their_place(self):
        assert expand_shorthand("2025 Topps - Jones (RC,SP)") == "2025 Topps - Jones Rookie SP"

    def test_mem_is_removed_entirely(self):
        assert expand_shorthand("2025 Donruss - Threads Relic Odunze #9 (MEM)") == (
            "2025 Donruss - Threads Relic Odunze #9"
        )

    def test_mem_is_removed_from_a_group(self):
        assert expand_shorthand("2025 Topps - Jones #9 (MEM,RC)") == "2025 Topps - Jones #9 Rookie"

    def test_rookie_is_not_added_twice(self):
        assert expand_shorthand("2025 Donruss - Rookie Revolution Carter #11 (RC)") == (
            "2025 Donruss - Rookie Revolution Carter #11"
        )

    def test_auto_is_not_added_when_autographs_is_present(self):
        assert expand_shorthand("2024 Topps Chrome - Autographs Luciano #AC-ML AU RC") == (
            "2024 Topps Chrome - Autographs Luciano #AC-ML Rookie"
        )

    def test_a_token_left_unexpanded_keeps_its_original_spelling(self):
        # The lookup key is uppercase; the title must not be.
        assert apply_shorthand("2025 Topps - Refractor Auto Bailey /150", expand=False)[0] == (
            "2025 Topps - Refractor Auto Bailey /150"
        )

    def test_card_numbers_are_not_expanded(self):
        assert expand_shorthand("2025 Topps - Jones #RC-6 Refractor") == (
            "2025 Topps - Jones #RC-6 Refractor"
        )

    def test_non_token_groups_are_left_alone(self):
        assert expand_shorthand("2025 Topps - Jones (107/182)") == "2025 Topps - Jones (107/182)"

    def test_words_containing_tokens_are_untouched(self):
        assert expand_shorthand("2025 Topps Archives - Aucoin RCS Foil") == (
            "2025 Topps Archives - Aucoin RCS Foil"
        )


class TestNormalizeCharacters:
    def test_repairs_utf8_read_as_latin1(self):
        # Live on eBay as item 317847408694.
        assert normalize_characters("Vidal BrujÃ¡n") == "Vidal Brujan"

    def test_repairs_multibyte_damage(self):
        assert normalize_characters("Nikola JokiÄ\x87") == "Nikola Jokic"

    def test_repairs_smart_quotes(self):
        assert normalize_characters("Yongxi â\x80\x9cJackyâ\x80\x9d Cui") == ('Yongxi "Jacky" Cui')

    def test_folds_correctly_encoded_accents(self):
        assert normalize_characters("José Ramírez") == "Jose Ramirez"

    def test_undamaged_text_survives_the_round_trip(self):
        # Latin-1 encodable but not valid UTF-8 -- must not be "repaired".
        assert normalize_characters("Montréal Expos") == "Montreal Expos"

    def test_ascii_is_left_alone(self):
        assert normalize_characters("2025 Topps - Jones #12") == "2025 Topps - Jones #12"

    def test_detects_illegal_characters(self):
        assert has_illegal_characters("Vidal BrujÃ¡n")
        assert not has_illegal_characters("Vidal Brujan")


class TestStripPhrases:
    def test_removes_configured_phrase(self):
        assert strip_phrases("2025 Topps Update - 1990 Topps Baseball Lou Gehrig #U90-14") == (
            "2025 Topps Update - 1990 Lou Gehrig #U90-14"
        )

    def test_leaves_other_titles_alone(self):
        title = "2025-26 Topps Basketball - Jones #12"
        assert strip_phrases(title) == title


class TestTitleNamesTeam:
    def test_matches_the_short_name(self):
        assert title_names_team("2025 Topps - Judge #1 Yankees", "New York Yankees")

    def test_matches_the_nickname_in_full(self):
        assert title_names_team("2026 Topps - Carroll #5 Diamondbacks", "Arizona Diamondbacks")

    def test_no_match_when_absent(self):
        assert not title_names_team("2026 Topps - Carroll #5 Gold Foil", "Arizona Diamondbacks")


class TestEnhanceTitle:
    def test_adds_team_and_expands_shorthand(self):
        result = enhance_title(
            "2025 Topps Chrome - Zebby Matthews #277 Raywave Refractor (RC)",
            "Minnesota Twins",
        )
        assert result.title == (
            "2025 Topps Chrome - Zebby Matthews #277 Raywave Refractor Twins Rookie"
        )
        assert result.changes == ["expanded_shorthand", "added_team"]

    def test_team_goes_before_the_print_run(self):
        result = enhance_title("2025 Topps - Jones #12 Gold /150", "Chicago Cubs")
        assert result.title == "2025 Topps - Jones #12 Gold Cubs /150"

    def test_team_already_present_is_not_repeated(self):
        result = enhance_title("2025 Topps - Jones #12 Cubs", "Chicago Cubs")
        assert result.title == "2025 Topps - Jones #12 Cubs"
        assert "team_already_in_title" in result.notes
        assert not result.changed

    def test_unmapped_team_is_flagged_not_guessed(self):
        result = enhance_title("2025 Topps - Jones #12 Gold", "Toledo Mud Hens")
        assert result.title == "2025 Topps - Jones #12 Gold"
        assert "team_not_in_crosswalk" in result.notes

    def test_multi_team_card_keeps_its_title(self):
        result = enhance_title(
            "2025 Topps - AL ERA Leaders #5 Foil", ["Detroit Tigers", "Houston Astros"]
        )
        assert "multiple_teams_skipped" in result.notes
        assert not result.changed

    def test_mojibake_is_repaired_and_the_team_added(self):
        result = enhance_title(
            "2025 Topps Heritage - Vidal BrujÃ¡n #82 Chrome Blue Sparkle Refractor",
            "Miami Marlins",
        )
        assert result.title == (
            "2025 Topps Heritage - Vidal Brujan #82 Chrome Blue Sparkle Refractor Marlins"
        )
        assert "normalized_characters" in result.changes

    def test_phrase_removal_frees_up_room(self):
        # item 318204871003 -- 79 chars before, and it fits once the phrase goes.
        result = enhance_title(
            "2026 Topps Series 1 - 1990 Topps Baseball Chrome Jhostynxon Garcia #91C-23 (RC)",
            "Boston Red Sox",
        )
        assert result.title == (
            "2026 Topps Series 1 - 1990 Chrome Jhostynxon Garcia #91C-23 Red Sox Rookie"
        )
        assert "removed_phrase" in result.changes

    def test_a_parallel_named_rookie_is_not_split_by_the_team(self):
        result = enhance_title("2025 Bowman Chrome - Acuna #32 Red Rookie (RC)", "New York Mets")
        assert result.title == "2025 Bowman Chrome - Acuna #32 Red Rookie Mets"

    def test_separator_goes_first_then_the_card_number(self):
        result = enhance_title(
            "2024 Topps Archives - 1995 Fan Favorites Autographs Wade Meckler #95FF-WM AU RC",
            "San Francisco Giants",
        )
        assert result.title == (
            "2024 Topps Archives 1995 Fan Favorites Autographs Wade Meckler Giants Rookie"
        )
        assert "removed_separator" in result.changes
        assert "removed_card_number" in result.changes

    def test_separator_alone_is_enough_when_it_fits(self):
        result = enhance_title(
            "2026 Topps Chrome - Rookie Autographs Green RayWave Redemption Nolan McLean /99",
            "New York Mets",
        )
        assert result.length <= MAX_TITLE_LENGTH
        assert " - " not in result.title
        assert "#" not in result.title or "removed_card_number" not in result.changes

    def test_team_is_dropped_before_the_expansion(self):
        long_title = "2025 Topps Archives - 1987 Boardwalk and Baseball Dylan Crews #87BB-4 (RC)"
        result = enhance_title(long_title, "Washington Nationals")
        assert result.length <= MAX_TITLE_LENGTH
        assert result.title.endswith("Rookie")

    def test_never_exceeds_the_ebay_limit(self):
        result = enhance_title("2026 Topps Heritage - Tanner Bibee #74 Chrome (RC)", "Cleveland")
        assert result.length <= MAX_TITLE_LENGTH


class TestBuildTitleChanges:
    def _frame(self, **overrides) -> pd.DataFrame:
        row = {
            "item_id": "1",
            "sku": "SKU1",
            "title": "2025 Topps Chrome - Jones #12 Gold (RC)",
            "team": "Chicago Cubs",
        }
        row.update(overrides)
        return pd.DataFrame([row])

    def test_reports_a_change(self):
        changes = build_title_changes(self._frame())
        assert changes.loc[0, "changed"]
        assert changes.loc[0, "new_title"] == "2025 Topps Chrome - Jones #12 Gold Cubs Rookie"
        assert changes.loc[0, "team_short"] == "Cubs"

    def test_variation_listings_are_skipped(self):
        changes = build_title_changes(
            self._frame(title="Complete Your Set - 2025 Topps Series 1 (RC)")
        )
        assert not changes.loc[0, "changed"]
        assert changes.loc[0, "skip_reason"] == "variation_listing"

    def test_rows_without_a_sku_route_through_trading(self):
        changes = build_title_changes(self._frame(sku=""))
        assert changes.loc[0, "changed"]
        assert changes.loc[0, "update_method"] == "trading"
        assert changes.loc[0, "skip_reason"] == ""

    def test_a_missing_sku_reads_as_blank_not_nan(self):
        # pandas hands back NaN for an absent SKU; str(nan) is a truthy "nan".
        changes = build_title_changes(self._frame(sku=float("nan")))
        assert changes.loc[0, "sku"] == ""
        assert changes.loc[0, "update_method"] == "trading"

    def test_rows_with_neither_identifier_are_skipped(self):
        changes = build_title_changes(self._frame(sku="", item_id=""))
        assert not changes.loc[0, "changed"]
        assert changes.loc[0, "skip_reason"] == "no_sku_or_item_id"

    def test_team_falls_back_to_item_specifics(self):
        row = pd.Series(
            {
                "title": "2025 Topps - Jones #12",
                "item_specifics": {"Team": ["Chicago Cubs"], "Sport": ["Baseball"]},
            }
        )
        assert extract_team(row) == ["Chicago Cubs"]

    def test_team_parses_json_item_specifics(self):
        row = pd.Series(
            {"title": "t", "item_specifics": '{"Team": ["New York Mets"]}', "team": None}
        )
        assert extract_team(row) == ["New York Mets"]


class TestApplyPath:
    """The eBay write path, exercised without touching the API."""

    def _item(self):
        from shoebox.models.ebay.inventory_item import InventoryItem

        return InventoryItem.from_api(
            {
                "sku": "SKU1",
                "condition": "USED_VERY_GOOD",
                "condition_descriptors": [{"name": "40001", "values": ["400010"]}],
                "availability": {"ship_to_location_availability": {"quantity": 2}},
                "package_weight_and_size": {
                    "dimensions": {"height": 1, "length": 7, "width": 5, "unit": "INCH"},
                    "package_type": "LETTER",
                    "weight": {"unit": "OUNCE", "value": 1},
                },
                "product": {
                    "title": "Old title",
                    "description": "<div>Set: 2025 Topps</div>",
                    "aspects": {"Team": ["Chicago Cubs"], "Card Number": "12"},
                    "image_urls": ["https://example.com/front.png"],
                },
            }
        )

    def test_body_changes_only_the_title(self):
        from shoebox.transforms.listing_builder import inventory_item_body_with_title

        body = inventory_item_body_with_title(self._item(), "New title")

        assert body["product"]["title"] == "New title"
        # Everything else round-trips untouched.
        assert body["product"]["description"] == "<div>Set: 2025 Topps</div>"
        assert body["product"]["imageUrls"] == ["https://example.com/front.png"]
        assert body["availability"]["shipToLocationAvailability"]["quantity"] == 2
        assert body["condition"] == "USED_VERY_GOOD"
        assert body["packageWeightAndSize"]["packageType"] == "LETTER"

    def test_aspects_are_sent_as_arrays(self):
        from shoebox.transforms.listing_builder import inventory_item_body_with_title

        aspects = inventory_item_body_with_title(self._item(), "New title")["product"]["aspects"]
        assert aspects == {"Team": ["Chicago Cubs"], "Card Number": ["12"]}

    def test_incomplete_packaging_falls_back_to_the_standard_package(self):
        from shoebox.models.ebay.inventory_item import InventoryItem
        from shoebox.transforms.listing_builder import inventory_item_body_with_title

        # eBay rejects an update carrying a weightless package (errorId 25020).
        item = InventoryItem.from_api(
            {
                "sku": "SKU1",
                "package_weight_and_size": {"package_type": "LETTER"},
                "product": {"title": "Old", "aspects": {}},
            }
        )
        weight = inventory_item_body_with_title(item, "New")["packageWeightAndSize"]["weight"]
        assert weight["value"] == 1
        assert weight["unit"] == "OUNCE"

    def test_apply_updates_changed_rows_and_survives_failures(self):
        from shoebox.pipelines.enhance_listing_titles import apply_title_changes

        class FakeEbay:
            def __init__(self):
                self.calls = []

            def update_listing_title(self, *, new_title, sku=None, item_id=None):
                self.calls.append((sku, item_id, new_title))
                if sku == "BAD":
                    raise RuntimeError("eBay said no")
                return {"sku": sku, "method": "inventory" if sku else "trading"}

        changes = pd.DataFrame(
            [
                {"sku": "GOOD", "item_id": "1", "new_title": "T1", "changed": True},
                {"sku": "BAD", "item_id": "2", "new_title": "T2", "changed": True},
                {"sku": "SKIP", "item_id": "3", "new_title": "T3", "changed": False},
            ]
        )
        fake = FakeEbay()
        applied = apply_title_changes(changes, ebay_api=fake)

        assert [sku for sku, _, _ in fake.calls] == ["GOOD", "BAD"]
        assert applied.set_index("sku")["status"].to_dict() == {
            "GOOD": "updated",
            "BAD": "failed",
        }

    def test_skuless_rows_are_sent_by_item_id(self):
        from shoebox.pipelines.enhance_listing_titles import apply_title_changes

        class FakeEbay:
            def __init__(self):
                self.calls = []

            def update_listing_title(self, *, new_title, sku=None, item_id=None):
                self.calls.append((sku, item_id))
                return {"method": "trading"}

        changes = pd.DataFrame(
            [{"sku": "", "item_id": "318590003811", "new_title": "T", "changed": True}]
        )
        fake = FakeEbay()
        applied = apply_title_changes(changes, ebay_api=fake)

        assert fake.calls == [(None, "318590003811")]
        assert applied.loc[0, "applied_method"] == "trading"

    def test_report_keeps_one_row_per_listing(self):
        from shoebox.pipelines.enhance_listing_titles import apply_title_changes

        class FakeEbay:
            def update_listing_title(self, *, new_title, sku=None, item_id=None):
                return {"method": "trading"}

        # Blank SKUs are not a join key -- merging on them multiplies the rows.
        changes = pd.DataFrame(
            [{"sku": "", "item_id": str(i), "new_title": "T", "changed": True} for i in range(20)]
        )
        applied = apply_title_changes(changes, ebay_api=FakeEbay())
        assert len(applied) == 20

    def test_limit_caps_the_number_of_updates(self):
        from shoebox.pipelines.enhance_listing_titles import apply_title_changes

        class FakeEbay:
            def __init__(self):
                self.calls = []

            def update_listing_title(self, *, new_title, sku=None, item_id=None):
                self.calls.append(sku)

        changes = pd.DataFrame(
            [
                {"sku": f"S{i}", "item_id": str(i), "new_title": "T", "changed": True}
                for i in range(5)
            ]
        )
        fake = FakeEbay()
        apply_title_changes(changes, ebay_api=fake, limit=2)
        assert fake.calls == ["S0", "S1"]


class TestUpdateRouting:
    """EbayClient.update_listing_title picks its API by what identifies the listing."""

    def _client(self, *, inventory_raises=None):
        from shoebox.clients.ebay.client import EbayClient
        from shoebox.models.ebay.inventory_item import InventoryItem

        client = EbayClient.__new__(EbayClient)  # no network, no credentials
        calls = {"inventory": [], "trading": []}

        class FakeInventory:
            def get_inventory_item(self, sku):
                return InventoryItem.from_api(
                    {"sku": sku, "product": {"title": "Old title", "aspects": {}}}
                )

            def upsert_inventory_item(self, sku, body):
                calls["inventory"].append((sku, body["product"]["title"]))
                if inventory_raises:
                    raise inventory_raises

        class FakeLegacy:
            def revise_listing_title(self, item_id, title):
                calls["trading"].append((item_id, title))
                return {"ack": "Success"}

        client.inventory = FakeInventory()
        client.trading = FakeLegacy()
        return client, calls

    def test_sku_goes_through_the_inventory_api(self):
        client, calls = self._client()
        out = client.update_listing_title(sku="SKU1", item_id="1", new_title="New title")
        assert calls["inventory"] == [("SKU1", "New title")]
        assert calls["trading"] == []
        assert out["method"] == "inventory"

    def test_no_sku_goes_through_trading(self):
        client, calls = self._client()
        out = client.update_listing_title(item_id="318590003811", new_title="New title")
        assert calls["inventory"] == []
        assert calls["trading"] == [("318590003811", "New title")]
        assert out["method"] == "trading"

    def test_inventory_failure_falls_back_to_trading(self):
        client, calls = self._client(inventory_raises=RuntimeError("errorId 25020"))
        out = client.update_listing_title(sku="SKU1", item_id="99", new_title="New title")
        assert calls["trading"] == [("99", "New title")]
        assert out["method"] == "trading_fallback"

    def test_inventory_failure_without_an_item_id_raises(self):
        import pytest

        client, _ = self._client(inventory_raises=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            client.update_listing_title(sku="SKU1", new_title="New title")

    def test_neither_identifier_is_an_error(self):
        import pytest

        client, _ = self._client()
        with pytest.raises(ValueError):
            client.update_listing_title(new_title="New title")


class TestBigQuerySource:
    """With no --input, listings come from the BigQuery view."""

    def test_queries_the_view_with_the_configured_dataset(self):
        from shoebox.pipelines.enhance_listing_titles import load_listings_from_bigquery

        calls = {}

        class FakeBQ:
            def run_query(self, sql, params=None, *, return_df=True):
                calls.update(sql=sql, params=params, return_df=return_df)
                return pd.DataFrame([{"item_id": "1", "sku": "S", "title": "t", "team": "Cubs"}])

        df = load_listings_from_bigquery(FakeBQ())

        assert calls["sql"] == "active_listing_details.sql"
        # conftest points settings at app.example.yml -> ebay_dataset: ebay
        assert calls["params"] == {"dataset": "ebay"}
        assert calls["return_df"] is True
        assert len(df) == 1

    def test_a_view_without_a_title_column_is_rejected(self):
        import pytest

        from shoebox.pipelines.enhance_listing_titles import load_listings_from_bigquery

        class FakeBQ:
            def run_query(self, sql, params=None, *, return_df=True):
                return pd.DataFrame([{"item_id": "1", "sku": "S"}])

        with pytest.raises(ValueError, match="title"):
            load_listings_from_bigquery(FakeBQ())

    def test_the_sql_file_ships_with_the_repo(self):
        from pathlib import Path

        sql = Path("configs/bigquery/queries/active_listing_details.sql").read_text()
        assert "{dataset}" in sql
        assert "v_active_listing_details" in sql

    def test_repeated_team_columns_are_read(self):
        from shoebox.pipelines.enhance_listing_titles import extract_team

        # BigQuery hands a REPEATED column back as a numpy array, not a list.
        row = pd.Series({"title": "t", "team": np.array(["Chicago Cubs", "New York Mets"])})
        assert extract_team(row) == ["Chicago Cubs", "New York Mets"]

    def test_a_null_team_falls_through_to_item_specifics(self):
        from shoebox.pipelines.enhance_listing_titles import extract_team

        row = pd.Series({"title": "t", "team": pd.NA, "item_specifics": {"Team": ["Chicago Cubs"]}})
        assert extract_team(row) == ["Chicago Cubs"]
