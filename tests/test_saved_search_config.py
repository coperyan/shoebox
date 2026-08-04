from datetime import timedelta
from decimal import Decimal

import pytest
import yaml
from pydantic import ValidationError

from shoebox.models.saved_search import (
    SearchesFile,
    load_searches_file,
    parse_interval,
)

EXAMPLE_PATH = "configs/searches.example.yml"


def _file(**overrides) -> SearchesFile:
    """A minimal valid document, with the first search patched."""
    search = {"name": "s1", "query": "jordan"}
    search.update(overrides)
    return SearchesFile(version=1, searches=[search])


class TestParseInterval:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("90s", timedelta(seconds=90)),
            ("15m", timedelta(minutes=15)),
            ("2h", timedelta(hours=2)),
            ("1d", timedelta(days=1)),
            ("  30M  ", timedelta(minutes=30)),
        ],
    )
    def test_valid(self, text, expected):
        assert parse_interval(text) == expected

    def test_bare_int_rejected_with_reason(self):
        # Pydantic would coerce 15 -> 15 seconds, which reads as minutes to a human.
        with pytest.raises(ValueError, match="ambiguous"):
            parse_interval(15)

    @pytest.mark.parametrize("text", ["15 minutes", "-5m", "1w", "m", "", "1.5h"])
    def test_malformed_rejected(self, text):
        with pytest.raises(ValueError, match="invalid interval"):
            parse_interval(text)

    def test_below_minimum_rejected(self):
        with pytest.raises(ValueError, match="below the 60s minimum"):
            parse_interval("30s")


class TestDefaultsInheritance:
    def test_search_inherits_defaults(self):
        f = SearchesFile(
            version=1,
            defaults={"interval": "1h", "max_notify": 3},
            searches=[{"name": "s1", "query": "jordan"}],
        )
        r = f.resolved()[0]
        assert r.interval == timedelta(hours=1)
        assert r.max_notify == 3

    def test_search_overrides_defaults(self):
        f = SearchesFile(
            version=1,
            defaults={"interval": "1h"},
            searches=[{"name": "s1", "query": "jordan", "interval": "15m"}],
        )
        assert f.resolved()[0].interval == timedelta(minutes=15)

    def test_empty_list_overrides_nonempty_default(self):
        """[] means 'explicitly no filter' and must beat a non-empty default.

        This is the case a naive dict.update merge gets wrong.
        """
        f = SearchesFile(
            version=1,
            defaults={"title_exclude": ["reprint", "lot"]},
            searches=[{"name": "s1", "query": "jordan", "title_exclude": []}],
        )
        assert f.resolved()[0].title_exclude == []

    def test_omitted_list_inherits_nonempty_default(self):
        f = SearchesFile(
            version=1,
            defaults={"title_exclude": ["reprint"]},
            searches=[{"name": "s1", "query": "jordan"}],
        )
        assert f.resolved()[0].title_exclude == ["reprint"]

    def test_defaults_are_usable_unset(self):
        r = _file().resolved()[0]
        assert r.sort == "newlyListed"
        assert r.buying_options == ["FIXED_PRICE", "AUCTION"]
        assert r.interval == timedelta(minutes=30)

    def test_enabled_false_is_not_treated_as_inherit(self):
        """False is falsy but not None -- it must override a True default."""
        f = SearchesFile(
            version=1,
            defaults={"enabled": True},
            searches=[{"name": "s1", "query": "j", "enabled": False}],
        )
        assert f.resolved()[0].enabled is False
        assert f.enabled_searches() == []


class TestChannelAliases:
    def _file(self, channels, **search):
        base = {"name": "s1", "query": "jordan"}
        base.update(search)
        return SearchesFile(version=1, channels=channels, searches=[base])

    def test_alias_resolves_to_id(self):
        f = self._file({"alerts": "C0123ABCD"}, channel="alerts")
        assert f.resolved()[0].channel == "C0123ABCD"

    def test_literal_id_passes_through(self):
        """Configs predating aliases must keep working."""
        f = self._file({}, channel="C0123ABCD")
        assert f.resolved()[0].channel == "C0123ABCD"

    def test_literal_id_still_works_alongside_aliases(self):
        f = self._file({"alerts": "C0123ABCD"}, channel="C9999ZZZZ")
        assert f.resolved()[0].channel == "C9999ZZZZ"

    def test_alias_in_defaults_is_resolved(self):
        f = SearchesFile(
            version=1,
            channels={"alerts": "C0123ABCD"},
            defaults={"channel": "alerts"},
            searches=[{"name": "s1", "query": "jordan"}],
        )
        assert f.resolved()[0].channel == "C0123ABCD"

    def test_per_search_alias_beats_defaults_alias(self):
        f = SearchesFile(
            version=1,
            channels={"a": "C0000AAAA", "b": "C1111BBBB"},
            defaults={"channel": "a"},
            searches=[
                {"name": "s1", "query": "j"},
                {"name": "s2", "query": "k", "channel": "b"},
            ],
        )
        assert [s.channel for s in f.resolved()] == ["C0000AAAA", "C1111BBBB"]

    def test_no_channel_stays_none(self):
        # None means "fall back to app.yaml" -- not an error here.
        assert self._file({"alerts": "C0123ABCD"}).resolved()[0].channel is None

    def test_unknown_alias_is_rejected_and_lists_known_ones(self):
        with pytest.raises(ValidationError, match="Defined aliases: alerts"):
            self._file({"alerts": "C0123ABCD"}, channel="alertz")

    def test_unknown_alias_with_no_map_is_rejected(self):
        with pytest.raises(ValidationError, match="unknown channel alias"):
            self._file({}, channel="not_an_id")

    def test_channel_name_instead_of_id_is_rejected(self):
        with pytest.raises(ValidationError, match="not a Slack channel ID"):
            self._file({"alerts": "#card-alerts"})

    def test_swapped_alias_and_id_is_caught(self):
        with pytest.raises(ValidationError, match="looks like a Slack channel ID"):
            self._file({"C0123ABCD": "alerts"})

    def test_private_group_and_dm_ids_accepted(self):
        assert self._file({"g": "G0123ABCD"}, channel="g").resolved()[0].channel == "G0123ABCD"
        assert self._file({"d": "D0123ABCD"}, channel="d").resolved()[0].channel == "D0123ABCD"


