"""Saved eBay search definitions loaded from ``configs/searches.yaml``.

Three layers:

- ``SearchDefaults`` — inheritable settings with concrete defaults.
- ``SavedSearch`` — one search as written in YAML. Every inheritable field is
  ``None`` by default, where ``None`` means *inherit* and ``[]`` means
  *explicitly empty*. A plain ``dict.update`` merge would destroy that
  distinction, so the merge walks the fields explicitly.
- ``ResolvedSearch`` — post-merge, all values concrete. Cross-field rules that
  depend on merged values are validated here.

Validation is deliberately strict (``extra="forbid"``, no bare-int intervals):
a saved search that silently searches for the wrong thing is worse than one
that refuses to load.
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BeforeValidator, ConfigDict, Field, field_validator, model_validator

from .common import Model

# eBay Browse sort values (ebay_rest a_p_i.py:309). "bestMatch" is deliberately
# excluded: with Best Match ordering a search sees an arbitrary slice of a
# possibly huge result set, so genuinely new listings can stay invisible for
# days. Time ordering is what makes a *watcher* work.
SortValue = Literal["newlyListed", "endingSoonest", "price", "-price"]

BuyingOption = Literal["FIXED_PRICE", "AUCTION", "BEST_OFFER", "CLASSIFIED_AD"]
ConditionValue = Literal["NEW", "USED", "UNSPECIFIED"]

# Used as a filename for the per-search seen cache, so keep it path-safe.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Slack IDs: C=public channel, G=private group, D=DM.
_CHANNEL_ID_RE = re.compile(r"^[CGD][A-Z0-9]{6,}$")
_INTERVAL_RE = re.compile(r"^(\d+)([smhd])$")
_INTERVAL_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}

# Below this, a cron-driven watcher just burns Browse API quota.
MIN_INTERVAL = timedelta(seconds=60)

# eBay truncates the q parameter at 100 characters (a_p_i.py:308).
MAX_QUERY_CHARS = 100

# eBay caps on the seller filters.
MAX_SELLERS = 250
MAX_EXCLUDE_SELLERS = 100


def parse_interval(value: object) -> timedelta:
    """Parse ``"15m"``-style intervals into a timedelta.

    Bare numbers are rejected on purpose. Pydantic would happily coerce ``15``
    into 15 *seconds*, but in a config full of polling intervals ``15`` reads as
    minutes — so the ambiguity is worth an error rather than a guess.
    """
    if isinstance(value, timedelta):
        return value
    if not isinstance(value, str):
        raise ValueError(
            f"interval must be a string like '15m', got {type(value).__name__}. "
            "Bare numbers are rejected because the unit would be ambiguous."
        )

    match = _INTERVAL_RE.match(value.strip().lower())
    if not match:
        raise ValueError(
            f"invalid interval {value!r}; expected <number><unit> where unit is "
            "s, m, h or d (e.g. '90s', '15m', '2h', '1d')"
        )

    amount, unit = int(match.group(1)), match.group(2)
    delta = timedelta(**{_INTERVAL_UNITS[unit]: amount})
    if delta < MIN_INTERVAL:
        raise ValueError(
            f"interval {value!r} is below the {int(MIN_INTERVAL.total_seconds())}s minimum"
        )
    return delta


Interval = Annotated[timedelta, BeforeValidator(parse_interval)]


class _ConfigModel(Model):
    """Base for everything read out of searches.yaml.

    Overrides only ``extra``; pydantic merges the rest of the parent's
    ConfigDict, so ``str_strip_whitespace`` and friends still apply. Unknown
    keys are rejected so a typo fails at load instead of being ignored.
    """

    model_config = ConfigDict(extra="forbid")


class PriceRange(_ConfigModel):
    min: Decimal | None = None
    max: Decimal | None = None

    @model_validator(mode="after")
    def _check_bounds(self) -> PriceRange:
        if self.min is None and self.max is None:
            raise ValueError("price needs at least one of 'min' or 'max'")
        for label, bound in (("min", self.min), ("max", self.max)):
            if bound is not None and bound < 0:
                raise ValueError(f"price.{label} must not be negative")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"price.min ({self.min}) must not exceed price.max ({self.max})")
        return self


class SearchDefaults(_ConfigModel):
    """Inheritable defaults. Scalars and flat lists only — no nested ``filters``
    block, because deep-merge semantics are ambiguous and every reader would
    guess differently."""

    enabled: bool = True
    interval: Interval = timedelta(minutes=30)
    sort: SortValue = "newlyListed"
    max_results: int = Field(default=200, ge=1)
    # The first run seeds from a much larger window: anything matching but not
    # seeded leaks out later as a false "new listing".
    seed_max_results: int = Field(default=2000, ge=1)
    # Slack allows ~1 message/sec/channel, so this is a time budget as much as a
    # noise budget. Overflow is summarized, never silently dropped.
    max_notify: int = Field(default=10, ge=1)
    channel: str | None = None

    currency: str = "USD"
    # eBay returns ONLY FIXED_PRICE listings unless buyingOptions is set
    # (a_p_i.py:295), so this is always emitted. Dropping AUCTION here means
    # never seeing an auction again.
    buying_options: list[BuyingOption] = Field(default_factory=lambda: ["FIXED_PRICE", "AUCTION"])
    conditions: list[ConditionValue] = Field(default_factory=list)
    item_location_countries: list[str] = Field(default_factory=lambda: ["US"])
    delivery_country: str | None = "US"
    sellers: list[str] = Field(default_factory=list)
    exclude_sellers: list[str] = Field(default_factory=list)
    free_shipping_only: bool = False

    # Post-filters (no eBay server-side equivalent — see transforms/search_filters.py)
    title_exclude: list[str] = Field(default_factory=list)
    title_must_include_all: list[str] = Field(default_factory=list)
    title_must_include_any: list[str] = Field(default_factory=list)
    seller_min_feedback_score: int | None = None
    max_total_price: Decimal | None = None

    prune_seen_after_days: int = Field(default=90, ge=1)


# Every field a search may inherit from defaults.
_INHERITABLE = tuple(SearchDefaults.model_fields)


class SavedSearch(_ConfigModel):
    """One entry under ``searches:``. ``None`` means inherit from defaults."""

    name: str
    query: str | None = None
    category_ids: list[str] = Field(default_factory=list)
    price: PriceRange | None = None
    aspects: dict[str, list[str]] = Field(default_factory=dict)

    enabled: bool | None = None
    interval: Interval | None = None
    sort: SortValue | None = None
    max_results: int | None = Field(default=None, ge=1)
    seed_max_results: int | None = Field(default=None, ge=1)
    max_notify: int | None = Field(default=None, ge=1)
    channel: str | None = None

    currency: str | None = None
    buying_options: list[BuyingOption] | None = None
    conditions: list[ConditionValue] | None = None
    item_location_countries: list[str] | None = None
    delivery_country: str | None = None
    sellers: list[str] | None = None
    exclude_sellers: list[str] | None = None
    free_shipping_only: bool | None = None

    title_exclude: list[str] | None = None
    title_must_include_all: list[str] | None = None
    title_must_include_any: list[str] | None = None
    seller_min_feedback_score: int | None = None
    max_total_price: Decimal | None = None

    prune_seen_after_days: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                f"invalid search name {v!r}: use lowercase letters, digits, '_' or '-' "
                "(max 64 chars, must start alphanumeric). The name is used as a "
                "filename for the seen-cache and as part of the dedup key."
            )
        return v

    @field_validator("query")
    @classmethod
    def _check_query(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if len(v) > MAX_QUERY_CHARS:
            raise ValueError(f"query is {len(v)} characters; eBay truncates q at {MAX_QUERY_CHARS}")
        if "*" in v:
            raise ValueError("eBay rejects the '*' wildcard in q")
        return v

    @field_validator("category_ids")
    @classmethod
    def _check_category_ids(cls, v: list[str]) -> list[str]:
        if len(v) > 1:
            raise ValueError(
                "eBay Browse accepts only one category ID per request "
                f"(got {len(v)}); split this into separate searches"
            )
        return v


class ResolvedSearch(_ConfigModel):
    """A search with defaults merged in. All values concrete."""

    name: str
    query: str | None = None
    category_ids: list[str] = Field(default_factory=list)
    price: PriceRange | None = None
    aspects: dict[str, list[str]] = Field(default_factory=dict)

    enabled: bool
    interval: timedelta
    sort: SortValue
    max_results: int
    seed_max_results: int
    max_notify: int
    channel: str | None

    currency: str
    buying_options: list[BuyingOption]
    conditions: list[ConditionValue]
    item_location_countries: list[str]
    delivery_country: str | None
    sellers: list[str]
    exclude_sellers: list[str]
    free_shipping_only: bool

    title_exclude: list[str]
    title_must_include_all: list[str]
    title_must_include_any: list[str]
    seller_min_feedback_score: int | None
    max_total_price: Decimal | None

    prune_seen_after_days: int

    @model_validator(mode="after")
    def _check_resolved(self) -> ResolvedSearch:
        where = f"search {self.name!r}"

        if not self.query and not self.category_ids:
            raise ValueError(f"{where}: needs at least one of 'query' or 'category_ids'")

        if self.aspects and len(self.category_ids) != 1:
            raise ValueError(
                f"{where}: 'aspects' requires exactly one category_id — eBay's "
                "aspect_filter must repeat the category ID inside the filter string"
            )
        for key, values in self.aspects.items():
            if "," in key or any("," in v for v in values):
                raise ValueError(
                    f"{where}: aspect {key!r} contains a comma; eBay documents no "
                    "escape for ',' inside aspect_filter"
                )
            if not values:
                raise ValueError(f"{where}: aspect {key!r} has no values")

        if not self.buying_options:
            raise ValueError(
                f"{where}: 'buying_options' must not be empty — eBay returns only "
                "FIXED_PRICE listings when the filter is absent, so an empty list "
                "would silently hide every auction"
            )

        if len(self.sellers) > MAX_SELLERS:
            raise ValueError(f"{where}: at most {MAX_SELLERS} entries in 'sellers'")
        if len(self.exclude_sellers) > MAX_EXCLUDE_SELLERS:
            raise ValueError(f"{where}: at most {MAX_EXCLUDE_SELLERS} entries in 'exclude_sellers'")
        if self.sellers and self.exclude_sellers:
            raise ValueError(f"{where}: 'sellers' and 'exclude_sellers' are mutually exclusive")

        if self.seed_max_results < self.max_results:
            raise ValueError(
                f"{where}: seed_max_results ({self.seed_max_results}) must be >= "
                f"max_results ({self.max_results})"
            )
        return self


class SearchesFile(_ConfigModel):
    """The whole ``configs/searches.yaml`` document."""

    version: Literal[1] = 1
    # Optional alias -> Slack channel ID map, so a channel is named once and
    # referenced by a readable alias everywhere else.
    channels: dict[str, str] = Field(default_factory=dict)
    defaults: SearchDefaults = Field(default_factory=SearchDefaults)
    searches: list[SavedSearch] = Field(default_factory=list)

    @field_validator("channels")
    @classmethod
    def _check_channel_map(cls, v: dict[str, str]) -> dict[str, str]:
        for alias, channel_id in v.items():
            if _CHANNEL_ID_RE.match(alias):
                raise ValueError(
                    f"channel alias {alias!r} looks like a Slack channel ID; the alias is "
                    "the readable name and the value is the ID (e.g. card_alerts: C0123ABCD)"
                )
            if not _CHANNEL_ID_RE.match(channel_id):
                raise ValueError(
                    f"channels.{alias}: {channel_id!r} is not a Slack channel ID. IDs start "
                    "with C, G or D (channel details -> copy the ID at the bottom), not '#name'"
                )
        return v

    def resolve_channel(self, value: str | None) -> str | None:
        """Turn an alias into a channel ID, passing raw IDs through.

        A value that is neither a known alias nor ID-shaped is a typo, and
        saying so at load time beats a ``channel_not_found`` from Slack at 3am.
        """
        if value is None:
            return None
        if value in self.channels:
            return self.channels[value]
        if _CHANNEL_ID_RE.match(value):
            return value
        known = ", ".join(sorted(self.channels)) or "(none defined)"
        raise ValueError(
            f"unknown channel alias {value!r}. Defined aliases: {known}. "
            "Use an alias from the 'channels:' block, or a literal Slack channel ID."
        )

    @model_validator(mode="after")
    def _validate_document(self) -> SearchesFile:
        seen: dict[str, int] = {}
        for i, search in enumerate(self.searches):
            if search.name in seen:
                raise ValueError(
                    f"duplicate search name {search.name!r} at indexes {seen[search.name]} and {i}"
                )
            seen[search.name] = i

        # Force the merge so ResolvedSearch's cross-field rules run now. Several
        # rules (query-or-category, non-empty buying_options, aspect/category
        # pairing) can only be checked post-merge, and a config error must
        # surface when the file loads -- not later, mid-run, per search.
        self.resolved()
        return self

    def resolved(self) -> list[ResolvedSearch]:
        """Merge defaults into each search."""
        out: list[ResolvedSearch] = []
        for search in self.searches:
            merged: dict[str, Any] = {
                "name": search.name,
                "query": search.query,
                "category_ids": search.category_ids,
                "price": search.price,
                "aspects": search.aspects,
            }
            for field in _INHERITABLE:
                value = getattr(search, field)
                merged[field] = getattr(self.defaults, field) if value is None else value
            # Aliases are resolved here so ResolvedSearch.channel is always a
            # concrete ID -- nothing downstream needs to know aliases exist.
            merged["channel"] = self.resolve_channel(merged["channel"])
            out.append(ResolvedSearch(**merged))
        return out

    def enabled_searches(self) -> list[ResolvedSearch]:
        return [s for s in self.resolved() if s.enabled]


def load_searches_file(path: str | Path) -> SearchesFile:
    """Load and validate a searches YAML document.

    Mirrors ``settings.get_settings`` but is deliberately *not* cached — a
    long-running process should pick up edits to the file.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Missing searches file: {path}. "
            "Create it from configs/searches.example.yml "
            "(or point paths.searches_file at another location)."
        )

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return SearchesFile(**raw)
