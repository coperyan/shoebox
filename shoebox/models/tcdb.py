"""Models for the Trading Card Database (tcdb.com) automation.

``AdvancedSearchQuery`` mirrors the fields on https://www.tcdb.com/AdvancedSearch.cfm
one-to-one, so adding a new site field is a one-line change here plus an entry
in ``_FORM_FIELDS``. Everything the interactive session lets you tweak
("set year=1993") resolves to an attribute on this model.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import ClassVar
from urllib.parse import urlencode, urljoin

from pydantic import Field, field_validator

from shoebox.models.common import Model, utc_now

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


class CardSpec(Model):
    """A card written the way TCDB titles it: ``{year} {set} #{number} {name}``.

    e.g. ``2009 Bowman Chrome - X-Fractors #171 Matt Cain`` ->
    year ``2009``, set_name ``Bowman Chrome - X-Fractors``, card_number ``171``,
    name ``Matt Cain``. ``name`` may be empty (match on year/set/number only).
    """

    raw: str
    year: str
    set_name: str
    card_number: str
    name: str = ""

    @property
    def title(self) -> str:
        """The TCDB title this spec should match exactly."""
        base = f"{self.year} {self.set_name} #{self.card_number}"
        return f"{base} {self.name}" if self.name else base

    @property
    def search_name(self) -> str:
        """Name to put in the search form: the first player of a multi-player card."""
        return self.name.split(" / ")[0].strip()


class TcdbCardPage(Model):
    """The identifying bits of a ViewCard.cfm page."""

    url: str
    title: str
    set_id: int | None = None
    card_id: int | None = None


class CollectionWidget(Model):
    """Which collection controls the ViewCard page's ``#colDiv`` box offers.

    TCDB fills ``#colDiv`` from ``ViewCard_ajax.cfm`` after the page loads. The
    page script wires ``#quickAddBtn`` (add one copy), ``.addAnotherBtn`` (add
    one more copy) and ``.removeBtn`` (remove a copy); the last two only make
    sense for a card already in the collection.
    """

    loaded: bool = False
    quick_add: bool = False
    add_another: bool = False
    remove: bool = False
    # Copies owned, when the box says (its quantity badge or "You have N").
    quantity: int | None = None

    @property
    def owned(self) -> bool:
        return self.add_another or self.remove


class CollectionInspection(Model):
    """A ViewCard.cfm page and the state of its collection box, read without changing anything."""

    card: TcdbCardPage
    widget: CollectionWidget
    widget_html: str = ""


class CollectionAddResult(Model):
    """Outcome of one add-to-collection attempt on a ViewCard.cfm page."""

    # "added" | "already_owned" | "no_add_button" | "failed"
    status: str
    card: TcdbCardPage
    detail: str = ""
    # The CollectionAdd*_ajax.cfm call the page made, when one was made.
    request_url: str | None = None
    # True when the collection box re-rendered showing the card as owned.
    verified: bool = False
    # Inner HTML of #colDiv, kept for debugging when the add did not happen.
    widget_html: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "added"


class WantlistCard(Model):
    """One card on a member's want list (a ``Filter=W`` collection row)."""

    title: str
    url: str
    # Pulled apart from the title: "1986 Topps Traded - Limited Edition (Tiffany)
    # #11T Barry Bonds" -> 1986 / "1986 Topps Traded" / "Limited Edition
    # (Tiffany)" / "11T" / "Barry Bonds". A set with no subset gets "Base".
    set_year: str = ""
    set_name: str = ""
    subset_name: str = ""
    card_number: str = ""
    player: str = ""
    # Filled only when the export is asked for teams: it costs a page per card.
    team: str = ""
    # TCDB's short codes on the row, e.g. "RC", "VAR", "SN50", "XRC".
    notes: str = ""
    # The <figcaption> spelling out a code, when the row carries one.
    note_detail: str = ""
    set_id: int | None = None
    card_id: int | None = None
    # TCDB's id for this row of the collection, needed to edit or remove it.
    item_id: int | None = None
    quantity: int = 1
    # The amount in the row's price column, when it has one. TCDB uses this
    # column for the value you recorded against the card, so most rows are blank.
    price: float | None = None
    price_text: str = ""
    # The row icon's label, e.g. "Wantlist".
    status: str = ""
    category: str = ""


