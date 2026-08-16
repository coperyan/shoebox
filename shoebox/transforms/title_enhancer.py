"""Rewrite an existing listing title into its enhanced form.

Applied in order, each step driven by ``configs/title_crosswalk.yaml``:

1. **Repair the characters.** Titles carrying accented or mis-encoded text
   ("Vidal BrujÃ¡n") are un-garbled and folded to plain ASCII.
2. **Strip dead phrases.** Anything in ``title_removals`` ("Topps Baseball")
   comes out wherever it appears.
3. **Handle the shorthand.** ``(RC)`` -> ``Rookie``, ``AU`` -> ``Auto``, ``MEM``
   dropped. An expansion is skipped when the title already says the same thing
   ("Rookie Revolution ... (RC)" keeps the insert name and drops the token).
   Card numbers such as ``#RC-6`` are never touched.
4. **Add the team.** The short name goes ahead of the trailing suffix tokens --
   the slot :func:`shoebox.transforms.listing_builder.title` uses -- unless the
   title already names the team in some spelling.

eBay caps titles at 80 characters. When the result overflows, pieces are given
up in this order: the ``" - "`` separator, then the card number, then the team,
then the expansions. Character repair, phrase stripping, and token removal are
never given up -- they only ever shorten the title. A title that cannot fit even
those is left exactly as it was and flagged. Every outcome is reported on
:class:`EnhancedTitle` rather than logged, so the pipeline can show the user
what happened before anything is pushed to eBay.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from unidecode import unidecode

from shoebox.utils.title_crosswalk import (
    all_team_aliases,
    shorten_team_names,
    split_team_values,
    title_removals,
    token_expansions,
    token_removals,
    token_synonyms,
)

# eBay's hard limit on listing titles.
MAX_TITLE_LENGTH = 80

# A token counts as shorthand only when it stands alone. The lookarounds
# exclude the hyphens and '#' of card numbers, so "#RC-6" and "RC-15" survive
# while "Judge RC" and "(AU,RC)" are rewritten.
_TOKEN_LOOKBEHIND = r"(?<![A-Za-z0-9#\-/])"
_TOKEN_LOOKAHEAD = r"(?![A-Za-z0-9\-/])"

# Serial numbering ("/499") trails everything else in a title.
_PRINT_RUN_RE = re.compile(r"^/\d+$")

# The card number is the "#..." token -- "#82", "#91C-23", "#CCR-ET".
_CARD_NUMBER_RE = re.compile(r"\s#\S+")

# The set/card separator the listing builder emits.
_SEPARATOR = " - "


@dataclass
class EnhancedTitle:
    """Result of enhancing one title. ``changed`` is the only thing the caller
    has to check before deciding whether to push an update."""

    original: str
    title: str
    team: str | None = None
    team_short: str | None = None
    changes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.title != self.original

    @property
    def length(self) -> int:
        return len(self.title)


# --------------------------------------------------------------------------
# Character repair
# --------------------------------------------------------------------------


def _repair_mojibake(text: str) -> str:
    """Undo UTF-8-read-as-latin-1 damage, as far as it goes.

    eBay stores whatever it was sent, so "Vidal Bruján" that went through a bad
    encoding hop is live as "Vidal BrujÃ¡n". The round-trip is its own test:
    text that was never damaged either isn't latin-1 encodable ("Jokić") or
    doesn't decode as UTF-8 ("Montréal"), and comes back untouched. Repeated
    because doubly-encoded titles exist.
    """
    for _ in range(3):
        if text.isascii():
            break
        try:
            repaired = text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if repaired == text:
            break
        text = repaired
    return text


def normalize_characters(title: str) -> str:
    """Fold a title to plain ASCII, repairing mis-encoded text on the way.

    "Vidal BrujÃ¡n" -> "Vidal Brujan", "Nikola JokiÄ" -> "Nikola Jokic",
    curly quotes -> straight ones.
    """
    repaired = _repair_mojibake(title)
    # NFKC first so decomposed accents (a + combining acute) fold as one
    # character rather than leaving a stray mark behind.
    ascii_text = unidecode(unicodedata.normalize("NFKC", repaired))
    # Control characters and soft hyphens survive unidecode; drop them.
    ascii_text = "".join(
        c for c in ascii_text if c == "\t" or not unicodedata.category(c)[0] == "C"
    )
    return _tidy(ascii_text)


def has_illegal_characters(title: str) -> bool:
    """True when a title carries anything outside plain printable ASCII."""
    return any(ord(c) > 126 or (ord(c) < 32 and c != "\t") for c in title)


# --------------------------------------------------------------------------
# Text tidying
# --------------------------------------------------------------------------


def _tidy(title: str) -> str:
    """Collapse the whitespace and dangling separators edits leave behind."""
    cleaned = re.sub(r"\s+", " ", title).strip()
    cleaned = re.sub(r"\s+([,)])", r"\1", cleaned)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" -")


def strip_phrases(title: str) -> str:
    """Remove every configured dead phrase (``title_removals``)."""
    for phrase in title_removals():
        title = re.sub(re.escape(phrase), " ", title, flags=re.IGNORECASE)
    return _tidy(title)


def remove_separator(title: str) -> str:
    """Drop the first ``" - "`` separator -- the cheapest two characters."""
    return _tidy(title.replace(_SEPARATOR, " ", 1))


def remove_card_number(title: str) -> str:
    """Drop the ``#...`` card-number token."""
    return _tidy(_CARD_NUMBER_RE.sub(" ", title, count=1))


