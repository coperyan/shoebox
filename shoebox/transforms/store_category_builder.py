"""Work out which eBay store categories a listing belongs in.

The store tree this builds toward:

    /Complete Your Set - You Pick
    /Baseball Singles/<team>
    /Basketball Singles
    /Football Singles
    /Hits/{Autographs,Relics,Numbered}      -- secondary, every sport

Only baseball is broken out by team: it is 93% of the shelf, so its 30 clubs
average ~50 listings each, while basketball (82 listings across 32 teams) and
football (41 across 22) would average two or three per team. Widening either is
a one-line change to :data:`TEAM_SUBCATEGORY_SPORTS`.

eBay allows a listing in **one or two** store categories, so a baseball hit
carries both its team category and its Hits category; everything else carries
one. See ``EbayOfferDetailsWithAll.store_category_names`` in ebay_rest.

This module only decides. Nothing here talks to eBay.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shoebox.utils.title_crosswalk import team_parents, team_sport

# Level-one branch holding the multi-variation "Complete Your Set" listings.
VARIATION_CATEGORY = "Complete Your Set - You Pick"

# Parent of the secondary categories that call out the good stuff.
HITS_PARENT = "Hits"

# Sports whose singles are broken out by team. See the module docstring.
TEAM_SUBCATEGORY_SPORTS = frozenset({"Baseball"})

# Sports that get the secondary Hits category. Kept separate from the above so
# the two can be widened independently -- Hits covers every sport, because a
# basketball case hit is worth calling out even though 82 basketball listings
# do not justify 32 team categories.
HITS_SPORTS = frozenset({"Baseball", "Basketball", "Football"})

# Branch for a listing whose sport we cannot establish from either the team or
# the Sport item specific.
FALLBACK_SPORT = "Other"

# eBay caps a listing at two store categories.
MAX_STORE_CATEGORIES = 2

# Title markers for a multi-variation listing. These are group listings with no
# single card behind them, so they are never given a team or a hit category.
_VARIATION_MARKERS = ("complete your set", "you pick")

_AUTOGRAPH_MARKERS = ("auto", "signature")
_RELIC_MARKERS = ("relic", "memorabilia", "patch")
_NUMBERED_MARKERS = ("serial numbered", "numbered")


def _text(value: object) -> str:
    """A trimmed string, treating any missing value as the empty string.

    Deliberately pandas-free so the transform stays importable and testable on
    its own; NaN is caught by its self-inequality rather than ``pd.isna``.
    """
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    return str(value).strip()


def is_variation_listing(title: object) -> bool:
    """True for a "Complete Your Set" / "You Pick" group listing."""
    lowered = _text(title).lower()
    return any(marker in lowered for marker in _VARIATION_MARKERS)


def hit_type(
    *,
    autographed: object = None,
    features: object = None,
    subset_name: object = None,
    print_run: object = None,
) -> str | None:
    """Which Hits category a card belongs in, or None for an ordinary card.

    Precedence matches the existing ``listing_builder.store_category``: an
    autographed relic is filed under Autographs, and a numbered parallel only
    counts as Numbered when it is neither.
    """
    auto = _text(autographed).lower()
    haystack = f"{_text(features)} {_text(subset_name)}".lower()

    if auto in ("yes", "true") or any(m in haystack for m in _AUTOGRAPH_MARKERS):
        return "Autographs"
    if any(m in haystack for m in _RELIC_MARKERS):
        return "Relics"
    if _text(print_run) or any(m in haystack for m in _NUMBERED_MARKERS):
        return "Numbered"
    return None


def resolve_sport(team: object = None, sport: object = None) -> tuple[str, bool]:
    """The sport branch for a listing, and whether it had to be guessed at.

    The team name wins over the Sport item specific: the two disagree on a
    small number of live listings -- basketball cards tagged Baseball, mostly --
    and the team is the more reliable of the pair. College teams imply no one
    sport, so they fall through to the item specific.
    """
    from_team = team_sport(_text(team) or None)
    if from_team:
        return from_team, True

    declared = _text(sport)
    if declared:
        return declared, True

    return FALLBACK_SPORT, False


def category_path(*segments: str) -> str:
    """Join category names into the ``/a/b`` path eBay expects."""
    return "".join(
        f"/{segment.strip()}" for segment in segments if segment and segment.strip()
    )


@dataclass(frozen=True)
class StoreCategoryPlan:
    """Where one listing should sit, and how that was decided."""

    categories: list[str]
    sport: str
    team: str | None = None
    hit: str | None = None
    is_variation: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def primary(self) -> str:
        return self.categories[0]

    @property
    def secondary(self) -> str | None:
        return self.categories[1] if len(self.categories) > 1 else None


def plan_store_category(
    *,
    title: object = None,
    team: object = None,
    sport: object = None,
    features: object = None,
    subset_name: object = None,
    print_run: object = None,
    autographed: object = None,
) -> StoreCategoryPlan:
    """Decide the store categories for a single listing.

    Every argument is optional and missing values are tolerated: this runs over
    a BigQuery export where any item specific may be null.
    """
    notes: list[str] = []

    if is_variation_listing(title):
        # A group listing spans every team in the set and has no one card to
        # call a hit, so it takes the You Pick branch and nothing else.
        return StoreCategoryPlan(
            categories=[category_path(VARIATION_CATEGORY)],
            sport=_text(sport) or FALLBACK_SPORT,
            is_variation=True,
            notes=["variation_listing"],
        )

    resolved_sport, confident = resolve_sport(team=team, sport=sport)
    if not confident:
        notes.append("sport_unresolved")

    team_name: str | None = None
    if resolved_sport in TEAM_SUBCATEGORY_SPORTS:
        parents = team_parents(team)
        team_name = parents[0]
        if len(parents) > 1:
            # Two-team cards are filed under the first; eBay's second slot is
            # spent on Hits, which is the more useful of the two.
            notes.append("multi_team")
        primary = category_path(f"{resolved_sport} Singles", team_name)
    else:
        primary = category_path(f"{resolved_sport} Singles")

    categories = [primary]

    hit = None
    if resolved_sport in HITS_SPORTS:
        hit = hit_type(
            autographed=autographed,
            features=features,
            subset_name=subset_name,
            print_run=print_run,
        )
        if hit:
            categories.append(category_path(HITS_PARENT, hit))

    if len(categories) > MAX_STORE_CATEGORIES:  # pragma: no cover - guard
        raise ValueError(f"eBay allows at most {MAX_STORE_CATEGORIES} store categories")

    return StoreCategoryPlan(
        categories=categories,
        sport=resolved_sport,
        team=team_name,
        hit=hit,
        notes=notes,
    )
