from datetime import timedelta

from shoebox.models.ebay.item_summary import ItemSummary
from shoebox.models.saved_search import SearchesFile
from shoebox.utils.search_formatting import (
    IMAGE_SIZE_PX,
    MAX_TITLE_CHARS,
    build_item_message,
    format_interval,
    format_item,
    format_overflow,
    format_parent,
    format_run_summary,
    format_seed,
    truncate_title,
)

THUMB = "https://i.ebayimg.com/images/g/abc/s-l225.jpg"


def resolve(**overrides):
    search = {"name": "s1", "query": "jordan"}
    search.update(overrides)
    return SearchesFile(version=1, searches=[search]).resolved()[0]


def item(**overrides) -> ItemSummary:
    data = {
        "item_id": "v1|123|0",
        "title": "1986 Fleer Michael Jordan Rookie",
        "price": {"value": "124.99", "currency": "USD"},
        "buying_options": ["FIXED_PRICE"],
        "item_web_url": "https://ebay.com/itm/123",
        "condition": "Used",
        "seller": {"username": "coolcards", "feedback_score": 4812},
    }
    data.update(overrides)
    return ItemSummary(**data)


class TestTruncateTitle:
    def test_short_title_untouched(self):
        assert truncate_title("short") == "short"

    def test_long_title_gets_ellipsis_within_limit(self):
        out = truncate_title("x" * 200)
        assert len(out) <= MAX_TITLE_CHARS
        assert out.endswith("…")

    def test_exact_limit_untouched(self):
        exact = "x" * MAX_TITLE_CHARS
        assert truncate_title(exact) == exact


class TestFormatInterval:
    def test_units(self):
        assert format_interval(timedelta(minutes=15)) == "15m"
        assert format_interval(timedelta(hours=2)) == "2h"
        assert format_interval(timedelta(days=1)) == "1d"
        assert format_interval(timedelta(seconds=90)) == "90s"


class TestFormatParent:
    def test_singular_and_plural(self):
        assert "1 new listing\n" in format_parent(resolve(), 1)
        assert "4 new listings\n" in format_parent(resolve(), 4)

    def test_includes_name_and_interval(self):
        out = format_parent(resolve(interval="15m"), 2)
        assert "*🔎 s1*" in out
        assert "every 15m" in out

    def test_price_range_rendered(self):
        out = format_parent(resolve(price={"min": 25, "max": 200}), 1)
        assert "$25.00–$200.00" in out

    def test_price_max_only_reads_as_under(self):
        assert "under $50.00" in format_parent(resolve(price={"max": 50}), 1)

    def test_buying_option_label(self):
        assert "Buy It Now" in format_parent(resolve(buying_options=["FIXED_PRICE"]), 1)
        assert "Auction" in format_parent(resolve(buying_options=["AUCTION"]), 1)


class TestFormatItem:
    def test_title_is_a_clickable_link(self):
        out = format_item(item(), resolve())
        assert "*<https://ebay.com/itm/123|1986 Fleer Michael Jordan Rookie>*" in out

    def test_bare_url_on_its_own_line_for_unfurling(self):
        assert format_item(item(), resolve()).splitlines()[-1] == "https://ebay.com/itm/123"

    def test_bare_url_suppressed_when_asked(self):
        out = format_item(item(), resolve(), include_bare_url=False)
        assert out.splitlines()[-1] != "https://ebay.com/itm/123"
        # The link survives in the headline; only the duplicate is gone.
        assert "https://ebay.com/itm/123|" in out

    def test_no_code_fence(self):
        # A fenced block would kill both the link and the unfurl.
        assert "```" not in format_item(item(), resolve())

    def test_price_and_buying_option(self):
        out = format_item(item(), resolve())
        assert "$124.99" in out
        assert "Buy It Now" in out

    def test_auction_shows_current_bid(self):
        out = format_item(
            item(
                buying_options=["AUCTION"],
                current_bid_price={"value": "45.00", "currency": "USD"},
            ),
            resolve(),
        )
        assert "$45.00 (bid)" in out
        assert "Auction" in out

    def test_free_shipping_label(self):
        out = format_item(
            item(shipping_options=[{"shipping_cost": {"value": "0.00", "currency": "USD"}}]),
            resolve(),
        )
        assert "Free shipping" in out

    def test_paid_shipping_shows_amount(self):
        out = format_item(
            item(shipping_options=[{"shipping_cost": {"value": "4.50", "currency": "USD"}}]),
            resolve(),
        )
        assert "+$4.50 ship" in out

    def test_seller_and_condition(self):
        out = format_item(item(), resolve())
        assert "`coolcards` (4,812)" in out
        assert "Used" in out

    def test_angle_brackets_escaped_so_link_survives(self):
        out = format_item(item(title="Jordan <RARE> Card"), resolve())
        assert "&lt;RARE&gt;" in out

    def test_missing_price_is_explicit(self):
        assert "price unknown" in format_item(item(price=None), resolve())

    def test_missing_url_still_renders(self):
        out = format_item(item(item_web_url=None), resolve())
        assert "1986 Fleer" in out
        assert "<" not in out


