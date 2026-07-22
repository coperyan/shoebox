from shoebox.utils.pricing import (
    calculate_new_price,
    parse_price_reply,
    round_up_to_nine,
)


class TestParsePriceReply:
    def test_plain_number(self):
        assert parse_price_reply("4.99") == 4.99

    def test_dollar_sign_and_whitespace(self):
        assert parse_price_reply("  $4.99 ") == 4.99

    def test_thousands_separator(self):
        assert parse_price_reply("1,299.99") == 1299.99

    def test_non_numeric_returns_none(self):
        assert parse_price_reply("yes") is None
        assert parse_price_reply("Y") is None

    def test_empty_and_none(self):
        assert parse_price_reply("") is None
        assert parse_price_reply(None) is None

    def test_non_positive_returns_none(self):
        assert parse_price_reply("0") is None
        assert parse_price_reply("-5") is None


class TestRoundUpToNine:
    def test_rounds_up_to_nearest_nine(self):
        assert round_up_to_nine(1.23) == 1.29
        assert round_up_to_nine(4.44) == 4.49

    def test_already_on_nine_is_stable(self):
        assert round_up_to_nine(1.59) == 1.59
        assert round_up_to_nine(9.99) == 9.99


class TestCalculateNewPrice:
    def test_floor_tier_has_no_discount(self):
        assert calculate_new_price(0.99) == 0.99

    def test_mid_tier_discount(self):
        # 5.00 falls in the 5.49 tier -> $0.50 markdown
        assert calculate_new_price(5.00) == 4.50

    def test_top_tier_discount(self):
        # 19.99 tier -> $3.00 markdown
        assert calculate_new_price(19.99) == 16.99
