"""TCDB advanced search: query building, results parsing, login detection, and
the interactive session's command parser. No browser is started here."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from shoebox.clients.tcdb.search import is_challenge_page, is_logged_in, parse_results
from shoebox.models.tcdb import AdvancedSearchQuery, TcdbSearchPage, TcdbSearchResult
from shoebox.pipelines.tcdb_search import (
    build_defaults,
    parse_command,
    results_table,
)

FIXTURE = Path(__file__).parent / "fixtures" / "tcdb_view_results.html"


@pytest.fixture(scope="module")
def results_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# --- AdvancedSearchQuery ---------------------------------------------------------


def test_query_url_carries_every_form_field():
    q = AdvancedSearchQuery(name="Bonds", card_number="25")
    url = q.to_url()
    parsed = urlparse(url)
    assert parsed.netloc == "www.tcdb.com"
    assert parsed.path == "/ViewResults.cfm"
    params = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
    assert params == {
        "MODE": "ADVANCED",
        "Type": "Baseball",
        "Year": "",
        "SetName": "",
        "SetType": "",
        "CardNum": "25",
        "Name": "Bonds",
        "Team": "",
        "Note": "",
    }


def test_category_is_normalized_case_insensitively():
    assert AdvancedSearchQuery(category="basketball").category == "Basketball"
    assert AdvancedSearchQuery(category="non sport").category == "Non-Sport"
    with pytest.raises(ValueError, match="Unknown TCDB category"):
        AdvancedSearchQuery(category="Curling")


def test_set_type_accepts_code_or_label():
    assert AdvancedSearchQuery(set_type="M").set_type == "M"
    assert AdvancedSearchQuery(set_type="minor league").set_type == "M"
    assert AdvancedSearchQuery(set_type="Any").set_type == ""
    with pytest.raises(ValueError, match="Unknown TCDB set type"):
        AdvancedSearchQuery(set_type="Bogus")


def test_with_updates_validates_field_names_and_clears():
    q = AdvancedSearchQuery(name="Bonds", year="1993")
    q2 = q.with_updates(year="", team="Giants")
    assert q2.year == "" and q2.team == "Giants" and q2.name == "Bonds"
    assert q.year == "1993", "with_updates must not mutate the original"
    with pytest.raises(ValueError, match="Unknown search field"):
        q.with_updates(player="Bonds")


def test_describe_lists_only_set_fields():
    assert AdvancedSearchQuery(name="Bonds").describe() == "category='Baseball', name='Bonds'"


# --- parse_results ---------------------------------------------------------------


def test_parse_results_reads_rows_and_count(results_html):
    page = parse_results(results_html, query_url="https://x/ViewResults.cfm?MODE=ADVANCED")
    assert page.total_results == 97
    assert len(page.results) == 5
    assert page.truncated  # fixture keeps 5 of 97 rows

    first = page.results[0]
    assert first.index == 1
    assert first.title == "1994 Upper Deck Fun Pack #25 Barry Bonds"
    assert first.url.startswith("https://www.tcdb.com/ViewCard.cfm/sid/10182/cid/440525/")
    assert (first.set_id, first.card_id) == (10182, 440525)
    assert first.thumb_url == "https://www.tcdb.com/Images/Thumbs/Baseball/10182/10182_25Thumb.jpg"
    assert first.note is None


def test_parse_results_captures_note_and_drops_placeholder_thumb(results_html):
    page = parse_results(results_html)
    last = page.results[-1]
    assert last.index == 97
    assert last.title.startswith("2025 Topps Holiday")
    assert last.note == "2005 Bowman’s Best"
    assert last.thumb_url is None, "AddImage.gif placeholder is not a real thumbnail"


def test_parse_results_handles_empty_page():
    html = "<html><body><p><strong>0 results</strong></p></body></html>"
    page = parse_results(html)
    assert page.total_results == 0
    assert page.results == []
    assert not page.truncated


# --- login / challenge detection -------------------------------------------------


def test_logged_out_header_is_detected(results_html):
    assert is_logged_in(results_html) is False


def test_logged_in_header_is_detected():
    html = """<ul class="navbar-nav ml-auto">
      <li class="nav-item"><a class="nav-link" href="/YourCollection.cfm">Collection</a></li>
      <li class="nav-item"><a class="nav-link" href="/Login.cfm?ACTION=LOGOUT">Logout</a></li>
    </ul>"""
    assert is_logged_in(html) is True


def test_page_without_nav_is_not_logged_in():
    assert is_logged_in("<html><body>Just a moment...</body></html>") is False


def test_challenge_page_detection():
    assert is_challenge_page("Just a moment...")
    assert is_challenge_page(None, "<h1>Performing security verification</h1>")
    assert not is_challenge_page("View Results | Trading Card Database")


# --- interactive session --------------------------------------------------------


@pytest.mark.parametrize(
    "line,kind",
    [("", "noop"), ("   ", "noop"), ("q", "quit"), ("QUIT", "quit"), ("exit", "quit")],
)
def test_parse_command_blank_and_quit(line, kind):
    assert parse_command(line).kind == kind


def test_parse_command_search_is_the_default():
    cmd = parse_command("25a")
    assert cmd.kind == "search" and cmd.value == "25a"


def test_parse_command_set_handles_quotes_and_clearing():
    cmd = parse_command('set year=1993 set_name="Upper Deck" team=')
    assert cmd.kind == "set"
    assert cmd.updates == {"year": "1993", "set_name": "Upper Deck", "team": ""}


def test_parse_command_set_normalizes_key_style():
    assert parse_command("set Set-Type=M").updates == {"set_type": "M"}


@pytest.mark.parametrize("line", ["set", "set year", 'set year="1993'])
def test_parse_command_set_rejects_malformed(line):
    with pytest.raises(ValueError):
        parse_command(line)


def test_parse_command_open():
    assert parse_command("open 3").index == 3
    with pytest.raises(ValueError, match="Usage: open N"):
        parse_command("open three")


def test_build_defaults_merges_config_then_cli():
    q = build_defaults(
        {"year": "1993", "team": ""},
        settings_defaults={"name": "Bonds", "year": "1986"},
    )
    assert q.name == "Bonds"
    assert q.year == "1993", "CLI flag overrides the config default"
    assert q.team == "", "blank CLI values do not clobber config"


def test_results_table_titles_and_caps_rows():
    page = TcdbSearchPage(
        query_url="u",
        total_results=3,
        results=[
            TcdbSearchResult(index=i, title=f"Card {i}", url=f"https://x/{i}") for i in (1, 2, 3)
        ],
    )
    table = results_table(page, AdvancedSearchQuery(name="Bonds"), max_rows=2)
    assert table.row_count == 2
    assert "3 result(s)" in str(table.title)
    assert "showing 2" in str(table.title)
