"""Trading Card Database (tcdb.com) automation."""

from .browser import LOGIN_URL, TcdbBrowser, TcdbLoginTimeout
from .card import (
    is_card_url,
    match_card,
    parse_card_page,
    parse_card_spec,
    parse_collection_widget,
)
from .collection import (
    BASE_SUBSET,
    FILTER_HAVE,
    FILTER_WANT,
    parse_card_team,
    parse_member,
    parse_title_fields,
    parse_wantlist_page,
    wantlist_url,
)
from .search import is_challenge_page, is_logged_in, parse_results

__all__ = [
    "BASE_SUBSET",
    "FILTER_HAVE",
    "FILTER_WANT",
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
    "parse_card_team",
    "parse_member",
    "parse_title_fields",
    "parse_results",
    "parse_wantlist_page",
    "wantlist_url",
]
