"""Trading Card Database (tcdb.com) automation."""

from .browser import LOGIN_URL, TcdbBrowser, TcdbLoginTimeout
from .card import (
    is_card_url,
    match_card,
    parse_card_page,
    parse_card_spec,
    parse_collection_widget,
)
from .search import is_challenge_page, is_logged_in, parse_results

__all__ = [
    "LOGIN_URL",
    "TcdbBrowser",
    "TcdbLoginTimeout",
    "is_card_url",
    "is_challenge_page",
    "is_logged_in",
    "match_card",
    "parse_card_page",
    "parse_card_spec",
    "parse_collection_widget",
    "parse_results",
]
