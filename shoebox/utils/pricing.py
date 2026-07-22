from __future__ import annotations

import math


def parse_price_reply(reply: str | None) -> float | None:
    """Parse a human price reply from Slack into a positive float.

    Tolerates '$4.99', ' 4.99 ', '1,299.99' (comma = thousands separator).
    Returns None for anything that isn't a positive price ('no', 'Y', '', None),
    so callers can decide whether to keep the proposed price or skip.
    """
    if reply is None:
        return None
    cleaned = reply.strip().lstrip("$").replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


def round_up_to_nine(value: float) -> float:
    """Round value up to the nearest x.x9 price point (e.g. 1.56 -> 1.59)."""
    value = round(float(value), 2)
    shifted = value * 100
    # Subtract 0.01 so an already-.9 value stays in its bracket before ceil.
    rounded = math.ceil((shifted - 0.01) / 10) * 10 - 1
    return rounded / 100


_DISCOUNT_MATRIX = [
    {"value": 0.99, "discount": 0},
    {"value": 1.49, "discount": 0.1},
    {"value": 1.99, "discount": 0.2},
    {"value": 2.69, "discount": 0.3},
    {"value": 2.99, "discount": 0.4},
    {"value": 5.49, "discount": 0.5},
    {"value": 6.99, "discount": 0.7},
    {"value": 9.49, "discount": 1},
    {"value": 12.99, "discount": 1.5},
    {"value": 14.99, "discount": 2},
    {"value": 16.99, "discount": 2.5},
    {"value": 19.99, "discount": 3},
]


def calculate_new_price(orig_price: float) -> float:
    """Apply the standard markdown discount for a given price tier."""
    value_tier = min(x["value"] for x in _DISCOUNT_MATRIX if orig_price <= x["value"])
    discount = next(x["discount"] for x in _DISCOUNT_MATRIX if x["value"] == value_tier)
    return round(orig_price - discount, 2)
