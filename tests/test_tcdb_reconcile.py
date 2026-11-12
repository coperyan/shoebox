"""TCDB reconcile: state and ledger, query rows -> SoldCard, candidate ranking,
command parsing, the browser's remove flow against a fake driver, and the
tcdb-reconcile pipeline against a fake browser and a fake BigQuery client.
No real browser or BigQuery is touched here."""

from __future__ import annotations

import io
import json
import math
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

import shoebox.pipelines.tcdb_reconcile as rec
from shoebox.clients.tcdb import TcdbBrowser
from shoebox.clients.tcdb.card import parse_collection_widget
from shoebox.models.tcdb import (
    CollectionInspection,
    CollectionRemoveResult,
    CollectionWidget,
    ReconcileEntry,
    ReconcileState,
    SoldCard,
    TcdbCardPage,
    TcdbSearchPage,
    TcdbSearchResult,
)

FIXTURE = Path(__file__).parent / "fixtures" / "tcdb_view_card.html"
CAIN_URL = "https://www.tcdb.com/ViewCard.cfm/sid/24459/cid/2840262/2009-Bowman-Chrome-X-Fractors-171-Matt-Cain"

QUICK_ADD_BOX = (
    '<button id="quickAddBtn" data-collectionid="1" data-cardid="2840262">Quick Add</button>'
)
OWNED_BOX = (
    '<span class="badge">1</span>'
    '<button class="addAnotherBtn">Add another</button><button class="removeBtn">Remove</button>'
)
OWNED_TWO_BOX = OWNED_BOX.replace('class="badge">1<', 'class="badge">2<')


def _quiet_console() -> Console:
    return Console(file=io.StringIO(), width=120, force_terminal=False)


def _card(**kw) -> SoldCard:
    base = dict(
        order_line_key="k1",
        order_id="11-22222-33333",
        line_item_id="1",
        order_created_date=date(2026, 11, 2),
        ebay_title="2020 Topps Chrome Pink Refractor Mike Trout #1",
        sku="ABC",
        quantity=1,
        matched=True,
        set_year="2020",
        set_name="2020 Topps Chrome",
        subset_name="Base",
        subset_type="Base",
        parallel_variety="Pink Refractor",
        card_number="1",
        player="Mike Trout",
        team="Los Angeles Angels",
    )
    base.update(kw)
    return SoldCard(**base)


def _result(index: int, title: str, cid: int | None = None) -> TcdbSearchResult:
    cid = cid or index
    return TcdbSearchResult(
        index=index, title=title, url=f"https://www.tcdb.com/ViewCard.cfm/sid/1/cid/{cid}"
    )


def _page(*titles: str) -> TcdbSearchPage:
    results = [_result(i, t) for i, t in enumerate(titles, 1)]
    return TcdbSearchPage(query_url="q", total_results=len(results), results=results)


# --------------------------------------------------------------------------- models


def test_sold_card_description_is_in_tcdb_title_shape():
    assert _card().description == "2020 Topps Chrome - Pink Refractor #1 Mike Trout"
    assert _card(parallel_variety="").description == "2020 Topps Chrome #1 Mike Trout"
    insert = _card(subset_name="Future Stars", subset_type="Insert", parallel_variety="")
    assert insert.description == "2020 Topps Chrome - Future Stars #1 Mike Trout"
    assert _card(matched=False).description == _card().ebay_title


def test_sold_card_search_name_is_the_first_player():
    assert _card(player="Barry Bonds / Neil Allen").search_name == "Barry Bonds"


# --------------------------------------------------------------------------- state


def test_state_round_trips_and_defaults(tmp_path):
    assert rec.load_state(tmp_path) == ReconcileState()
    rec.save_state(tmp_path, ReconcileState(reconciled_through=date(2026, 11, 1)))
    loaded = rec.load_state(tmp_path)
    assert loaded.reconciled_through == date(2026, 11, 1)
    assert loaded.updated_at is not None


