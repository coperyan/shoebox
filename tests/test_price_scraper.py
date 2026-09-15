from datetime import UTC, datetime, timedelta

import pytest
from bs4 import BeautifulSoup

from shoebox.clients.price_scraper import PriceScraper

# Markup mirrors 130point's sold-results rows as of their Next.js rebuild: the
# card title lives in an img alt, and Best Offer rows show the asking price
# struck through ahead of the price the offer was accepted at.
BEST_OFFER_ROW = """
<a href="https://www.ebay.com/itm/820127152884">
  <img alt="Jackson Merrill 2024 Topps Chrome Cosmic /75 Green Refractor PSA 3"/>
  <img alt="eBay"/>
  <p class="font-bold text-[var(--icon-orange)] line-through"><span>$14.99 USD</span></p>
  <p class="font-bold text-[var(--text-default)]"><span>$14.00 USD</span></p>
  <p><span>Best Offer Accepted</span></p>
</a>
"""

AUCTION_ROW = """
<a href="https://www.ebay.com/itm/128070773621">
  <img alt="2024 Topps Chrome Jackson Merrill Rookie RC #207 Padres PSA 9"/>
  <p class="font-bold text-[var(--text-default)]"><span>$9.26 USD</span></p>
  <p><span>Auction</span></p><p><span>6 bids</span></p>
</a>
"""

FIXED_ROW = """
<a href="https://www.ebay.com/itm/999000111222">
  <img alt="2024 Topps Chrome Update Jackson Merrill PSA 10"/>
  <p class="font-bold text-[var(--text-default)]"><span>$1,250.00 USD</span></p>
  <p><span>Fixed Price Listing</span></p>
</a>
"""


def parse(markup: str) -> dict:
    card = BeautifulSoup(markup, "html.parser").find("a")
    return PriceScraper()._parse_card(card)


class TestSalePrice:
    def test_best_offer_uses_accepted_not_asking_price(self):
        assert parse(BEST_OFFER_ROW)["sale_price"] == 14.00

    def test_auction_price(self):
        assert parse(AUCTION_ROW)["sale_price"] == 9.26

    def test_fixed_price_with_thousands_separator(self):
        assert parse(FIXED_ROW)["sale_price"] == 1250.00

    def test_struck_through_only_row_yields_no_price(self):
        markup = """
        <a href="https://www.ebay.com/itm/1"><img alt="t"/>
          <p class="line-through"><span>$14.99 USD</span></p>
        </a>
        """
        assert parse(markup)["sale_price"] is None

    def test_bare_numbers_are_never_treated_as_prices(self):
        markup = """
        <a href="https://www.ebay.com/itm/2">
          <img alt="2024 Topps Chrome #207"/><p><span>2024</span></p>
        </a>
        """
        assert parse(markup)["sale_price"] is None


def local_naive(dt: datetime) -> datetime:
    """Render an aware datetime back in local time, so assertions are tz-independent."""
    return dt.astimezone().replace(tzinfo=None)


class TestSoldDate:
    def test_iso_attribute_on_the_anchor_is_used(self):
        markup = """
        <a href="https://www.ebay.com/itm/1" data-item-endtime="2026-09-15T02:04:45.000Z">
          <img alt="t"/><p><span>$5.00 USD</span></p>
        </a>
        """
        assert parse(markup)["sold_date"] == datetime(2026, 9, 15, 2, 4, 45, tzinfo=UTC)

    def test_falls_back_to_nested_result_end_time(self):
        markup = """
        <a href="https://www.ebay.com/itm/1"><img alt="t"/>
          <span data-result-end-time="2026-09-15T02:04:45.000Z">14 Sept 26 19:04:45</span>
        </a>
        """
        assert parse(markup)["sold_date"] == datetime(2026, 9, 15, 2, 4, 45, tzinfo=UTC)

    def test_iso_attribute_wins_over_display_text(self):
        markup = """
        <a href="https://www.ebay.com/itm/1" data-item-endtime="2026-09-15T02:04:45.000Z">
          <img alt="t"/><p><span>01 Jan 26 00:00:00</span></p>
        </a>
        """
        assert parse(markup)["sold_date"] == datetime(2026, 9, 15, 2, 4, 45, tzinfo=UTC)

    def test_display_text_fallback_when_no_attribute(self):
        markup = """
        <a href="https://www.ebay.com/itm/1"><img alt="t"/>
          <p><span>14 Sept 26 19:04:45</span></p>
        </a>
        """
        # No offset in the text, so it is read as local time.
        assert local_naive(parse(markup)["sold_date"]) == datetime(2026, 9, 14, 19, 4, 45)

    def test_four_letter_september_abbreviation(self):
        # 130point renders en-GB short months, where September alone is 4 letters.
        assert local_naive(PriceScraper._to_datetime("14 Sept 26 19:04:45")) == datetime(
            2026, 9, 14, 19, 4, 45
        )

    def test_three_letter_months_still_parse(self):
        assert local_naive(PriceScraper._to_datetime("03 Mar 26 08:15:00")) == datetime(
            2026, 3, 3, 8, 15, 0
        )

    def test_legacy_format_still_parses(self):
        assert local_naive(PriceScraper._to_datetime("Sun 28 Dec 2025 19:08:30")) == datetime(
            2025, 12, 28, 19, 8, 30
        )

    def test_unparseable_text_yields_nulls(self):
        out = PriceScraper._parse_date("not a date")
        assert out["sold_date"] is None
        assert out["days_ago"] is None
        assert out["sold_date_str"] == "not a date"

    def test_empty_input(self):
        assert PriceScraper._parse_date("")["sold_date"] is None
        assert PriceScraper._parse_date(None)["sold_date"] is None

    def test_days_ago(self):
        three_days_ago = datetime.now(UTC) - timedelta(days=3, hours=1)
        out = PriceScraper._parse_date(three_days_ago.isoformat().replace("+00:00", "Z"))
        assert out["days_ago"] == 3


