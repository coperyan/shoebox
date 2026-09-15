"""Models for the Trading Card Database (tcdb.com) automation.

``AdvancedSearchQuery`` mirrors the fields on https://www.tcdb.com/AdvancedSearch.cfm
one-to-one, so adding a new site field is a one-line change here plus an entry
in ``_FORM_FIELDS``. Everything the interactive session lets you tweak
("set year=1993") resolves to an attribute on this model.
"""

from __future__ import annotations

import re
from typing import ClassVar
from urllib.parse import urlencode, urljoin

from pydantic import Field, field_validator

from shoebox.models.common import Model

TCDB_BASE_URL = "https://www.tcdb.com"

# Values of the "Category" <select> on the advanced search form.
CATEGORIES: tuple[str, ...] = (
    "Baseball",
    "Basketball",
    "Boxing",
    "Cricket",
    "Football",
    "Gaming",
    "Golf",
    "Hockey",
    "Misc Sports",
    "MMA",
    "Multi-Sport",
    "Non-Sport",
    "Racing",
    "Soccer",
    "Tennis",
    "Wrestling",
)

# "Set Types" <select>: form value -> label. The form value is what TCDB wants;
# the label is accepted as input for convenience ("set set_type=Minor League").
SET_TYPES: dict[str, str] = {
    "": "Any",
    "Y": "Major Release",
    "C": "College",
    "CSL": "College Summer League",
    "D": "Draft",
    "J": "Japanese League",
    "KOR": "Korean League",
    "M": "Minor League",
    "TAI": "Taiwanese League",
    "BOX": "Box Set",
    "FOD": "Food Issue",
    "O": "Oddball",
    "ODM": "On-Demand",
    "POS": "Postcard",
    "R": "Promo",
    "REP": "Reprint",
    "SGA": "Stadium Giveaway (SGA)",
    "TI": "Team Issue",
    "TPH": "Team Photo",
    "T": "Team Set",
    "TST": "Test Issue",
    "UNL": "Unlicensed",
}


def normalize_category(value: str) -> str:
    """Map user input to the exact <option> value TCDB expects (case-insensitive)."""
    wanted = re.sub(r"[\s_-]+", "", value or "").casefold()
    for cat in CATEGORIES:
        if re.sub(r"[\s_-]+", "", cat).casefold() == wanted:
            return cat
    raise ValueError(f"Unknown TCDB category {value!r}. Choose one of: {', '.join(CATEGORIES)}")


def normalize_set_type(value: str) -> str:
    """Accept a set-type code ("M") or label ("Minor League"); return the code."""
    raw = (value or "").strip()
    if raw.casefold() in ("", "any"):
        return ""
    for code, label in SET_TYPES.items():
        if raw.casefold() in (code.casefold(), label.casefold()):
            return code
    choices = ", ".join(f"{c}={label}" for c, label in SET_TYPES.items() if c)
    raise ValueError(f"Unknown TCDB set type {value!r}. Choose one of: {choices}")


class AdvancedSearchQuery(Model):
    """One advanced-search request. Empty strings mean "leave the field blank"."""

    category: str = "Baseball"
    year: str = ""
    set_name: str = ""
    set_type: str = ""
    card_number: str = ""
    name: str = ""
    team: str = ""
    note: str = ""

    # Model attribute -> form field name on AdvancedSearch.cfm.
    _FORM_FIELDS: ClassVar[dict[str, str]] = {
        "category": "Type",
        "year": "Year",
        "set_name": "SetName",
        "set_type": "SetType",
        "card_number": "CardNum",
        "name": "Name",
        "team": "Team",
        "note": "Note",
    }

    @field_validator("category")
    @classmethod
    def _category(cls, v: str) -> str:
        return normalize_category(v)

    @field_validator("set_type")
    @classmethod
    def _set_type(cls, v: str) -> str:
        return normalize_set_type(v)

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        """User-facing field names, in form order (for help text and ``set``)."""
        return tuple(cls._FORM_FIELDS)

    def to_params(self) -> dict[str, str]:
        """Query-string parameters for ViewResults.cfm (GET works despite the POST form)."""
        params = {"MODE": "ADVANCED"}
        for attr, form_name in self._FORM_FIELDS.items():
            params[form_name] = str(getattr(self, attr) or "")
        return params

    def to_url(self, base_url: str = TCDB_BASE_URL) -> str:
        return urljoin(base_url, "/ViewResults.cfm") + "?" + urlencode(self.to_params())

    def with_updates(self, **changes: str) -> AdvancedSearchQuery:
        """Return a copy with the given fields replaced (validated)."""
        unknown = set(changes) - set(self._FORM_FIELDS)
        if unknown:
            raise ValueError(
                f"Unknown search field(s) {sorted(unknown)}; "
                f"valid fields: {', '.join(self._FORM_FIELDS)}"
            )
        return AdvancedSearchQuery(**{**self.model_dump(), **changes})

    def describe(self) -> str:
        """Human-readable one-liner of the non-empty criteria."""
        parts = [f"{k}={v!r}" for k, v in self.model_dump().items() if v]
        return ", ".join(parts) or "(no criteria)"


class TcdbSearchResult(Model):
    """One row of a ViewResults.cfm result table."""

    index: int
    title: str
    url: str
    set_id: int | None = None
    card_id: int | None = None
    thumb_url: str | None = None
    # Grey sub-line under the title, e.g. the original set of a buyback card.
    note: str | None = None


class TcdbSearchPage(Model):
    """Parsed results page: the rows plus the site's own result count."""

    query_url: str
    total_results: int | None = None
    results: list[TcdbSearchResult] = Field(default_factory=list)

    @property
    def truncated(self) -> bool:
        """True when TCDB reports more matches than rows we parsed."""
        return self.total_results is not None and self.total_results > len(self.results)
