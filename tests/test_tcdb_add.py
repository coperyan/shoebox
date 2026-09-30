"""TCDB add-to-collection: card-title parsing, result matching, ViewCard page and
collection-box parsing, the browser's add flow against a fake driver, and the
tcdb-add pipeline against a fake browser. No real browser is started here."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

import shoebox.pipelines.tcdb_add as tcdb_add
from shoebox.clients.tcdb import TcdbBrowser, is_logged_in
from shoebox.clients.tcdb.card import (
    is_card_url,
    match_card,
    normalize_title,
    parse_card_page,
    parse_card_spec,
    parse_collection_widget,
)
from shoebox.models.tcdb import (
    CollectionAddResult,
    TcdbCardPage,
    TcdbSearchPage,
    TcdbSearchResult,
)
from shoebox.pipelines.tcdb_add import read_card_lines, search_query_for
from shoebox.pipelines.tcdb_search import parse_command

FIXTURE = Path(__file__).parent / "fixtures" / "tcdb_view_card.html"
CAIN = "2009 Bowman Chrome - X-Fractors #171 Matt Cain"
CAIN_URL = "https://www.tcdb.com/ViewCard.cfm/sid/24459/cid/2840262/2009-Bowman-Chrome-X-Fractors-171-Matt-Cain"

QUICK_ADD_BOX = (
    '<select name="CollectionID"><option value="1">Main</option></select>'
    '<button id="quickAddBtn" data-collectionid="1" data-cardid="2840262">Quick Add</button>'
)
OWNED_BOX = (
    '<p>You have 1</p><button class="addAnotherBtn" data-collectionid="1">+1</button>'
    '<button class="removeBtn" data-collectionid="1">Remove</button>'
)


@pytest.fixture(scope="module")
def card_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _page(*titles: str) -> TcdbSearchPage:
    return TcdbSearchPage(
        query_url="u",
        total_results=len(titles),
        results=[
            TcdbSearchResult(
                index=i, title=t, url=f"https://www.tcdb.com/ViewCard.cfm/sid/1/cid/{i}"
            )
            for i, t in enumerate(titles, 1)
        ],
    )


# --- parse_card_spec ------------------------------------------------------------


def test_parse_card_spec_splits_the_tcdb_title():
    spec = parse_card_spec(CAIN)
    assert (spec.year, spec.set_name, spec.card_number, spec.name) == (
        "2009",
        "Bowman Chrome - X-Fractors",
        "171",
        "Matt Cain",
    )
    assert spec.title == CAIN


@pytest.mark.parametrize(
    "text,year,set_name,number,name",
    [
        ("2009-10 Upper Deck #5 LeBron James", "2009-10", "Upper Deck", "5", "LeBron James"),
        ("2021 Bowman - Chrome Prospects #BCP-12 Bobby Witt Jr.", "2021", "Bowman - Chrome Prospects", "BCP-12", "Bobby Witt Jr."),
        ("  1993   Upper Deck  #25   Barry Bonds ", "1993", "Upper Deck", "25", "Barry Bonds"),
        ("1994 Upper Deck Fun Pack #25", "1994", "Upper Deck Fun Pack", "25", ""),
    ],
)  # fmt: skip
def test_parse_card_spec_variants(text, year, set_name, number, name):
    spec = parse_card_spec(text)
    assert (spec.year, spec.set_name, spec.card_number, spec.name) == (year, set_name, number, name)


@pytest.mark.parametrize("text", ["", "Matt Cain", "2009 Bowman Chrome 171 Matt Cain", "#171 Cain"])
def test_parse_card_spec_rejects_non_titles(text):
    with pytest.raises(ValueError, match="Write it as TCDB titles it"):
        parse_card_spec(text)


def test_search_uses_year_number_and_first_name_but_not_set():
    spec = parse_card_spec("2008 Topps - Combos #C1 Matt Cain / Tim Lincecum")
    q = search_query_for(spec, "baseball")
    assert (q.category, q.year, q.card_number, q.name) == ("Baseball", "2008", "C1", "Matt Cain")
    assert q.set_name == "", "TCDB reads the set's ' - ' as an exclusion, so it stays out"


# --- match_card -------------------------------------------------------------------


def test_match_card_picks_the_exact_parallel():
    page = _page(
        "2009 Bowman Chrome #171 Matt Cain",
        "2009 Bowman Chrome - Refractors #171 Matt Cain",
        "2009 Bowman Chrome - X-Fractors #171 Matt Cain",
        "2009 Bowman Chrome - Blue Refractors #171 Matt Cain",
    )
    matches = match_card(page, parse_card_spec(CAIN))
    assert [m.index for m in matches] == [3]


def test_match_card_ignores_case_accents_and_apostrophes():
    page = _page("2005 Bowman’s Best #10 José Reyes")
    assert match_card(page, parse_card_spec("2005 bowman's best #10 jose reyes"))


def test_match_card_without_name_matches_any_player_on_that_number():
    page = _page(
        "1994 Upper Deck Fun Pack #25 Barry Bonds", "1994 Upper Deck Fun Pack #250 Someone"
    )
    assert [m.index for m in match_card(page, parse_card_spec("1994 Upper Deck Fun Pack #25"))] == [
        1
    ]


def test_match_card_reports_every_same_title_row():
    page = _page(CAIN, CAIN)
    assert len(match_card(page, parse_card_spec(CAIN))) == 2


def test_normalize_title_collapses_whitespace():
    assert normalize_title("  2009  Bowman\tChrome ") == "2009 bowman chrome"


# --- ViewCard page and collection box ----------------------------------------------


def test_parse_card_page_reads_title_and_ids(card_html):
    card = parse_card_page(card_html)
    assert card.title == CAIN
    assert card.url == CAIN_URL
    assert (card.set_id, card.card_id) == (24459, 2840262)


def test_card_fixture_is_logged_in(card_html):
    assert is_logged_in(card_html) is True


def test_card_fixture_starts_with_an_empty_collection_box(card_html):
    # The box is filled by JavaScript, so the saved HTML has an empty #colDiv:
    # the reason add_to_collection waits for it in the live page.
    assert '<div id="colDiv"></div>' in card_html
    assert "#quickAddBtn" in card_html and "CollectionAddO_ajax.cfm" in card_html


def test_parse_collection_widget_states():
    assert parse_collection_widget("   ").loaded is False
    fresh = parse_collection_widget(QUICK_ADD_BOX)
    assert fresh.loaded and fresh.quick_add and not fresh.owned
    owned = parse_collection_widget(OWNED_BOX)
    assert owned.owned and owned.add_another and not owned.quick_add


def test_is_card_url():
    assert is_card_url(CAIN_URL)
    assert is_card_url("https://tcdb.com/ViewCard.cfm/sid/1/cid/2")
    assert not is_card_url(CAIN)
    assert not is_card_url("https://www.tcdb.com/ViewSet.cfm/sid/24459")


# --- TcdbBrowser.add_to_collection against a fake driver ------------------------------


class FakeDriver:
    """Just enough of a Selenium driver for add_to_collection: serves #colDiv's
    HTML, records clicks, and reports the add request the page would make."""

    def __init__(self, box: str, *, after_click: str = OWNED_BOX, status: int = 200):
        self.box = box
        self.after_click = after_click
        self.status = status
        self.clicked: list[str] = []
        self.requests: list[dict] = []

    def execute_script(self, script: str, *args):
        if "el.click()" in script:
            self.clicked.append(args[0])
            endpoint = {
                "#quickAddBtn": "CollectionAddO_ajax.cfm",
                ".addAnotherBtn": "CollectionAddIncrement_ajax.cfm",
            }[args[0]]
            self.requests.append(
                {"url": f"https://www.tcdb.com/{endpoint}?CardID=2840262&CollectionID=1",
                 "ok": 200 <= self.status < 300, "status": self.status}
            )  # fmt: skip
            self.box = self.after_click
            return True
        if "__shoebox.requests" in script and script.lstrip().startswith("return"):
            return list(self.requests)
        if "getElementById('colDiv')" in script:
            return self.box
        return None  # the fetch-watcher install


def _browser(monkeypatch, card_html, driver: FakeDriver) -> TcdbBrowser:
    browser = TcdbBrowser("unused", settle_delay_s=0)
    browser.driver = driver
    monkeypatch.setattr(browser, "get", lambda url: card_html)
    return browser


def test_add_clicks_quick_add_and_verifies(monkeypatch, card_html):
    driver = FakeDriver(QUICK_ADD_BOX)
    res = _browser(monkeypatch, card_html, driver).add_to_collection(CAIN_URL, poll_s=0)
    assert res.status == "added" and res.ok and res.verified
    assert driver.clicked == ["#quickAddBtn"]
    assert "CollectionAddO_ajax.cfm" in res.request_url
    assert res.card.card_id == 2840262


def test_add_skips_a_card_already_owned(monkeypatch, card_html):
    driver = FakeDriver(OWNED_BOX)
    res = _browser(monkeypatch, card_html, driver).add_to_collection(CAIN_URL, poll_s=0)
    assert res.status == "already_owned"
    assert driver.clicked == []


def test_add_duplicate_uses_add_another(monkeypatch, card_html):
    driver = FakeDriver(OWNED_BOX, after_click=OWNED_BOX.replace("1", "2"))
    browser = _browser(monkeypatch, card_html, driver)
    res = browser.add_to_collection(CAIN_URL, allow_duplicate=True, poll_s=0)
    assert res.status == "added"
    assert driver.clicked == [".addAnotherBtn"]


def test_add_reports_http_errors(monkeypatch, card_html):
    driver = FakeDriver(QUICK_ADD_BOX, status=500)
    res = _browser(monkeypatch, card_html, driver).add_to_collection(CAIN_URL, poll_s=0)
    assert res.status == "failed" and "HTTP 500" in res.detail
    assert res.widget_html == QUICK_ADD_BOX


def test_add_without_a_button_does_not_click(monkeypatch, card_html):
    driver = FakeDriver("<p>Log in to track your collection</p>")
    res = _browser(monkeypatch, card_html, driver).add_to_collection(CAIN_URL, poll_s=0)
    assert res.status == "no_add_button"
    assert driver.clicked == []


def test_add_times_out_when_the_box_never_loads(monkeypatch, card_html):
    driver = FakeDriver("")
    browser = _browser(monkeypatch, card_html, driver)
    res = browser.add_to_collection(CAIN_URL, timeout_s=0, poll_s=0)
    assert res.status == "failed" and "did not load" in res.detail


# --- pipeline -----------------------------------------------------------------------


def test_read_card_lines_merges_args_and_file(tmp_path):
    f = tmp_path / "cards.txt"
    f.write_text(f"# my pulls\n\n{CAIN}\n  1993 Upper Deck #25 Barry Bonds  \n", encoding="utf-8")
    assert read_card_lines(["  2009 Topps #1 A B ", ""], f) == [
        "2009 Topps #1 A B",
        CAIN,
        "1993 Upper Deck #25 Barry Bonds",
    ]


def test_parse_command_add():
    cmd = parse_command("add 2")
    assert cmd.kind == "add" and cmd.index == 2
    with pytest.raises(ValueError, match="Usage: add N"):
        parse_command("add two")


class FakeBrowser:
    """Stands in for TcdbBrowser inside run_tcdb_add."""

    def __init__(self, pages: dict[str, TcdbSearchPage]):
        self.pages = pages
        self.searches: list = []
        self.added: list[str] = []

    def __call__(self, *args, **kwargs):  # used as the TcdbBrowser class
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def ensure_logged_in(self, **kwargs):
        return True

    def advanced_search(self, query):
        self.searches.append(query)
        return self.pages[query.name]

    def add_to_collection(self, url, *, allow_duplicate=False):
        self.added.append(url)
        return CollectionAddResult(status="added", card=TcdbCardPage(url=url, title="t"))


@pytest.fixture
def fake_settings(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        tcdb=SimpleNamespace(
            profile_dir=str(tmp_path / "p"), login_timeout_s=1, search_defaults={}
        ),
        paths=SimpleNamespace(data_dir=str(tmp_path)),
    )
    monkeypatch.setattr(tcdb_add, "get_settings", lambda: settings)
    return settings


def test_run_tcdb_add_adds_exact_matches_and_skips_the_rest(monkeypatch, fake_settings):
    pages = {
        "Matt Cain": _page("2009 Bowman Chrome #171 Matt Cain", CAIN),
        "Barry Bonds": _page("1993 Topps #25 Barry Bonds"),
    }
    fake = FakeBrowser(pages)
    monkeypatch.setattr(tcdb_add, "TcdbBrowser", fake)
    code = tcdb_add.run_tcdb_add(
        cards=[CAIN, "1993 Upper Deck #25 Barry Bonds", CAIN_URL],
        console=_quiet_console(),
    )
    assert code == 1, "one card was not found"
    assert fake.added == ["https://www.tcdb.com/ViewCard.cfm/sid/1/cid/2", CAIN_URL]
    assert [q.set_name for q in fake.searches] == ["", ""]


def test_run_tcdb_add_dry_run_adds_nothing(monkeypatch, fake_settings):
    fake = FakeBrowser({"Matt Cain": _page(CAIN)})
    monkeypatch.setattr(tcdb_add, "TcdbBrowser", fake)
    assert tcdb_add.run_tcdb_add(cards=[CAIN], dry_run=True, console=_quiet_console()) == 0
    assert fake.added == []


def test_run_tcdb_add_rejects_bad_lines_before_opening_a_browser(monkeypatch, fake_settings):
    def boom(*a, **k):
        raise AssertionError("browser must not start")

    monkeypatch.setattr(tcdb_add, "TcdbBrowser", boom)
    assert tcdb_add.run_tcdb_add(cards=[CAIN, "Matt Cain"], console=_quiet_console()) == 2


def _quiet_console() -> Console:
    return Console(file=io.StringIO(), width=200)
