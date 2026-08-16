"""Lookups keyed off the eBay ``Team`` item specific, plus token expansions.

Covers listing-title text (team short names, shorthand expansion) and eBay
store category routing (:func:`team_parent`, :func:`team_sport`).

The data lives in ``configs/title_crosswalk.yaml`` so teams can be added or
renamed without a code change. Everything here is cached after the first read;
call :func:`reload_crosswalk` in a REPL after editing the file.

Resolution order for the file:
  1. ``$SHOEBOX_TITLE_CROSSWALK``
  2. ``configs/title_crosswalk.yaml`` under the current working directory
  3. ``configs/title_crosswalk.yaml`` next to the installed package
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import yaml

_ENV_VAR = "SHOEBOX_TITLE_CROSSWALK"
_RELATIVE_PATH = Path("configs/title_crosswalk.yaml")

# Cards shared by two teams arrive as a list in item specifics, and flattened
# with one of these separators once they land in a dataframe or BigQuery.
_TEAM_SEPARATORS = re.compile(r"\s*[|;]\s*|\s+/\s+")

# Trailing words too generic to treat as "this team is already in the title".
# Without these, a team like "National League" would match any title mentioning
# a league leaders insert.
_GENERIC_ALIAS_WORDS = {"league", "team", "game", "club", "national", "american"}


def crosswalk_path() -> Path:
    """Path the crosswalk is read from."""
    env = os.getenv(_ENV_VAR)
    if env:
        return Path(env)
    cwd_path = Path.cwd() / _RELATIVE_PATH
    if cwd_path.exists():
        return cwd_path
    return Path(__file__).resolve().parents[2] / _RELATIVE_PATH


def _normalize(name: str) -> str:
    """Casefold and collapse whitespace so lookups tolerate messy input."""
    return re.sub(r"\s+", " ", name).strip().casefold()


@lru_cache(maxsize=1)
def _crosswalk() -> dict:
    path = crosswalk_path()
    if not path.exists():
        raise FileNotFoundError(
            f"Missing title crosswalk: {path}. "
            f"Ship configs/title_crosswalk.yaml with the app or set ${_ENV_VAR}."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    store = raw.get("store_categories") or {}
    group_sports = {
        str(group): (str(sport) if sport else None)
        for group, sport in (store.get("group_sports") or {}).items()
    }

    teams: dict[str, str] = {}
    display: dict[str, str] = {}
    team_sports: dict[str, str | None] = {}
    for group_name, group in (raw.get("teams") or {}).items():
        sport = group_sports.get(str(group_name))
        for long_name, short_name in (group or {}).items():
            if not short_name:
                continue
            key = _normalize(long_name)
            teams[key] = str(short_name)
            display[key] = str(long_name)
            team_sports[key] = sport

    # Only exceptions are configured; every other team parents to its short
    # name, which already folds most alternate spellings together.
    parents: dict[str, str] = {}
    for long_name, parent in (store.get("team_parents") or {}).items():
        if parent:
            parents[_normalize(long_name)] = str(parent)

    unmapped_team = str(store.get("unmapped_team") or "Other Teams")

    tokens: dict[str, str | None] = {}
    for token, expansion in (raw.get("token_expansions") or {}).items():
        tokens[str(token).upper()] = str(expansion) if expansion else None

    removals = [str(t).upper() for t in (raw.get("token_removals") or [])]

    synonyms: dict[str, tuple[str, ...]] = {}
    for word, alternatives in (raw.get("token_synonyms") or {}).items():
        synonyms[str(word)] = tuple(str(a) for a in (alternatives or []))

    phrases = [str(p) for p in (raw.get("title_removals") or []) if str(p).strip()]

    return {
        "teams": teams,
        "display": display,
        "team_sports": team_sports,
        "team_parents": parents,
        "unmapped_team": unmapped_team,
        "tokens": tokens,
        "token_removals": removals,
        "token_synonyms": synonyms,
        "title_removals": phrases,
    }


def reload_crosswalk() -> None:
    """Drop the cached crosswalk so the next lookup re-reads the YAML."""
    _crosswalk.cache_clear()
    team_aliases.cache_clear()


def shorten_team_name(long_name: str | None) -> str | None:
    """Short title form of an eBay ``Team`` item specific, or None if unmapped.

    Unmapped teams deliberately return None: the title leaves the team out
    rather than guessing at an abbreviation.
    """
    if not long_name:
        return None
    return _crosswalk()["teams"].get(_normalize(long_name))


@lru_cache(maxsize=512)
def team_aliases(long_name: str | None) -> tuple[str, ...]:
    """Every spelling that means "this title already names the team".

    Covers the short name, the full eBay value, and its trailing one- and
    two-word nicknames -- so a title reading "Red Sox" or "Diamondbacks"
    counts as already tagged for "Boston Red Sox" / "Arizona Diamondbacks".
    """
    if not long_name:
        return ()

    candidates: list[str] = [long_name.strip()]
    short = shorten_team_name(long_name)
    if short:
        candidates.append(short)

    words = long_name.split()
    for take in (1, 2):
        if len(words) > take:
            suffix = " ".join(words[-take:])
            if suffix.casefold() not in _GENERIC_ALIAS_WORDS:
                candidates.append(suffix)

    seen: set[str] = set()
    out: list[str] = []
    for candidate in candidates:
        key = _normalize(candidate)
        if key and key not in seen:
            seen.add(key)
            out.append(candidate)
    return tuple(out)


def split_team_values(value: str | Iterable[str] | None) -> list[str]:
    """Normalize a Team item specific into a list of individual team names.

    Accepts what each source actually hands over: a list from the eBay API, a
    ``"Detroit Tigers | Houston Astros"`` string from a dataframe or BigQuery,
    or a plain single name.
    """
    if value is None:
        return []
    if isinstance(value, str):
        raw = [value]
    else:
        raw = [str(v) for v in value]

    out: list[str] = []
    for item in raw:
        for part in _TEAM_SEPARATORS.split(item):
            part = part.strip()
            if part:
                out.append(part)
    return out


def shorten_team_names(value: str | Iterable[str] | None) -> list[str]:
    """Distinct short names for a Team item specific, in the order given.

    Two-team cards usually collapse to one name -- "Brooklyn Dodgers |
    Los Angeles Dodgers" is just "Dodgers" -- which is why this dedupes rather
    than returning one short name per input value.
    """
    out: list[str] = []
    for name in split_team_values(value):
        short = shorten_team_name(name)
        if short and short not in out:
            out.append(short)
    return out


def all_team_aliases(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Aliases for every team named in a Team item specific."""
    out: list[str] = []
    seen: set[str] = set()
    for name in split_team_values(value):
        for alias in team_aliases(name):
            key = _normalize(alias)
            if key not in seen:
                seen.add(key)
                out.append(alias)
    return tuple(out)


