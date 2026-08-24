"""Store category routing for the eBay Team item specific.

Covers the two questions the raw item specific answers badly: which sport
branch a card belongs under, and which team category folds its franchise's
alternate names together.
"""

import yaml

from shoebox.utils.title_crosswalk import (
    crosswalk_path,
    shorten_team_name,
    team_parent,
    team_parents,
    team_sport,
    unmapped_team,
)


class TestTeamParent:
    def test_current_team_keeps_its_full_name(self):
        assert team_parent("Arizona Diamondbacks") == "Arizona Diamondbacks"
        assert team_parent("New York Yankees") == "New York Yankees"

    def test_categories_use_the_full_city_name_not_the_title_short_form(self):
        assert shorten_team_name("Toronto Blue Jays") == "Jays"
        assert team_parent("Toronto Blue Jays") == "Toronto Blue Jays"

    def test_renamed_franchise_folds_into_current_name(self):
        assert team_parent("Cleveland Indians") == "Cleveland Guardians"
        assert team_parent("Cleveland Guardians") == "Cleveland Guardians"
        # eBay sometimes returns the bare city.
        assert team_parent("Cleveland") == "Cleveland Guardians"

    def test_relocated_franchise_folds_into_current_club(self):
        assert team_parent("Montreal Expos") == "Washington Nationals"
        assert team_parent("St. Louis Browns") == "Baltimore Orioles"
        assert team_parent("Seattle Pilots") == "Milwaukee Brewers"

    def test_city_variants_collapse_onto_one_club(self):
        # eBay returns the A's with and without a city; all three eras are one.
        for name in ("Athletics", "Oakland Athletics", "Philadelphia Athletics"):
            assert team_parent(name) == "Athletics"
        for name in ("Los Angeles Angels", "Angels", "California Angels"):
            assert team_parent(name) == "Los Angeles Angels"

    def test_minor_league_affiliate_routes_to_parent_club(self):
        assert team_parent("Jupiter Hammerheads") == "Miami Marlins"
        assert team_parent("Jacksonville Jumbo Shrimp") == "Miami Marlins"
        assert team_parent("Bowling Green Hot Rods") == "Tampa Bay Rays"
        assert team_parent("Visalia Rawhide") == "Arizona Diamondbacks"

    def test_affiliates_route_to_the_parent_club(self):
        assert team_parent("ACL Dodgers") == "Los Angeles Dodgers"
        assert team_parent("Fredericksburg Nationals") == "Washington Nationals"

    def test_negro_leagues_go_to_the_fallback_category(self):
        for name in (
            "Homestead Grays",
            "Kansas City Monarchs",
            "Newark Eagles",
            "Pittsburgh Crawfords",
        ):
            assert team_parent(name) == unmapped_team()

    def test_league_values_go_to_the_fallback_category(self):
        assert team_parent("National League") == unmapped_team()
        assert team_parent("American League") == unmapped_team()

    def test_ambiguous_franchise_keeps_its_own_category(self):
        # The Senators became both the Twins and the Rangers; no safe guess.
        assert team_parent("Washington Senators") == "Washington Senators"

    def test_relocated_nba_and_nfl_teams_fold_forward(self):
        assert team_parent("Washington Bullets") == "Washington Wizards"
        assert team_parent("Seattle SuperSonics") == "Oklahoma City Thunder"
        assert team_parent("Charlotte Bobcats") == "Charlotte Hornets"
        assert team_parent("Houston Oilers") == "Tennessee Titans"
        assert team_parent("Washington Football Team") == "Washington Commanders"

    def test_same_nickname_in_two_leagues_stays_apart(self):
        # Short names collide here; full names do not.
        assert team_parent("Phoenix Cardinals") == "Arizona Cardinals"
        assert team_parent("St. Louis Cardinals") == "St. Louis Cardinals"

    def test_lookup_tolerates_casing_and_spacing(self):
        assert team_parent("  cleveland   INDIANS ") == "Cleveland Guardians"

    def test_unknown_team_falls_back_rather_than_returning_none(self):
        assert team_parent("Toledo Mud Hens") == unmapped_team()

    def test_missing_team_falls_back_too(self):
        # Every listing needs somewhere to live, even with no Team specific.
        assert team_parent(None) == unmapped_team()
        assert team_parent("") == unmapped_team()

    def test_fallback_category_is_configured(self):
        assert unmapped_team() == "Other Teams"


