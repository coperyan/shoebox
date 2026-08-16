"""Store category rules, and the export-shape tolerance around them."""

import pandas as pd

from shoebox.pipelines.plan_store_categories import (
    build_category_plan,
    category_counts,
    read_fields,
)
from shoebox.transforms.store_category_builder import (
    MAX_STORE_CATEGORIES,
    VARIATION_CATEGORY,
    category_path,
    hit_type,
    is_variation_listing,
    plan_store_category,
    resolve_sport,
)


class TestVariationListings:
    def test_you_pick_listing_takes_the_variation_branch(self):
        plan = plan_store_category(
            title="2025 Topps Update Base Cards - #US1-US175 Complete Your Set, You Pick!",
            team="Cincinnati Reds",
            sport="Baseball",
        )
        assert plan.categories == [f"/{VARIATION_CATEGORY}"]
        assert plan.is_variation

    def test_a_group_listing_gets_no_team_or_hit(self):
        # It spans every team in the set and has no one card to call a hit.
        plan = plan_store_category(
            title="2025 Bowman Chrome - Complete Your Set, You Pick!",
            team="New York Yankees",
            sport="Baseball",
            print_run="150",
        )
        assert plan.team is None
        assert plan.hit is None
        assert plan.secondary is None

    def test_marker_detection_is_case_insensitive(self):
        assert is_variation_listing("2025 Topps COMPLETE YOUR SET")
        assert is_variation_listing("2025 Topps - You Pick")
        assert not is_variation_listing("2025 Topps Chrome #43 Daniel Schneemann")
        assert not is_variation_listing(None)


class TestSingles:
    def test_baseball_is_broken_out_by_team(self):
        plan = plan_store_category(
            title="2025 Topps Chrome #43 Patrick Bailey Giants",
            team="San Francisco Giants",
            sport="Baseball",
        )
        assert plan.categories == ["/Baseball Singles/San Francisco Giants"]
        assert plan.team == "San Francisco Giants"

    def test_other_sports_stay_flat(self):
        for sport, team in (("Basketball", "Denver Nuggets"), ("Football", "Dallas Cowboys")):
            plan = plan_store_category(title="a card", team=team, sport=sport)
            assert plan.categories == [f"/{sport} Singles"]
            assert plan.team is None

    def test_team_variants_land_in_one_category(self):
        for name in ("Cleveland Indians", "Cleveland Guardians", "Cleveland"):
            plan = plan_store_category(title="a card", team=name, sport="Baseball")
            assert plan.primary == "/Baseball Singles/Cleveland Guardians"

    def test_unknown_team_falls_back(self):
        plan = plan_store_category(title="a card", team="Toledo Mud Hens", sport="Baseball")
        assert plan.primary == "/Baseball Singles/Other Teams"

    def test_two_team_card_files_under_the_first_and_is_flagged(self):
        plan = plan_store_category(
            title="a card",
            team="Detroit Tigers | Houston Astros",
            sport="Baseball",
        )
        assert plan.primary == "/Baseball Singles/Detroit Tigers"
        assert "multi_team" in plan.notes


class TestSportResolution:
    def test_team_name_beats_the_sport_specific(self):
        # These arrive tagged Sport=Baseball on a handful of live listings.
        plan = plan_store_category(title="a card", team="Denver Nuggets", sport="Baseball")
        assert plan.sport == "Basketball"
        assert plan.primary == "/Basketball Singles"

    def test_sport_specific_is_used_when_the_team_implies_none(self):
        sport, confident = resolve_sport(team="Ole Miss Rebels", sport="Baseball")
        assert (sport, confident) == ("Baseball", True)

    def test_unresolvable_sport_is_flagged_not_guessed(self):
        # The pipeline skips these before they reach the transform; the
        # transform still refuses to invent a branch if one slips through.
        plan = plan_store_category(title="a card", team=None, sport=None)
        assert plan.primary == "/Other Singles"
        assert "sport_unresolved" in plan.notes