# --------------------------------------------------------------------------
# Shorthand tokens
# --------------------------------------------------------------------------


def _known_tokens() -> dict[str, str | None]:
    """Every recognized shorthand token -> its replacement (None = keep as-is,
    and removals map to None too since they never reach the output)."""
    tokens = token_expansions()
    for token in token_removals():
        tokens.setdefault(token, None)
    return tokens


def _shorthand_matches(title: str, tokens: dict[str, str | None]) -> list[re.Match[str]]:
    """Locate every standalone shorthand occurrence, bare or parenthesized."""
    names = "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))
    pattern = re.compile(
        rf"\(([^)]*)\)|{_TOKEN_LOOKBEHIND}(?P<bare>{names}){_TOKEN_LOOKAHEAD}",
        re.IGNORECASE,
    )

    matches = []
    for match in pattern.finditer(title):
        if match.group("bare"):
            matches.append(match)
            continue
        # A parenthesized group only counts when every part of it is shorthand;
        # "(107/182)" and free-text notes are left alone.
        parts = [p.strip() for p in match.group(1).split(",")]
        if parts and all(p.upper() in tokens for p in parts):
            matches.append(match)
    return matches


def _match_tokens(match: re.Match[str]) -> list[tuple[str, str]]:
    """The tokens in one match as ``(as written, lookup key)`` pairs.

    The original spelling is kept so a token left unexpanded goes back exactly
    as the seller wrote it -- "Auto" must not come back as "AUTO".
    """
    if match.group("bare"):
        raw = match.group("bare")
        return [(raw, raw.upper())]
    return [(p.strip(), p.strip().upper()) for p in match.group(1).split(",")]


def _already_said(word: str, context: str) -> bool:
    """True when ``context`` already carries ``word`` or one of its synonyms."""
    for synonym in token_synonyms(word):
        if re.search(rf"(?<![A-Za-z]){re.escape(synonym)}(?![A-Za-z])", context, re.IGNORECASE):
            return True
    return False


def apply_shorthand(title: str, *, expand: bool = True) -> tuple[str, list[str]]:
    """Expand, drop, and de-duplicate the shorthand tokens in a title.

    Returns the rewritten title and the change labels that describe what
    happened, so the caller can report which rule fired. ``expand=False`` still
    drops removed tokens (MEM is unwanted at any length) but leaves the rest
    written as they were -- the last resort before giving up on a long title.
    """
    tokens = _known_tokens()
    removals = set(token_removals())
    matches = _shorthand_matches(title, tokens)
    if not matches:
        return _tidy(title), []

    # Every expansion is judged against the title *minus* all its shorthand, so
    # a token never counts as its own duplicate.
    residual = title
    for match in reversed(matches):
        residual = residual[: match.start()] + " " + residual[match.end() :]

    changes: set[str] = set()
    out: list[str] = []
    cursor = 0
    for match in matches:
        out.append(title[cursor : match.start()])
        cursor = match.end()

        words: list[str] = []
        for raw, key in _match_tokens(match):
            if key in removals:
                changes.add("removed_token")
                continue
            expansion = tokens.get(key)
            if not expand or not expansion or expansion == raw:
                words.append(raw)
                continue
            if _already_said(expansion, residual) or _already_said(expansion, " ".join(words)):
                changes.add("removed_redundant_token")
                continue
            changes.add("expanded_shorthand")
            words.append(expansion)

        out.append(" ".join(words))
    out.append(title[cursor:])

    return _tidy("".join(out)), sorted(changes)


# --------------------------------------------------------------------------
# Team
# --------------------------------------------------------------------------