def test_ledger_appends_and_only_final_entries_count(tmp_path):
    for key, status in [("a", "removed"), ("b", "skipped"), ("c", "ignored"), ("d", "failed")]:
        rec.append_ledger(
            tmp_path,
            ReconcileEntry(order_line_key=key, order_created_date=date(2026, 11, 1), status=status),
        )
    ledger = rec.load_ledger(tmp_path)
    assert [e.order_line_key for e in ledger] == ["a", "b", "c", "d"]
    assert rec.final_keys(ledger) == {"a", "c"}
    cards = [_card(order_line_key=k) for k in "abcd"]
    assert [c.order_line_key for c in rec.pending_cards(cards, ledger)] == ["b", "d"]


def test_next_reconciled_through_is_held_back_by_open_sales():
    today = date(2026, 11, 11)
    assert rec.next_reconciled_through([], today) == today
    open_sales = [
        _card(order_created_date=date(2026, 11, 5)),
        _card(order_created_date=date(2026, 11, 3)),
    ]
    assert rec.next_reconciled_through(open_sales, today) == date(2026, 11, 2)


def test_since_for_run_prefers_the_flag_then_the_stored_date():
    state = ReconcileState(reconciled_through=date(2026, 11, 1))
    assert rec.since_for_run("2026-10-01", state) == date(2026, 10, 1)
    assert rec.since_for_run(None, state) == date(2026, 11, 2)
    assert rec.since_for_run(None, ReconcileState()) is None
    with pytest.raises(ValueError):
        rec.since_for_run("yesterday", state)


# --------------------------------------------------------------------------- rows


def test_sold_cards_from_rows_cleans_pandas_values():
    rows = [
        {
            "order_line_key": "k",
            "order_id": "o",
            "line_item_id": 7,
            "order_created_date": date(2026, 11, 2),
            "ebay_title": "t",
            "sku": "s",
            "quantity": 2,
            "legacy_item_id": 123456789,
            "is_matched_to_meta": 1,
            "set_year": "2020",
            "set_name": "2020 Topps Chrome",
            "subset_name": "Base",
            "subset_type": "Base",
            "card_number": "1",
            "player": "Mike Trout",
            "team": "Los Angeles Angels",
            "parallel_variety": math.nan,
            "print_run": math.nan,
        },
        {
            "order_line_key": "u",
            "order_created_date": date(2026, 11, 3),
            "ebay_title": "unmatched",
            "quantity": None,
            "is_matched_to_meta": 0,
            "set_name": None,
        },
    ]
    cards = rec.sold_cards_from_rows(rows)
    assert cards[0].quantity == 2
    assert cards[0].legacy_item_id == "123456789"
    assert cards[0].line_item_id == "7"
    assert cards[0].parallel_variety == ""
    assert cards[0].print_run is None
    assert cards[0].matched
    assert not cards[1].matched
    assert cards[1].quantity == 1
    assert cards[1].set_name == ""


# --------------------------------------------------------------------------- search


@pytest.mark.parametrize(
    "set_name, category",
    [
        ("2026-27 Topps Flagship Basketball", "Basketball"),
        ("2020 Topps Chrome", "Baseball"),
        ("2024 Panini Prizm Football", "Football"),
        ("", "Baseball"),
    ],
)
def test_guess_category(set_name, category):
    assert rec.guess_category(set_name) == category


def test_search_uses_year_number_and_first_name_but_not_set():
    q = rec.search_query_for(_card(player="Barry Bonds / Neil Allen"), "Baseball")
    assert (q.year, q.card_number, q.name, q.set_name) == ("2020", "1", "Barry Bonds", "")


@pytest.mark.parametrize(
    "title, expected",
    [
        ("2024 Topps Heritage High Number - Jackson Chourio #632 Brewers (RC)", ("2024", "632", "Chourio")),
        ("2024 Topps Series 2 - 1989 Topps Baseball Prince Fielder #89B2-23 Brewers", ("2024", "89B2-23", "Fielder")),
        ("2026 Topps Holiday Baseball - Jazz Chisholm Jr. #H53 Blue Glitter Yankees", ("2026", "H53", "Chisholm")),
        ("2026-27 Topps Basketball - Ja Morant #36 Grizzlies", ("2026-27", "36", "Morant")),
        ("Lot of 5 cards", ("", "", "")),
        ("", ("", "", "")),
    ],
)  # fmt: skip
def test_parse_ebay_title(title, expected):
    assert rec.parse_ebay_title(title) == expected