def unmapped_team() -> str:
    """Store category for teams with no usable home of their own."""
    return _crosswalk()["unmapped_team"]


def team_parent(long_name: str | None) -> str:
    """Store category a team's cards belong under. Always returns a category.

    Folds alternate, historical and minor-league names into the one category a
    buyer would look under: "Cleveland Indians" and "Cleveland" both land on
    "Cleveland Guardians", "Jupiter Hammerheads" on "Miami Marlins". Teams with
    no explicit parent keep their own name; anything unresolved -- an unknown
    team, a blank Team specific -- falls back to :func:`unmapped_team`.

    Categories carry the full city name, where :func:`shorten_team_name` gives
    the short form titles use. Full names also keep leagues apart: "Cardinals"
    is two clubs, "Arizona Cardinals" and "St. Louis Cardinals" are not.

    Unlike :func:`shorten_team_name`, this never returns None. A title can omit
    a team it cannot name, but every listing needs somewhere to live in the
    store tree.
    """
    if not long_name:
        return unmapped_team()
    key = _normalize(long_name)
    crosswalk = _crosswalk()
    return (
        crosswalk["team_parents"].get(key)
        # No parent: the team's own name as written in the crosswalk, which is
        # already "City Nickname" for every current club.
        or crosswalk["display"].get(key)
        or crosswalk["unmapped_team"]
    )


def team_sport(long_name: str | None) -> str | None:
    """Sport implied by the team name itself, or None if it implies no one sport.

    Preferred over the Sport item specific, which disagrees on a small number of
    listings -- basketball cards tagged Baseball, mostly. College teams field
    several sports, so they return None and leave the decision to the caller.
    """
    if not long_name:
        return None
    return _crosswalk()["team_sports"].get(_normalize(long_name))


def team_parents(value: str | Iterable[str] | None) -> list[str]:
    """Distinct store categories for a Team item specific, in the order given.

    Two-team cards usually collapse to one, the same way
    :func:`shorten_team_names` does -- "Brooklyn Dodgers | Los Angeles Dodgers"
    is one Dodgers category, not two.

    The fallback is a last resort, not a co-category: a card naming one known
    team and one unknown one files under the known team alone, and only a value
    that resolves to nothing at all lands in :func:`unmapped_team`.
    """
    out: list[str] = []
    fallback = unmapped_team()
    for name in split_team_values(value):
        parent = team_parent(name)
        if parent != fallback and parent not in out:
            out.append(parent)
    return out or [fallback]


def token_expansions() -> dict[str, str | None]:
    """Title shorthand -> replacement word. A None value means "recognized as a
    suffix token, but left as written" (e.g. SP)."""
    return dict(_crosswalk()["tokens"])


def token_removals() -> tuple[str, ...]:
    """Shorthand tokens dropped from titles entirely (e.g. MEM)."""
    return tuple(_crosswalk()["token_removals"])


def token_synonyms(word: str) -> tuple[str, ...]:
    """Words that already say what ``word`` says, ``word`` itself included."""
    configured = _crosswalk()["token_synonyms"].get(word)
    return configured if configured else (word,)


def title_removals() -> tuple[str, ...]:
    """Phrases stripped from every title."""
    return tuple(_crosswalk()["title_removals"])


def known_teams() -> dict[str, str]:
    """Full crosswalk as ``{eBay team value: short name}``, original casing."""
    display = _crosswalk()["display"]
    return {display[key]: short for key, short in _crosswalk()["teams"].items()}