class TestTeamSport:
    def test_sport_comes_from_the_team_not_the_listing(self):
        # These arrive tagged Sport=Baseball on a handful of live listings.
        assert team_sport("Denver Nuggets") == "Basketball"
        assert team_sport("New York Knicks") == "Basketball"

    def test_each_league_maps_to_its_branch(self):
        assert team_sport("Cincinnati Reds") == "Baseball"
        assert team_sport("Brooklyn Dodgers") == "Baseball"
        assert team_sport("Visalia Rawhide") == "Baseball"
        assert team_sport("Dallas Cowboys") == "Football"
        assert team_sport("Boston Celtics") == "Basketball"

    def test_college_implies_no_single_sport(self):
        assert team_sport("Marquette Golden Eagles") is None
        assert team_sport("Ole Miss Rebels") is None

    def test_unknown_team_implies_no_sport(self):
        assert team_sport("Toledo Mud Hens") is None
        assert team_sport(None) is None


class TestMultiTeamValues:
    def test_two_team_card_collapses_to_one_category(self):
        assert team_parents("Brooklyn Dodgers | Los Angeles Dodgers") == ["Los Angeles Dodgers"]

    def test_genuinely_distinct_teams_are_kept_in_order(self):
        assert team_parents("Detroit Tigers | Houston Astros") == [
            "Detroit Tigers",
            "Houston Astros",
        ]

    def test_accepts_a_list_from_the_ebay_api(self):
        assert team_parents(["Cleveland Indians", "Cleveland"]) == ["Cleveland Guardians"]

    def test_a_value_resolving_to_nothing_falls_back(self):
        assert team_parents("Toledo Mud Hens") == [unmapped_team()]
        assert team_parents(None) == [unmapped_team()]

    def test_fallback_does_not_ride_along_with_a_known_team(self):
        # One known team and one unknown files under the known team alone.
        assert team_parents("Detroit Tigers | Toledo Mud Hens") == ["Detroit Tigers"]


class TestConfigIntegrity:
    """Guards against the YAML drifting out of step with itself."""

    @staticmethod
    def _raw() -> dict:
        return yaml.safe_load(crosswalk_path().read_text(encoding="utf-8"))

    def test_every_parent_key_is_a_known_team(self):
        raw = self._raw()
        known = {name.strip().casefold() for group in raw["teams"].values() for name in group}
        unknown = [
            name
            for name in raw["store_categories"]["team_parents"]
            if name.strip().casefold() not in known
        ]
        assert not unknown, f"team_parents keys missing from teams: {unknown}"

    def test_every_team_group_declares_a_sport(self):
        raw = self._raw()
        declared = set(raw["store_categories"]["group_sports"])
        missing = set(raw["teams"]) - declared
        assert not missing, f"team groups with no group_sports entry: {missing}"

    def test_every_parent_value_is_itself_a_known_team(self):
        """A parent must name a club the crosswalk knows, or the fallback.

        Naming one "Diamondbacks" while the club itself is "Arizona
        Diamondbacks" silently splits it across two categories.
        """
        raw = self._raw()
        store = raw["store_categories"]
        allowed = {name for group in raw["teams"].values() for name in group}
        allowed.add(store["unmapped_team"])
        stray = {
            name: parent for name, parent in store["team_parents"].items() if parent not in allowed
        }
        assert not stray, f"parents naming no known team: {stray}"

    def test_no_parent_entry_is_redundant(self):
        """A parent equal to what the fallback would give is dead config.

        Compared exactly, not case-insensitively: where two spellings of a team
        normalize to the same key ("Portland Trail Blazers" and "Portland Trail
        blazers"), an entry pinning the right casing is doing real work.
        """
        raw = self._raw()
        # Mirrors the loader: later groups win, so this is the name a team with
        # no parent entry falls back to.
        display = {
            name.strip().casefold(): name for group in raw["teams"].values() for name in group
        }
        redundant = [
            name
            for name, parent in raw["store_categories"]["team_parents"].items()
            if display.get(name.strip().casefold()) == parent
        ]
        assert not redundant, f"team_parents entries that change nothing: {redundant}"