def test_unmatched_cards_are_searched_from_the_ebay_title():
    card = _card(matched=False, ebay_title="2024 Topps Heritage - Sal Frelick #451 Brewers (RC)")
    q = rec.search_query_for(card, "Baseball")
    assert (q.year, q.card_number, q.name) == ("2024", "451", "Frelick")
    ranked = rec.rank_candidates(
        card,
        _page(
            "2024 Topps Heritage #451 Sal Frelick", "2024 Topps Heritage - Chrome #451 Sal Frelick"
        ).results,
    )
    assert rec.suggest(ranked).index == 1


def test_rank_candidates_stars_the_parallel_over_the_base_card():
    page = _page(
        "2020 Topps Chrome #1 Mike Trout",
        "2020 Topps Chrome - Pink Refractor #1 Mike Trout",
        "2020 Topps Chrome - Gold Refractor #1 Mike Trout",
    )
    ranked = rec.rank_candidates(_card(), page.results)
    assert ranked[0].result.index == 2 and ranked[0].score == 1.0
    assert rec.suggest(ranked).index == 2


def test_rank_candidates_stars_the_base_card_when_nothing_was_sold_as_a_parallel():
    page = _page(
        "2020 Topps Chrome #1 Mike Trout",
        "2020 Topps Chrome - Pink Refractor #1 Mike Trout",
    )
    ranked = rec.rank_candidates(_card(parallel_variety=""), page.results)
    assert rec.suggest(ranked).index == 1


def test_suggest_declines_ties_and_weak_matches():
    tie = _page("2020 Topps Chrome #1 Mike Trout", "2020 Topps Chrome #1 Mike Trout")
    assert rec.suggest(rec.rank_candidates(_card(parallel_variety=""), tie.results)) is None
    weak = _page("1993 Upper Deck #1 Barry Bonds")
    assert rec.suggest(rec.rank_candidates(_card(), weak.results)) is None
    assert rec.suggest([]) is None


# --------------------------------------------------------------------------- commands


@pytest.mark.parametrize(
    "line, kind, index, updates",
    [
        ("", "accept", None, {}),
        ("   ", "accept", None, {}),
        ("3", "remove", 3, {}),
        ("remove 3", "remove", 3, {}),
        ("rm 3", "remove", 3, {}),
        ("open 2", "open", 2, {}),
        ("o 2", "open", 2, {}),
        ("skip", "skip", None, {}),
        ("s", "skip", None, {}),
        ("ignore", "ignore", None, {}),
        ("i", "ignore", None, {}),
        ("help", "help", None, {}),
        ("?", "help", None, {}),
        ("q", "quit", None, {}),
        ("quit", "quit", None, {}),
        ('set name=Bonds year=1993 set_name="Upper Deck"', "set", None,
         {"name": "Bonds", "year": "1993", "set_name": "Upper Deck"}),
    ],
)  # fmt: skip
def test_parse_reconcile_command(line, kind, index, updates):
    cmd = rec.parse_reconcile_command(line)
    assert (cmd.kind, cmd.index, cmd.updates) == (kind, index, updates)


@pytest.mark.parametrize("line", ["open x", "set", "set name", "add 3", "whatever"])
def test_parse_reconcile_command_rejects_garbage(line):
    with pytest.raises(ValueError):
        rec.parse_reconcile_command(line)


# --------------------------------------------------------------------------- browser


