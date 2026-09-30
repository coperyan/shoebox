"""Tests for the TCDB want-list parser and export.

The fixture is a trimmed real ``ViewCollectionMode.cfm?Filter=W`` page: three
rows (one priced, two not, two of them multi-player cards), the record count
and the pager.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from shoebox.clients.tcdb.collection import (
    ROWS_PER_PAGE,
    parse_member,
    parse_wantlist_page,
    wantlist_url,
)
from shoebox.models.tcdb import WantlistCard, WantlistPage
from shoebox.pipelines.tcdb_wantlist import (
    FIELDS,
    card_row,
    default_out_path,
    write_wantlist,
)

FIXTURE = Path(__file__).parent / "fixtures" / "tcdb_wantlist.html"


@pytest.fixture(scope="module")
def page() -> WantlistPage:
    return parse_wantlist_page(
        FIXTURE.read_text(encoding="utf-8"), category="Baseball", query_url="http://x"
    )


class TestWantlistUrl:
    def test_first_page_omits_paging_params(self):
        url = wantlist_url("rcbbsf247")
        assert "ViewCollectionMode.cfm" in url
        assert "Member=rcbbsf247" in url
        assert "Filter=W" in url
        assert "Type=Baseball" in url
        assert "PageIndex" not in url

    def test_later_page_carries_index_and_records(self):
        url = wantlist_url("rcbbsf247", page_index=3, records=4018)
        assert "PageIndex=3" in url
        assert "Records=4018" in url

    def test_category_is_url_encoded(self):
        assert "Type=Misc+Sports" in wantlist_url("x", category="Misc Sports")

    def test_honours_base_url(self):
        assert wantlist_url("x", base_url="http://local/").startswith("http://local/View")


class TestParseWantlistPage:
    def test_reads_every_row(self, page):
        assert len(page.cards) == 3

    def test_reads_the_record_count(self, page):
        assert page.total_records == 4018

    def test_derives_page_count_from_the_record_count(self, page):
        # 4018 rows at 100 a page
        assert page.total_pages == 41
        assert page.has_more is True

    def test_reads_title_and_url(self, page):
        card = page.cards[0]
        assert card.title == "1986 Broder (unlicensed) #28 Barry Bonds"
        assert card.url.startswith("https://www.tcdb.com/ViewCard.cfm/sid/110886/cid/7596583/")

    def test_reads_the_ids_from_the_edit_link(self, page):
        card = page.cards[0]
        assert (card.set_id, card.card_id, card.item_id) == (110886, 7596583, 928945277)

    def test_reads_quantity_and_status(self, page):
        assert page.cards[0].quantity == 1
        assert page.cards[0].status == "Wantlist"

    def test_reads_a_price_when_the_row_has_one(self, page):
        assert page.cards[0].price == 3.50
        assert page.cards[0].price_text == "$3.50"

    def test_blank_price_stays_none(self, page):
        assert page.cards[1].price is None
        assert page.cards[1].price_text == ""

    def test_keeps_multi_player_titles_whole(self, page):
        assert page.cards[1].title == "1987 O-Pee-Chee Stickers #131 / 292 Barry Bonds / Neil Allen"

    def test_stamps_the_category_on_every_card(self, page):
        assert {c.category for c in page.cards} == {"Baseball"}

    def test_empty_page_is_not_an_error(self):
        page = parse_wantlist_page("<html><body><p>0 record(s)</p></body></html>")
        assert page.cards == []
        assert page.total_records == 0
        assert page.has_more is False

    def test_rows_without_a_card_link_are_skipped(self):
        html = '<table><tr class="collection_row"><td>junk</td></tr></table>'
        assert parse_wantlist_page(html).cards == []

    def test_page_count_prefers_the_pager_when_it_is_higher(self):
        html = (
            "<p>50 record(s)</p>"
            '<a href="/x.cfm?PageIndex=7">7</a><a href="/x.cfm?PageIndex=3">3</a>'
        )
        # 50 records implies 1 page, but the pager says 7
        assert parse_wantlist_page(html).total_pages == 7

    def test_rows_per_page_matches_the_site(self):
        assert ROWS_PER_PAGE == 100


class TestParseMember:
    def test_reads_the_username_from_the_profile_link(self):
        html = '<ul><li><a href="/Profile.cfm/rcbbsf247">Profile</a></li></ul>'
        assert parse_member(html) == "rcbbsf247"

    def test_none_when_signed_out(self):
        assert parse_member('<a href="/Login.cfm">Login</a>') is None


class TestWriteWantlist:
    def _cards(self):
        return [
            WantlistCard(
                title="1986 Broder #28 Barry Bonds",
                url="https://www.tcdb.com/ViewCard.cfm/sid/1/cid/2/x",
                set_id=1,
                card_id=2,
                item_id=3,
                quantity=2,
                price=3.5,
                price_text="$3.50",
                status="Wantlist",
                category="Baseball",
            ),
            WantlistCard(title="No price", url="http://x", category="Baseball"),
        ]

    def test_card_row_has_exactly_the_declared_fields(self):
        assert tuple(card_row(self._cards()[0])) == FIELDS

    def test_csv_round_trips(self, tmp_path):
        out = write_wantlist(self._cards(), tmp_path / "w.csv", "csv")
        rows = list(csv.DictReader(out.open(encoding="utf-8")))
        assert len(rows) == 2
        assert rows[0]["title"] == "1986 Broder #28 Barry Bonds"
        assert rows[0]["quantity"] == "2"
        assert rows[0]["price"] == "3.5"
        assert rows[1]["price"] == ""

    def test_jsonl_round_trips(self, tmp_path):
        out = write_wantlist(self._cards(), tmp_path / "w.jsonl", "jsonl")
        rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 2
        assert rows[0]["price"] == 3.5
        assert rows[1]["price"] is None

    def test_creates_the_output_directory(self, tmp_path):
        out = write_wantlist(self._cards(), tmp_path / "deep" / "w.csv", "csv")
        assert out.exists()

    def test_default_path_is_stamped_and_foldered_by_format(self, tmp_path):
        p = default_out_path(tmp_path, "jsonl", stamp="20260929_120000")
        assert p == tmp_path / "jsonl" / "tcdb_wantlist_20260929_120000.jsonl"