class TestBuildItemMessage:
    def test_photo_becomes_an_image_block_below_the_text(self):
        msg = build_item_message(item(thumbnail_images=[{"image_url": THUMB}]), resolve())
        assert [b["type"] for b in msg.blocks] == ["section", "image"]
        assert msg.blocks[0]["text"]["text"].startswith("*<https://ebay.com/itm/123|")

    def test_image_is_upscaled_from_the_default_thumbnail(self):
        msg = build_item_message(item(thumbnail_images=[{"image_url": THUMB}]), resolve())
        assert msg.blocks[1]["image_url"].endswith(f"/s-l{IMAGE_SIZE_PX}.jpg")

    def test_unfurling_off_once_the_photo_is_explicit(self):
        msg = build_item_message(item(thumbnail_images=[{"image_url": THUMB}]), resolve())
        assert msg.unfurl_links is False
        assert msg.text.splitlines()[-1] != "https://ebay.com/itm/123"

    def test_alt_text_is_the_title(self):
        msg = build_item_message(item(thumbnail_images=[{"image_url": THUMB}]), resolve())
        assert msg.blocks[1]["alt_text"] == "1986 Fleer Michael Jordan Rookie"

    def test_long_title_alt_text_is_bounded(self):
        msg = build_item_message(
            item(title="x" * 500, thumbnail_images=[{"image_url": THUMB}]), resolve()
        )
        assert len(msg.blocks[1]["alt_text"]) <= 200

    def test_image_field_used_when_thumbnails_absent(self):
        msg = build_item_message(item(image={"image_url": THUMB}), resolve())
        assert msg.blocks[1]["type"] == "image"

    def test_no_photo_falls_back_to_unfurling(self):
        msg = build_item_message(item(), resolve())
        assert msg.blocks is None
        assert msg.unfurl_links is True
        # The bare URL has to be there or the fallback has nothing to unfurl.
        assert msg.text.splitlines()[-1] == "https://ebay.com/itm/123"

    def test_untitled_photo_still_gets_alt_text(self):
        msg = build_item_message(
            item(title=None, thumbnail_images=[{"image_url": THUMB}]), resolve()
        )
        assert msg.blocks[1]["alt_text"] == "listing photo"


class TestFormatOverflow:
    def test_counts_are_exact(self):
        out = format_overflow(total_new=12, shown=5, search=resolve(max_notify=5))
        assert "+7 more new listings" in out
        assert "max_notify=5" in out
        assert "All 12" in out

    def test_singular(self):
        assert "+1 more new listing " in format_overflow(6, 5, resolve(max_notify=5))


class TestFormatSeedAndSummary:
    def test_seed_mentions_count_and_that_it_is_not_an_alert(self):
        out = format_seed(resolve(interval="15m"), 312)
        assert "312 existing listing(s)" in out
        assert "new ones only" in out

    def test_run_summary_lists_each_failure(self):
        out = format_run_summary([("a", "boom"), ("b", "kaput")])
        assert "2 search(es) failed" in out
        assert "`a` — boom" in out
        assert "`b` — kaput" in out