class FakeDriver:
    """Just enough of a Selenium driver for remove_from_collection: serves
    #colDiv's HTML, records clicks, and reports the remove request the page
    would make. ``boxes`` is what the box shows after each successive click."""

    def __init__(self, box: str, *boxes: str, status: int = 200, refresh: bool = True):
        self.box = box
        self.after = list(boxes)
        self.status = status
        self.refresh = refresh
        self.clicked: list[str] = []
        self.requests: list[dict] = []

    def execute_script(self, script: str, *args):
        if "el.click()" in script:
            self.clicked.append(args[0])
            assert args[0] == ".removeBtn"
            self.requests.append(
                {
                    "url": "https://www.tcdb.com/CollectionRemove_ajax.cfm?CardID=2840262",
                    "ok": 200 <= self.status < 300,
                    "status": self.status,
                }
            )
            if self.refresh:
                self.box = self.after.pop(0)
            return True
        if "__shoebox.requests" in script and script.lstrip().startswith("return"):
            return list(self.requests)
        if "getElementById('colDiv')" in script:
            return self.box
        self.requests = []  # the fetch-watcher install resets the log
        return None


@pytest.fixture
def card_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _browser(monkeypatch, card_html, driver: FakeDriver) -> TcdbBrowser:
    browser = TcdbBrowser("unused", settle_delay_s=0)
    browser.driver = driver
    monkeypatch.setattr(browser, "get", lambda url: card_html)
    return browser


def test_remove_clicks_remove_and_sees_the_card_go(monkeypatch, card_html):
    driver = FakeDriver(OWNED_BOX, QUICK_ADD_BOX)
    res = _browser(monkeypatch, card_html, driver).remove_from_collection(CAIN_URL, poll_s=0)
    assert res.ok and res.status == "removed"
    assert (res.copies_removed, res.still_owned) == (1, False)
    assert driver.clicked == [".removeBtn"]
    assert res.card.title == "2009 Bowman Chrome - X-Fractors #171 Matt Cain"
    assert "CollectionRemove_ajax.cfm" in res.request_url


def test_remove_two_copies_clicks_twice(monkeypatch, card_html):
    driver = FakeDriver(OWNED_TWO_BOX, OWNED_BOX, QUICK_ADD_BOX)
    res = _browser(monkeypatch, card_html, driver).remove_from_collection(
        CAIN_URL, copies=2, poll_s=0
    )
    assert res.ok and res.copies_removed == 2 and res.still_owned is False
    assert driver.clicked == [".removeBtn"] * 2


def test_remove_stops_when_fewer_copies_are_owned(monkeypatch, card_html):
    driver = FakeDriver(OWNED_BOX, QUICK_ADD_BOX)
    res = _browser(monkeypatch, card_html, driver).remove_from_collection(
        CAIN_URL, copies=3, poll_s=0
    )
    assert res.status == "removed" and not res.ok
    assert res.copies_removed == 1
    assert "only 1 of 3" in res.detail
    assert driver.clicked == [".removeBtn"]


def test_remove_skips_a_card_not_in_the_collection(monkeypatch, card_html):
    driver = FakeDriver(QUICK_ADD_BOX)
    res = _browser(monkeypatch, card_html, driver).remove_from_collection(CAIN_URL, poll_s=0)
    assert res.status == "not_owned" and res.copies_removed == 0
    assert driver.clicked == []


def test_remove_reports_http_errors(monkeypatch, card_html):
    driver = FakeDriver(OWNED_BOX, OWNED_BOX, status=500)
    res = _browser(monkeypatch, card_html, driver).remove_from_collection(CAIN_URL, poll_s=0)
    assert res.status == "failed" and "HTTP 500" in res.detail


def test_remove_never_clicks_a_box_that_did_not_refresh(monkeypatch, card_html):
    driver = FakeDriver(OWNED_TWO_BOX, refresh=False)
    browser = _browser(monkeypatch, card_html, driver)
    res = browser.remove_from_collection(CAIN_URL, copies=2, timeout_s=0.01, poll_s=0)
    assert res.status == "removed" and res.copies_removed == 1
    assert "did not refresh" in res.detail
    assert driver.clicked == [".removeBtn"]