class WantlistPage(Model):
    """One page of a want list (100 rows) plus what the page says about the rest."""

    cards: list[WantlistCard] = Field(default_factory=list)
    query_url: str = ""
    category: str = ""
    page_index: int = 1
    total_records: int | None = None
    total_pages: int | None = None

    @property
    def has_more(self) -> bool:
        return bool(self.total_pages and self.page_index < self.total_pages)


class CollectionRemoveResult(Model):
    """Outcome of removing copies of a card from the collection on its ViewCard.cfm page."""

    # "removed" | "not_owned" | "failed"
    status: str
    card: TcdbCardPage
    copies_requested: int = 1
    copies_removed: int = 0
    # Whether the collection box still showed the card as owned afterwards
    # (None when the box never loaded).
    still_owned: bool | None = None
    detail: str = ""
    # The last CollectionRemove_ajax.cfm call the page made, when one was made.
    request_url: str | None = None
    # Inner HTML of #colDiv, kept for debugging when the remove did not happen.
    widget_html: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "removed" and self.copies_removed >= self.copies_requested


class SoldCard(Model):
    """One sold eBay line item (never a variation) and the checklist card its
    SKU resolves to. Built from ``configs/bigquery/queries/tcdb_sold_cards.sql``.

    ``matched`` is false when the SKU matched no checklist card; the card
    columns are then empty and only ``ebay_title`` says what sold.
    """

    order_line_key: str
    order_id: str = ""
    line_item_id: str = ""
    order_created_date: date
    ebay_title: str = ""
    sku: str = ""
    quantity: int = 1
    legacy_item_id: str = ""
    matched: bool = False
    set_year: str = ""
    # The checklist's set name, which already carries the year ("2020 Topps Chrome").
    set_name: str = ""
    subset_name: str = ""
    subset_type: str = ""
    parallel_variety: str = ""
    card_number: str = ""
    player: str = ""
    team: str = ""
    print_run: int | None = None

    @property
    def description(self) -> str:
        """The card as the checklist knows it, in TCDB's title shape:
        ``2020 Topps Chrome - Pink Refractor #1 Mike Trout``. The eBay title
        when the SKU matched nothing."""
        if not self.matched:
            return self.ebay_title
        parts = [self.set_name]
        if self.subset_name and self.subset_name.casefold() != "base":
            parts.append(self.subset_name)
        if self.parallel_variety:
            parts.append(self.parallel_variety)
        head = " - ".join(p for p in parts if p)
        return f"{head} #{self.card_number} {self.player}".strip()

    @property
    def search_name(self) -> str:
        """Name for the search form: the first player of a multi-player card."""
        return self.player.split(" / ")[0].strip()


class ReconcileEntry(Model):
    """One line of the reconcile ledger: what was decided about one sold line item.

    ``removed`` and ``ignored`` are final (the sale is never shown again);
    ``skipped`` and ``failed`` are not, so the sale comes back next run.
    """

    order_line_key: str
    order_id: str = ""
    line_item_id: str = ""
    order_created_date: date
    status: str  # removed | ignored | skipped | failed
    card: str = ""
    quantity: int = 1
    tcdb_title: str = ""
    tcdb_url: str = ""
    copies_removed: int = 0
    detail: str = ""
    at: datetime = Field(default_factory=utc_now)

    @property
    def final(self) -> bool:
        return self.status in RECONCILE_FINAL_STATUSES


RECONCILE_FINAL_STATUSES = frozenset({"removed", "ignored"})


class ReconcileState(Model):
    """Where the reconcile left off: every sale created on or before
    ``reconciled_through`` has been dealt with."""

    reconciled_through: date | None = None
    updated_at: datetime | None = None