class TestHits:
    def test_autograph_relic_and_numbered_each_get_a_second_category(self):
        cases = [
            ({"subset_name": "Rookie Autographs"}, "/Hits/Autographs"),
            ({"features": "Relic"}, "/Hits/Relics"),
            ({"print_run": "150"}, "/Hits/Numbered"),
        ]
        for kwargs, expected in cases:
            plan = plan_store_category(
                title="a card", team="San Francisco Giants", sport="Baseball", **kwargs
            )
            assert plan.categories == ["/Baseball Singles/San Francisco Giants", expected]

    def test_the_autographed_specific_is_honored(self):
        plan = plan_store_category(
            title="a card", team="New York Mets", sport="Baseball", autographed="Yes"
        )
        assert plan.hit == "Autographs"

    def test_precedence_runs_auto_then_relic_then_numbered(self):
        # An autographed, numbered relic is filed under Autographs alone.
        assert hit_type(subset_name="Rookie Autographs Relic", print_run="50") == "Autographs"
        assert hit_type(features="Relic", print_run="50") == "Relics"
        assert hit_type(print_run="50") == "Numbered"
        assert hit_type() is None

    def test_an_ordinary_card_gets_one_category(self):
        plan = plan_store_category(
            title="a card", team="Chicago Cubs", sport="Baseball", features="Base"
        )
        assert plan.hit is None
        assert len(plan.categories) == 1

    def test_hits_apply_to_every_sport(self):
        # Team breakout is baseball-only, but a basketball case hit is still
        # worth calling out -- so the flat branch picks up a second category.
        plan = plan_store_category(
            title="a card", team="Dallas Mavericks", sport="Basketball", print_run="99"
        )
        assert plan.hit == "Numbered"
        assert plan.categories == ["/Basketball Singles", "/Hits/Numbered"]

    def test_never_exceeds_the_ebay_cap(self):
        plan = plan_store_category(
            title="a card",
            team="Boston Red Sox",
            sport="Baseball",
            subset_name="Rookie Autographs",
            features="Relic | Serial Numbered",
            print_run="25",
            autographed="Yes",
        )
        assert len(plan.categories) <= MAX_STORE_CATEGORIES


class TestCategoryPath:
    def test_segments_join_with_leading_slashes(self):
        assert (
            category_path("Baseball Singles", "San Francisco Giants")
            == "/Baseball Singles/San Francisco Giants"
        )
        assert category_path("Complete Your Set - You Pick") == "/Complete Your Set - You Pick"

    def test_blank_segments_are_dropped(self):
        assert category_path("Hits", "") == "/Hits"


class TestReadFields:
    """The pipeline reads both the flattened view and the raw JSONL export."""

    def test_flattened_view_columns_win(self):
        row = pd.Series(
            {
                "title": "a card",
                "team": "San Francisco Giants",
                "sport": "Baseball",
                "item_specifics": {"Team": ["Chicago Cubs"]},
            }
        )
        fields = read_fields(row)
        assert fields["team"] == "San Francisco Giants"

    def test_falls_back_to_item_specifics(self):
        row = pd.Series(
            {
                "title": "a card",
                "item_specifics": {
                    "Team": ["Chicago Cubs"],
                    "Sport": ["Baseball"],
                    "Print Run": ["150"],
                },
            }
        )
        fields = read_fields(row)
        assert fields["team"] == "Chicago Cubs"
        assert fields["sport"] == "Baseball"
        assert fields["print_run"] == "150"

    def test_repeated_specifics_flatten_the_way_the_view_does(self):
        row = pd.Series(
            {"title": "a card", "item_specifics": {"Team": ["Detroit Tigers", "Houston Astros"]}}
        )
        assert read_fields(row)["team"] == "Detroit Tigers | Houston Astros"

    def test_json_string_specifics_are_parsed(self):
        row = pd.Series({"title": "a card", "item_specifics": '{"Team": ["Chicago Cubs"]}'})
        assert read_fields(row)["team"] == "Chicago Cubs"


class TestBuildCategoryPlan:
    @staticmethod
    def _frame() -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "item_id": "1",
                    "sku": "A",
                    "title": "2025 Topps Chrome #43 Patrick Bailey",
                    "team": "San Francisco Giants",
                    "sport": "Baseball",
                    "subset_name": "Rookie Autographs",
                },
                {
                    "item_id": "2",
                    "sku": "B",
                    "title": "2025 Topps Update Complete Your Set, You Pick!",
                    "team": "Cincinnati Reds",
                    "sport": "Baseball",
                },
                {
                    "item_id": "3",
                    "sku": "C",
                    "title": "2025 Prizm Cooper Flagg",
                    "team": "Dallas Mavericks",
                    "sport": "Basketball",
                },
            ]
        )

    def test_one_report_row_per_listing(self):
        plan = build_category_plan(self._frame())
        assert len(plan) == 3
        assert list(plan["item_id"]) == ["1", "2", "3"]

    def test_store_category_names_carry_both_slots(self):
        plan = build_category_plan(self._frame())
        assert plan.loc[0, "store_category_names"] == (
            "/Baseball Singles/San Francisco Giants | /Hits/Autographs"
        )
        assert plan.loc[0, "category_count"] == 2
        assert plan.loc[1, "store_category_names"] == "/Complete Your Set - You Pick"

    def test_counts_credit_both_slots(self):
        counts = category_counts(build_category_plan(self._frame()))
        by_category = dict(zip(counts["category"], counts["listings"], strict=True))
        assert by_category["/Baseball Singles/San Francisco Giants"] == 1
        assert by_category["/Hits/Autographs"] == 1
        assert by_category["/Basketball Singles"] == 1