def test_remove_times_out_when_the_box_never_loads(monkeypatch, card_html):
    driver = FakeDriver("")
    res = _browser(monkeypatch, card_html, driver).remove_from_collection(
        CAIN_URL, timeout_s=0.01, poll_s=0
    )
    assert res.status == "failed" and res.still_owned is None


# --------------------------------------------------------------------------- pipeline


class FakeBrowser:
    """Stands in for TcdbBrowser inside run_tcdb_reconcile."""

    def __init__(
        self,
        pages: dict[str, TcdbSearchPage],
        *,
        not_owned: set[str] = frozenset(),
        owned: dict[str, int | None] | None = None,
    ):
        self.pages = pages
        self.not_owned = set(not_owned)
        # url -> copies the collection box reports (None = box does not say)
        self.owned = owned or {}
        self.inspected: list[str] = []
        self.searches: list = []
        self.removed: list[tuple[str, int]] = []
        self.opened: list[str] = []

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
        return self.pages.get(query.name) or self.pages.get(query.card_number) or _page()

    def open(self, url):
        self.opened.append(url)

    def inspect_collection(self, url):
        self.inspected.append(url)
        card = TcdbCardPage(url=url, title="t")
        if url in self.not_owned:
            return CollectionInspection(
                card=card, widget=CollectionWidget(loaded=True, quick_add=True)
            )
        qty = self.owned.get(url, 1)
        widget = CollectionWidget(loaded=True, add_another=True, remove=True, quantity=qty)
        return CollectionInspection(card=card, widget=widget)

    def remove_from_collection(self, url, *, copies=1):
        card = TcdbCardPage(url=url, title="t")
        if url in self.not_owned:
            return CollectionRemoveResult(
                status="not_owned",
                card=card,
                copies_requested=copies,
                detail="not in the collection",
            )
        self.removed.append((url, copies))
        return CollectionRemoveResult(
            status="removed", card=card, copies_requested=copies, copies_removed=copies
        )


class FakeBq:
    def __init__(self, cards: list[SoldCard]):
        self.cards = cards
        self.params: list[dict] = []

    def run_query(self, sql, params=None, *, return_df=True):
        assert sql == rec.SOLD_CARDS_SQL
        self.params.append(params)
        since = date.fromisoformat(params["since"])
        rows = [
            {**c.model_dump(), "is_matched_to_meta": int(c.matched)}
            for c in self.cards
            if c.order_created_date >= since
        ]
        return SimpleNamespace(to_dict=lambda kind: rows)


@pytest.fixture
def fake_settings(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        tcdb=SimpleNamespace(
            profile_dir=str(tmp_path / "p"), login_timeout_s=1, search_defaults={}
        ),
        paths=SimpleNamespace(data_dir=str(tmp_path)),
    )
    monkeypatch.setattr(rec, "get_settings", lambda: settings)
    return settings


def _inputs(*lines: str):
    it = iter(lines)

    def fn(prompt: str) -> str:
        try:
            return next(it)
        except StopIteration as e:
            raise EOFError from e

    return fn


TROUT = "2020 Topps Chrome - Pink Refractor #1 Mike Trout"
BONDS = "1993 Upper Deck #25 Barry Bonds"


def _state_dir(settings) -> Path:
    return Path(settings.paths.data_dir) / rec.STATE_DIRNAME


def test_first_run_needs_since(fake_settings):
    code = rec.run_tcdb_reconcile(bq=FakeBq([]), console=_quiet_console())
    assert code == 2


def test_dry_run_lists_without_a_browser_or_state(monkeypatch, fake_settings):
    def boom(*a, **k):
        raise AssertionError("browser must not start on a dry run")

    monkeypatch.setattr(rec, "TcdbBrowser", boom)
    bq = FakeBq([_card()])
    code = rec.run_tcdb_reconcile(since="2026-11-01", dry_run=True, bq=bq, console=_quiet_console())
    assert code == 0
    assert bq.params == [{"since": "2026-11-01"}]
    assert not (_state_dir(fake_settings) / rec.STATE_FILENAME).exists()