def title_names_team(title: str, team: str | Iterable[str] | None) -> bool:
    """True when the title already identifies the team, in any known spelling."""
    for alias in all_team_aliases(team):
        if re.search(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", title, re.IGNORECASE):
            return True
    return False


def _suffix_pattern() -> re.Pattern[str]:
    """Matches the shorthand and print-run tokens trailing a title.

    Deliberately matches shorthand only -- "(RC)", "(AU, RC)", "AU", "/499" --
    and never the words it expands to. "2025 Bowman - Acuna #32 Red Rookie"
    ends in a *parallel name*, and the team belongs after it, not in front.
    """
    tokens = _known_tokens()
    names = "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))
    group = rf"\(\s*(?:{names})(?:\s*,\s*(?:{names}))*\s*\)"
    unit = rf"(?:/\d+|{group}|{_TOKEN_LOOKBEHIND}(?:{names}){_TOKEN_LOOKAHEAD})"
    return re.compile(rf"(?:\s+{unit})+$", re.IGNORECASE)


def _split_suffix(title: str) -> tuple[str, str]:
    """Split a title into its body and its trailing shorthand/print-run tokens."""
    match = _suffix_pattern().search(title)
    if not match:
        return title, ""
    return title[: match.start()].rstrip(), match.group(0).strip()


def add_team(title: str, team_short: str) -> str:
    """Insert the short team name ahead of any trailing suffix tokens."""
    body, tail = _split_suffix(title)
    return " ".join(filter(None, [body, team_short, tail]))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def enhance_title(
    title: str,
    team: str | Iterable[str] | None = None,
    *,
    max_length: int = MAX_TITLE_LENGTH,
) -> EnhancedTitle:
    """Enhanced form of ``title`` for a card whose eBay ``Team`` value is ``team``.

    ``team`` takes whatever the source hands over -- a list, a "Team A | Team B"
    string, or a single name.
    """
    original = (title or "").strip()
    teams = split_team_values(team)
    result = EnhancedTitle(original=original, title=original, team=" | ".join(teams) or None)

    if not original:
        result.notes.append("empty_title")
        return result

    # --- corrections: never traded away for length, only ever shorten ---
    corrected = normalize_characters(original)
    base_changes: list[str] = []
    if corrected != original:
        base_changes.append("normalized_characters")

    stripped = strip_phrases(corrected)
    if stripped != corrected:
        base_changes.append("removed_phrase")

    # --- team ---
    # Placed before the shorthand is expanded so the trailing tokens are still
    # recognizable as shorthand; expansion then happens around the team.
    shorts = shorten_team_names(teams)
    # A card credited to two teams that share a short name ("Brooklyn Dodgers |
    # Los Angeles Dodgers") is safe to tag; a genuine multi-team card (league
    # leaders, combo cards) is left alone rather than crammed with names.
    team_short = shorts[0] if len(shorts) == 1 else None
    result.team_short = team_short

    teamed = stripped
    team_changes: list[str] = []
    if len(shorts) > 1:
        result.notes.append("multiple_teams_skipped")
    elif teams and not team_short:
        result.notes.append("team_not_in_crosswalk")
    elif team_short and title_names_team(stripped, teams):
        result.notes.append("team_already_in_title")
    elif team_short:
        teamed = add_team(stripped, team_short)
        team_changes.append("added_team")

    # --- shorthand ---
    with_team, token_changes = apply_shorthand(teamed)
    # The same title without the team, and again with nothing expanded: held in
    # reserve for titles short on room.
    expanded, _ = apply_shorthand(stripped)
    no_team_minimal, no_team_minimal_changes = apply_shorthand(stripped, expand=False)

    # --- fit to 80 characters, giving up the least valuable piece first: the
    # separator, then the card number, then the team, then the expansions ---
    full_changes = base_changes + token_changes + team_changes
    no_sep = remove_separator(with_team)
    candidates: list[tuple[str, list[str], list[str]]] = [
        (with_team, full_changes, []),
        (no_sep, full_changes, []),
        (remove_card_number(no_sep), full_changes, []),
        (
            remove_card_number(remove_separator(expanded)),
            base_changes + token_changes,
            ["team_dropped_for_length"],
        ),
        (
            remove_card_number(remove_separator(no_team_minimal)),
            base_changes + no_team_minimal_changes,
            ["team_dropped_for_length", "expansion_dropped_for_length"],
        ),
    ]

    for candidate, changes, fallback_notes in candidates:
        if not candidate or len(candidate) > max_length:
            continue

        result.title = candidate
        result.changes = list(changes)
        # Only claim a step that actually did something: a title with no " - "
        # wasn't shortened by dropping one.
        if _SEPARATOR in with_team and _SEPARATOR not in candidate:
            result.changes.append("removed_separator")
        if _CARD_NUMBER_RE.search(with_team) and not _CARD_NUMBER_RE.search(candidate):
            result.changes.append("removed_card_number")
        for note in fallback_notes:
            if note == "team_dropped_for_length" and not team_changes:
                continue  # no team was added, so none was given up
            if note == "expansion_dropped_for_length" and "expanded_shorthand" not in token_changes:
                continue
            result.notes.append(note)
        return result

    # Even the corrections alone overflow -- leave the live title untouched.
    result.title = original
    result.changes = []
    result.notes.append("over_max_length_unchanged")
    return result