class TestValidation:
    def test_unknown_key_is_rejected_at_its_location(self):
        with pytest.raises(ValidationError) as exc:
            SearchesFile(version=1, searches=[{"name": "s1", "query": "j", "intervl": "5m"}])
        assert "searches.0.intervl" in str(exc.value)

    def test_duplicate_names_rejected(self):
        with pytest.raises(ValidationError, match="duplicate search name"):
            SearchesFile(
                version=1,
                searches=[{"name": "dup", "query": "a"}, {"name": "dup", "query": "b"}],
            )

    @pytest.mark.parametrize("name", ["Has Caps", "_leading", "with space", "a" * 65, ""])
    def test_bad_names_rejected(self, name):
        with pytest.raises(ValidationError, match="invalid search name"):
            _file(name=name)

    def test_query_over_100_chars_rejected(self):
        with pytest.raises(ValidationError, match="truncates q at 100"):
            _file(query="x" * 101)

    def test_wildcard_query_rejected(self):
        with pytest.raises(ValidationError, match=r"'\*' wildcard"):
            _file(query="jordan*")

    def test_multiple_category_ids_rejected(self):
        with pytest.raises(ValidationError, match="only one category ID"):
            _file(category_ids=["261328", "212"])

    def test_neither_query_nor_category_rejected(self):
        with pytest.raises(ValidationError, match="at least one of 'query' or 'category_ids'"):
            SearchesFile(version=1, searches=[{"name": "s1"}])

    def test_category_only_is_allowed(self):
        r = SearchesFile(
            version=1, searches=[{"name": "s1", "category_ids": ["261328"]}]
        ).resolved()[0]
        assert r.query is None

    def test_empty_buying_options_rejected(self):
        # An empty list would make eBay silently return FIXED_PRICE only.
        with pytest.raises(ValidationError, match="silently hide every auction"):
            _file(buying_options=[])

    def test_aspects_require_exactly_one_category(self):
        with pytest.raises(ValidationError, match="requires exactly one category_id"):
            _file(aspects={"Grade": ["10"]})

    def test_aspect_with_comma_rejected(self):
        with pytest.raises(ValidationError, match="no.*escape for ','"):
            _file(category_ids=["261328"], aspects={"Gr,ade": ["10"]})

    def test_sellers_and_exclude_sellers_are_exclusive(self):
        with pytest.raises(ValidationError, match="mutually exclusive"):
            _file(sellers=["a"], exclude_sellers=["b"])

    def test_seed_smaller_than_max_results_rejected(self):
        with pytest.raises(ValidationError, match="seed_max_results"):
            _file(max_results=200, seed_max_results=50)

    def test_price_min_above_max_rejected(self):
        with pytest.raises(ValidationError, match="must not exceed"):
            _file(price={"min": 100, "max": 10})

    def test_price_needs_a_bound(self):
        with pytest.raises(ValidationError, match="at least one of 'min' or 'max'"):
            _file(price={})

    def test_negative_price_rejected(self):
        with pytest.raises(ValidationError, match="must not be negative"):
            _file(price={"min": -1})

    def test_bad_sort_rejected(self):
        with pytest.raises(ValidationError):
            _file(sort="bestMatch")


class TestLoader:
    def test_missing_file_points_at_the_example(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="searches.example.yml"):
            load_searches_file(tmp_path / "nope.yaml")

    def test_empty_file_is_a_valid_empty_document(self, tmp_path):
        p = tmp_path / "searches.yaml"
        p.write_text("")
        assert load_searches_file(p).searches == []

    def test_non_mapping_rejected(self, tmp_path):
        p = tmp_path / "searches.yaml"
        p.write_text("- just\n- a list\n")
        with pytest.raises(ValueError, match="YAML mapping"):
            load_searches_file(p)

    def test_roundtrip(self, tmp_path):
        p = tmp_path / "searches.yaml"
        p.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "defaults": {"interval": "45m"},
                    "searches": [{"name": "s1", "query": "jordan"}],
                }
            )
        )
        assert load_searches_file(p).resolved()[0].interval == timedelta(minutes=45)


class TestShippedExample:
    """Guards against the committed template drifting out of sync with the schema."""

    def test_example_file_validates(self):
        f = load_searches_file(EXAMPLE_PATH)
        assert f.searches, "the example should ship at least one search"

    def test_example_resolves(self):
        resolved = {s.name: s for s in load_searches_file(EXAMPLE_PATH).resolved()}
        assert resolved["jordan_psa10_bin"].interval == timedelta(minutes=15)
        assert resolved["jordan_psa10_bin"].price.max == Decimal("200")
        # The disabled example stays out of the run set.
        assert "graded_chrome_autos" not in {
            s.name for s in load_searches_file(EXAMPLE_PATH).enabled_searches()
        }