def test_run_removes_accepts_skips_ignores_and_quits(monkeypatch, fake_settings):
    cards = [
        _card(order_line_key="k1", order_created_date=date(2026, 11, 2)),  # Enter -> star
        _card(order_line_key="k2", order_created_date=date(2026, 11, 3), quantity=2,
              set_year="1993", set_name="1993 Upper Deck", parallel_variety="",
              card_number="25", player="Barry Bonds"),  # "2" -> explicit pick
        _card(order_line_key="k3", order_created_date=date(2026, 11, 4)),  # skip
        _card(order_line_key="k4", order_created_date=date(2026, 11, 5)),  # ignore
        _card(order_line_key="k5", order_created_date=date(2026, 11, 6)),  # quit before
    ]  # fmt: skip
    pages = {
        "Mike Trout": _page("2020 Topps Chrome #1 Mike Trout", TROUT),
        "Barry Bonds": _page("1993 Upper Deck #25 Barry Bonds", "1993 Upper Deck - Gold #25 Barry Bonds"),
    }  # fmt: skip
    fake = FakeBrowser(pages, owned={"https://www.tcdb.com/ViewCard.cfm/sid/1/cid/2": 3})
    monkeypatch.setattr(rec, "TcdbBrowser", fake)
    code = rec.run_tcdb_reconcile(
        since="2026-11-01",
        bq=FakeBq(cards),
        # k2: pick result 2, then Enter at the keep prompt (3 owned - 2 sold = keep 1)
        input_fn=_inputs("", "open 2", "2", "", "skip", "ignore", "quit"),
        console=_quiet_console(),
        today=date(2026, 11, 11),
    )
    assert code == 0
    assert fake.removed == [
        ("https://www.tcdb.com/ViewCard.cfm/sid/1/cid/2", 1),
        ("https://www.tcdb.com/ViewCard.cfm/sid/1/cid/2", 2),
    ]
    assert fake.opened == ["https://www.tcdb.com/ViewCard.cfm/sid/1/cid/2"]
    assert fake.inspected == ["https://www.tcdb.com/ViewCard.cfm/sid/1/cid/2"], (
        "only the qty-2 sale"
    )
    # every card shown is searched first, including k5, which was then quit on
    assert [q.name for q in fake.searches] == ["Mike Trout", "Barry Bonds"] + ["Mike Trout"] * 3

    ledger = rec.load_ledger(_state_dir(fake_settings))
    assert [(e.order_line_key, e.status) for e in ledger] == [
        ("k1", "removed"), ("k2", "removed"), ("k3", "skipped"), ("k4", "ignored")
    ]  # fmt: skip
    assert ledger[0].tcdb_title == "t" and ledger[1].copies_removed == 2
    # k3 was skipped and k5 never reached: the date stops the day before k3.
    state = rec.load_state(_state_dir(fake_settings))
    assert state.reconciled_through == date(2026, 11, 3)


def test_second_run_starts_after_the_stored_date_and_hides_decided_sales(
    monkeypatch, fake_settings
):
    state_dir = _state_dir(fake_settings)
    rec.save_state(state_dir, ReconcileState(reconciled_through=date(2026, 11, 3)))
    rec.append_ledger(
        state_dir,
        ReconcileEntry(order_line_key="k4", order_created_date=date(2026, 11, 5), status="ignored"),
    )
    cards = [
        _card(order_line_key="k3", order_created_date=date(2026, 11, 4)),
        _card(order_line_key="k4", order_created_date=date(2026, 11, 5)),
        _card(order_line_key="k5", order_created_date=date(2026, 11, 6)),
    ]
    fake = FakeBrowser({"Mike Trout": _page(TROUT)})
    monkeypatch.setattr(rec, "TcdbBrowser", fake)
    bq = FakeBq(cards)
    code = rec.run_tcdb_reconcile(
        bq=bq, input_fn=_inputs("", ""), console=_quiet_console(), today=date(2026, 11, 11)
    )
    assert code == 0
    assert bq.params == [{"since": "2026-11-04"}]
    assert len(fake.removed) == 2, "k4 was already ignored; k3 and k5 get removed"
    assert rec.load_state(state_dir).reconciled_through == date(2026, 11, 11)