class TestOtherFields:
    def test_title_comes_from_the_first_img_alt(self):
        assert parse(AUCTION_ROW)["title"] == (
            "2024 Topps Chrome Jackson Merrill Rookie RC #207 Padres PSA 9"
        )

    def test_url(self):
        assert parse(BEST_OFFER_ROW)["url"] == "https://www.ebay.com/itm/820127152884"

    def test_auction_bid_count(self):
        record = parse(AUCTION_ROW)
        assert record["type"] == "Auction"
        assert record["bid_count"] == "6"

    def test_best_offer_type(self):
        assert parse(BEST_OFFER_ROW)["type"] == "Best offer"


class FakeElement:
    """Stands in for the React-controlled search input."""

    def __init__(self, *, interactable_after=0, drops_keystrokes=False):
        self.value = ""
        self.submitted = False
        self.clicks = 0
        self._looks_ready = 0
        self._interactable_after = interactable_after
        self._drops_keystrokes = drops_keystrokes

    def poll_interactable(self) -> bool:
        ready = self._looks_ready >= self._interactable_after
        self._looks_ready += 1
        return ready

    def click(self):
        self.clicks += 1

    def set_value(self, value):
        # A page that drops keystrokes loses the last character of the first write.
        if self._drops_keystrokes and not self.value:
            self.value = value[:-1]
        else:
            self.value = value

    def get_attribute(self, name):
        return self.value if name == "value" else None

    def send_keys(self, keys):
        self.submitted = True


class FakeDriver:
    """Minimal driver: understands the three scripts PriceScraper injects."""

    def __init__(self, element=None, totals=None):
        self.element = element if element is not None else FakeElement()
        # successive answers for the [data-total-results] poll; None means "still loading"
        self.totals = list(totals) if totals is not None else ["200"]

    def find_element(self, by, selector):
        if self.element is None:
            raise RuntimeError("no such element")
        return self.element

    def execute_script(self, script, *args):
        if script is PriceScraper._INTERACTABLE_JS:
            return args[0].poll_interactable()
        if script is PriceScraper._SET_QUERY_JS:
            args[0].set_value(args[1])
            return None
        if script == PriceScraper._TOTAL_JS:
            return self.totals.pop(0) if len(self.totals) > 1 else self.totals[0]
        return None  # scrollIntoView etc.


def scraper_with(driver):
    scraper = PriceScraper(polite_delay_s=0)
    scraper.driver = driver
    return scraper


class TestSearchBoxReadiness:
    def test_waits_until_the_input_is_actually_interactable(self):
        element = FakeElement(interactable_after=3)
        box = scraper_with(FakeDriver(element))._find_search_box(timeout=5)
        assert box is element

    def test_raises_when_the_input_never_becomes_interactable(self):
        element = FakeElement(interactable_after=10_000)
        with pytest.raises(RuntimeError, match="never became interactable"):
            scraper_with(FakeDriver(element))._find_search_box(timeout=0.5)


class TestDoSearch:
    def test_sets_the_whole_query_at_once_and_submits(self):
        element = FakeElement()
        scraper_with(FakeDriver(element))._do_search("Jackson Merrill Refractor")
        assert element.value == "Jackson Merrill Refractor"
        assert element.submitted

    def test_retries_when_the_box_ends_up_with_the_wrong_text(self):
        element = FakeElement(drops_keystrokes=True)
        scraper_with(FakeDriver(element))._do_search("Jackson Merrill")
        assert element.value == "Jackson Merrill"
        assert element.submitted

    def test_never_submits_a_corrupted_query(self):
        class AlwaysDrops(FakeElement):
            def set_value(self, value):
                self.value = value[:-1]

        element = AlwaysDrops()
        with pytest.raises(RuntimeError, match="Could not enter"):
            scraper_with(FakeDriver(element))._do_search("Jackson Merrill", attempts=2)
        assert not element.submitted


class TestWaitForResults:
    def test_returns_the_reported_total(self):
        assert scraper_with(FakeDriver(totals=["200"]))._wait_for_results(timeout=5) == 200

    def test_keeps_waiting_while_the_grid_is_still_empty(self):
        # The panel renders before its rows, so the attribute is absent at first.
        driver = FakeDriver(totals=[None, None, "37"])
        assert scraper_with(driver)._wait_for_results(timeout=5) == 37

    def test_zero_results_is_an_answer_not_a_timeout(self):
        assert scraper_with(FakeDriver(totals=["0"]))._wait_for_results(timeout=5) == 0

    def test_raises_when_the_search_never_resolves(self):
        with pytest.raises(TimeoutError, match="no sold results"):
            scraper_with(FakeDriver(totals=[None]))._wait_for_results(timeout=0.5)