def test_nothing_pending_advances_the_date_without_a_browser(monkeypatch, fake_settings):
    monkeypatch.setattr(rec, "TcdbBrowser", lambda *a, **k: pytest.fail("no browser needed"))
    code = rec.run_tcdb_reconcile(
        since="2026-11-01", bq=FakeBq([]), console=_quiet_console(), today=date(2026, 11, 11)
    )
    assert code == 0
    assert rec.load_state(_state_dir(fake_settings)).reconciled_through == date(2026, 11, 11)


def test_set_reruns_the_search_and_an_unreadable_title_searches_nothing_by_default(
    monkeypatch, fake_settings
):
    cards = [
        _card(matched=False, ebay_title="Lot of cards", set_year="", card_number="", player="")
    ]
    fake = FakeBrowser({"Bonds": _page(BONDS)})
    monkeypatch.setattr(rec, "TcdbBrowser", fake)
    code = rec.run_tcdb_reconcile(
        since="2026-11-01",
        bq=FakeBq(cards),
        input_fn=_inputs("1", "set year=1993 card_number=25 name=Bonds", "1"),
        console=_quiet_console(),
        today=date(2026, 11, 11),
    )
    assert code == 0
    assert len(fake.searches) == 1, "no search until the user supplied fields"
    assert fake.searches[0].year == "1993"
    assert fake.removed == [("https://www.tcdb.com/ViewCard.cfm/sid/1/cid/1", 1)]


def test_failed_removes_are_not_final(monkeypatch, fake_settings):
    url = "https://www.tcdb.com/ViewCard.cfm/sid/1/cid/2"
    fake = FakeBrowser(
        {"Mike Trout": _page("2020 Topps Chrome #1 Mike Trout", TROUT)}, not_owned={url}
    )
    monkeypatch.setattr(rec, "TcdbBrowser", fake)
    code = rec.run_tcdb_reconcile(
        since="2026-11-01",
        bq=FakeBq([_card()]),
        input_fn=_inputs(""),
        console=_quiet_console(),
        today=date(2026, 11, 11),
    )
    assert code == 1
    ledger = rec.load_ledger(_state_dir(fake_settings))
    assert ledger[0].status == "failed" and "not in the collection" in ledger[0].detail
    assert rec.load_state(_state_dir(fake_settings)).reconciled_through == date(2026, 11, 1)


@pytest.mark.parametrize(
    "line, default, expected",
    [("", 1, 1), ("3", 1, 3), ("0", 2, 0), ("skip", 1, "skip"), ("s", 1, "skip"), ("q", 1, "quit")],
)
def test_parse_count(line, default, expected):
    assert rec.parse_count(line, default) == expected


@pytest.mark.parametrize("line", ["two", "-1", "1.5", "keep 2"])
def test_parse_count_rejects_garbage(line):
    with pytest.raises(ValueError):
        rec.parse_count(line, 1)


@pytest.mark.parametrize(
    "box, quantity",
    [
        (OWNED_BOX, 1),
        (OWNED_TWO_BOX, 2),
        ('<span class="badge bg-primary" title="Quantity">4</span><button class="removeBtn">x</button>', 4),
        ('<p>You have 3 of this card</p><button class="removeBtn">x</button>', 3),
        ('<p>Qty: 5</p><button class="removeBtn">x</button>', 5),
        ('<button class="removeBtn">Remove</button>', None),
        (QUICK_ADD_BOX, None),
        ("", None),
    ],
)  # fmt: skip
def test_collection_widget_quantity(box, quantity):
    assert parse_collection_widget(box).quantity == quantity


URL2 = "https://www.tcdb.com/ViewCard.cfm/sid/1/cid/2"


def _run_multi(monkeypatch, fake_settings, fake, *answers: str, sold: int = 2) -> int:
    monkeypatch.setattr(rec, "TcdbBrowser", fake)
    return rec.run_tcdb_reconcile(
        since="2026-11-01",
        bq=FakeBq([_card(quantity=sold)]),
        input_fn=_inputs("", *answers),
        console=_quiet_console(),
        today=date(2026, 11, 11),
    )


def test_single_copy_sales_remove_without_a_question(monkeypatch, fake_settings):
    fake = FakeBrowser({"Mike Trout": _page("2020 Topps Chrome #1 Mike Trout", TROUT)})
    assert _run_multi(monkeypatch, fake_settings, fake, sold=1) == 0
    assert fake.inspected == [] and fake.removed == [(URL2, 1)]


def test_multi_copy_sale_asks_how_many_to_keep(monkeypatch, fake_settings):
    fake = FakeBrowser({"Mike Trout": _page("x", TROUT)}, owned={URL2: 5})
    assert _run_multi(monkeypatch, fake_settings, fake, "4", sold=2) == 0
    assert fake.removed == [(URL2, 1)], "5 owned, keep 4 -> remove 1"
    entry = rec.load_ledger(_state_dir(fake_settings))[0]
    assert (entry.status, entry.copies_removed) == ("removed", 1)


def test_keeping_every_copy_settles_the_sale_without_removing(monkeypatch, fake_settings):
    fake = FakeBrowser({"Mike Trout": _page("x", TROUT)}, owned={URL2: 2})
    assert _run_multi(monkeypatch, fake_settings, fake, "2", sold=2) == 0
    assert fake.removed == []
    entry = rec.load_ledger(_state_dir(fake_settings))[0]
    assert entry.status == "ignored" and "kept all 2" in entry.detail
    assert rec.load_state(_state_dir(fake_settings)).reconciled_through == date(2026, 11, 11)


def test_keep_prompt_rejects_more_than_owned_then_accepts(monkeypatch, fake_settings):
    fake = FakeBrowser({"Mike Trout": _page("x", TROUT)}, owned={URL2: 3})
    assert _run_multi(monkeypatch, fake_settings, fake, "7", "two", "0", sold=2) == 0
    assert fake.removed == [(URL2, 3)]


def test_unknown_owned_count_asks_how_many_to_remove(monkeypatch, fake_settings):
    fake = FakeBrowser({"Mike Trout": _page("x", TROUT)}, owned={URL2: None})
    assert _run_multi(monkeypatch, fake_settings, fake, "", sold=3) == 0
    assert fake.removed == [(URL2, 3)], "Enter removes the quantity sold"


def test_skip_and_quit_at_the_keep_prompt(monkeypatch, fake_settings):
    fake = FakeBrowser({"Mike Trout": _page("x", TROUT)}, owned={URL2: 3})
    assert _run_multi(monkeypatch, fake_settings, fake, "skip", sold=2) == 0
    assert fake.removed == []
    assert rec.load_ledger(_state_dir(fake_settings))[0].status == "skipped"

    fake2 = FakeBrowser({"Mike Trout": _page("x", TROUT)}, owned={URL2: 3})
    assert _run_multi(monkeypatch, fake_settings, fake2, "quit", sold=2) == 0
    assert fake2.removed == []
    assert len(rec.load_ledger(_state_dir(fake_settings))) == 1, "quit logs nothing"


def test_multi_copy_sale_of_a_card_not_owned_fails_without_removing(monkeypatch, fake_settings):
    fake = FakeBrowser({"Mike Trout": _page("x", TROUT)}, not_owned={URL2})
    assert _run_multi(monkeypatch, fake_settings, fake, sold=2) == 1
    assert fake.removed == []
    assert rec.load_ledger(_state_dir(fake_settings))[0].status == "failed"


def test_state_file_is_plain_json(fake_settings):
    state_dir = _state_dir(fake_settings)
    rec.save_state(state_dir, ReconcileState(reconciled_through=date(2026, 11, 3)))
    raw = json.loads((state_dir / rec.STATE_FILENAME).read_text())
    assert raw["reconciled_through"] == "2026-11-03"
