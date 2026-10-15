"""Pure logic behind `shoebox variation-template`.

A variation template is a workbook with one row per card in a subset (e.g.
2026 Topps Update Base, US1-US350) that the seller fills in with quantities and
prices. Each row carries a Segment number, and every segment becomes its own
multi-variation listing, because eBay caps a listing at 250 variations.

Nothing here touches Excel, BigQuery or eBay; the pipeline module does the I/O.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from shoebox.transforms.listing_builder import rookie
from shoebox.transforms.variation_listing_builder import ChecklistVariationRow

# eBay allows at most 250 variations in one multi-variation listing.
MAX_VARIATIONS = 250
# eBay rejects a variation value (the dropdown text) longer than 65 characters.
DISPLAY_NAME_MAX = 65

# Column order of the Checklist sheet: the columns the seller edits come first.
# Names match load_checklist_from_excel so the sheet also reads as an inventory
# workbook for `create-variation-listings`.
TEMPLATE_COLUMNS = [
    "Segment",
    "Card Number",
    "Player",
    "Team",
    "Note",
    "Display Name",
    "Qty",
    "Price",
    "Set Year",
    "Set Name",
    "Subset Name",
    "Subset Type",
    "Subset ID",
    "Derived Card Number",
    "Record ID",
]
EDITABLE_COLUMNS = {"Segment", "Display Name", "Qty", "Price"}

# Labels in column A of the Listing sheet.
DEFAULT_PRICE_LABEL = "Default Price"
PROMOTE_RATE_LABEL = "Promote Rate (%)"
TIERS_HEADER_LABEL = "Min Qty"
DEFAULT_PROMOTE_RATE = 20
DEFAULT_TIER_QUANTITIES = (2, 3, 4)


class TemplateError(ValueError):
    """A template that cannot be listed; the message names every problem found."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(f"- {p}" for p in problems))


@dataclass
class ListingSettings:
    default_price: float | None = None
    promote_rate: int = DEFAULT_PROMOTE_RATE
    volume_discount_tiers: list[dict[int, int]] = field(default_factory=list)


def _text(value: Any) -> str:
    """Cell value as text; integral floats lose their '.0' and blanks become ''."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def card_sort_key(card_number: Any, derived_card_number: Any = None) -> tuple:
    """Order cards the way the checklist prints them: US2 before US10.

    Uses the view's derived (prefix-stripped) number when it is numeric, else
    the trailing digits of the card number; text-only numbers sort last.
    """
    cn = _text(card_number)
    derived = _text(derived_card_number)
    if derived.isdigit():
        return (0, int(derived), cn)
    m = re.search(r"(\d+)\D*$", cn)
    if m:
        return (0, int(m.group(1)), cn)
    return (1, 0, cn)


def segment_sizes(n_cards: int, segments: int) -> list[int]:
    """Split n_cards into contiguous, near-equal segments of at most MAX_VARIATIONS.

    The requested count is raised when it would leave a segment over eBay's cap
    and lowered when there are fewer cards than segments.
    """
    if segments < 1:
        raise ValueError("segments must be at least 1")
    if n_cards <= 0:
        return []
    k = min(max(segments, math.ceil(n_cards / MAX_VARIATIONS)), n_cards)
    base, extra = divmod(n_cards, k)
    return [base + 1 if i < extra else base for i in range(k)]


def default_display_name(
    card_number: Any, player: Any, note: Any = None, note_check: Any = None, subset_name: Any = ""
) -> str:
    """Dropdown text for a card, e.g. 'US1 Shohei Ohtani RC', capped at eBay's limit."""
    name = " ".join(p for p in (_text(card_number), _text(player)) if p)
    if rookie(_text(note), _text(note_check), _text(subset_name)):
        name += " RC"
    return name[:DISPLAY_NAME_MAX].rstrip()


def build_template_records(checklist: list[dict[str, Any]], segments: int) -> list[dict[str, Any]]:
    """Checklist rows (v_checklist columns) -> Checklist sheet rows, sorted and segmented.

    Qty and Price are left blank for the seller to fill in.
    """
    ordered = sorted(
        checklist, key=lambda r: card_sort_key(r.get("card_number"), r.get("derived_card_number"))
    )
    segment_of: list[int] = []
    for seg, size in enumerate(segment_sizes(len(ordered), segments), 1):
        segment_of.extend([seg] * size)

    records = []
    for seg, r in zip(segment_of, ordered, strict=True):
        records.append(
            {
                "Segment": seg,
                "Card Number": _text(r.get("card_number")),
                "Player": _text(r.get("player")),
                "Team": _text(r.get("team")) or None,
                "Note": _text(r.get("note")) or None,
                "Display Name": default_display_name(
                    r.get("card_number"),
                    r.get("player"),
                    r.get("note"),
                    r.get("note_check"),
                    r.get("subset_name"),
                ),
                "Qty": None,
                "Price": None,
                "Set Year": _text(r.get("set_year")),
                "Set Name": _text(r.get("set_name")),
                "Subset Name": _text(r.get("subset_name")),
                "Subset Type": _text(r.get("subset_type")),
                "Subset ID": _text(r.get("subset_id")),
                "Derived Card Number": _text(r.get("derived_card_number")) or None,
                "Record ID": _text(r.get("card_id")),
            }
        )
    return records


def _parse_int(value: Any) -> int | None:
    """Whole number from a cell; None when blank. Raises ValueError otherwise."""
    if value is None or _text(value) == "":
        return None
    number = float(value) if isinstance(value, int | float) else float(_text(value))
    if not number.is_integer():
        raise ValueError(f"{value!r} is not a whole number")
    return int(number)


def _parse_price(value: Any) -> float | None:
    """Dollar amount from a cell ('$1.50' allowed); None when blank."""
    if value is None or _text(value) == "":
        return None
    if isinstance(value, int | float):
        return round(float(value), 2)
    return round(float(_text(value).replace("$", "").replace(",", "")), 2)


def parse_listing_settings(rows: list[tuple[Any, ...]]) -> ListingSettings:
    """Read the Listing sheet (rows of cell values) into ListingSettings.

    Settings are found by their label in the first column, so moving or adding
    rows does not break the read. The volume-discount table is every row under
    the 'Min Qty' header until the first blank row; tiers with no % are skipped.
    """
    settings = ListingSettings()
    problems: list[str] = []
    in_tiers = False
    seen_qty: set[int] = set()

    for row in rows:
        label = _text(row[0]) if row else ""
        value = row[1] if len(row) > 1 else None

        if in_tiers:
            if label == "":
                in_tiers = False
                continue
            try:
                qty = _parse_int(row[0])
                pct = _parse_int(value)
            except ValueError as e:
                problems.append(f"Volume discount row '{label}': {e}")
                continue
            if pct is None:
                continue
            if qty is None or qty < 2:
                problems.append(f"Volume discount Min Qty must be 2 or more, got {label!r}")
            elif not 0 < pct < 100:
                problems.append(f"Volume discount for {qty}+ must be 1-99% off, got {pct}")
            elif qty in seen_qty:
                problems.append(f"Volume discount Min Qty {qty} appears twice")
            else:
                seen_qty.add(qty)
                settings.volume_discount_tiers.append({qty: pct})
            continue

        try:
            if label == DEFAULT_PRICE_LABEL:
                settings.default_price = _parse_price(value)
                if settings.default_price is not None and settings.default_price <= 0:
                    problems.append(f"{DEFAULT_PRICE_LABEL} must be above 0")
            elif label == PROMOTE_RATE_LABEL:
                rate = _parse_int(value)
                settings.promote_rate = 0 if rate is None else rate
                if not 0 <= settings.promote_rate <= 100:
                    problems.append(f"{PROMOTE_RATE_LABEL} must be 0-100")
            elif label == TIERS_HEADER_LABEL:
                in_tiers = True
        except ValueError as e:
            problems.append(f"{label}: {e}")

    settings.volume_discount_tiers.sort(key=lambda t: next(iter(t)))
    if problems:
        raise TemplateError(problems)
    return settings


def group_id(subset_id: str, segment: int) -> str:
    """Key one segment's listing by; build_group_key hashes it into the eBay group key."""
    return f"{subset_id}|{segment}"


def parse_template_rows(
    records: list[dict[str, Any]],
    *,
    default_price: float | None = None,
) -> dict[str, list[ChecklistVariationRow]]:
    """Validate filled-in Checklist rows and group them by segment.

    Every card is listed so the listing covers the whole checklist; a blank or
    0 Qty shows as a sold-out variation until restocked. Every card therefore
    needs a Price or the sheet's Default Price.
    Raises TemplateError naming every problem, so one pass fixes the sheet.
    """
    problems: list[str] = []
    groups: dict[str, list[ChecklistVariationRow]] = {}
    names_seen: dict[str, set[str]] = {}

    for i, d in enumerate(records, start=2):  # row 1 is the header
        card = _text(d.get("Card Number"))
        if not card:
            continue
        where = f"Row {i} ({card})"

        try:
            qty = _parse_int(d.get("Qty")) or 0
        except ValueError as e:
            problems.append(f"{where}: Qty {e}")
            continue
        if qty < 0:
            problems.append(f"{where}: Qty cannot be negative")
            continue

        try:
            price = _parse_price(d.get("Price"))
        except ValueError:
            problems.append(f"{where}: Price {d.get('Price')!r} is not a number")
            continue
        if price is None:
            price = default_price
        if price is None or price <= 0:
            problems.append(f"{where}: needs a Price (or set {DEFAULT_PRICE_LABEL})")
            continue

        try:
            segment = _parse_int(d.get("Segment"))
        except ValueError as e:
            problems.append(f"{where}: Segment {e}")
            continue
        if segment is None or segment < 1:
            problems.append(f"{where}: needs a Segment of 1 or more")
            continue

        subset_id = _text(d.get("Subset ID"))
        if not subset_id:
            problems.append(f"{where}: Subset ID is blank")
            continue

        display = _text(d.get("Display Name")) or card
        if len(display) > DISPLAY_NAME_MAX:
            problems.append(f"{where}: Display Name is over {DISPLAY_NAME_MAX} characters")
            continue

        gid = group_id(subset_id, segment)
        seen = names_seen.setdefault(gid, set())
        if display in seen:
            problems.append(f"{where}: Display Name {display!r} repeats within segment {segment}")
            continue
        seen.add(display)

        groups.setdefault(gid, []).append(
            ChecklistVariationRow(
                record_id=_text(d.get("Record ID")),
                set_year=_text(d.get("Set Year")),
                subset_id=subset_id,
                set_name=_text(d.get("Set Name")),
                subset_name=_text(d.get("Subset Name")),
                subset_type=_text(d.get("Subset Type")),
                derived_card_number=_text(d.get("Derived Card Number")) or None,
                card_number=card,
                qty=qty,
                price=price,
                player=_text(d.get("Player")),
                team=_text(d.get("Team")) or None,
                display_name=display,
                note=_text(d.get("Note")) or None,
            )
        )

    for gid, rows in groups.items():
        if len(rows) > MAX_VARIATIONS:
            seg = gid.rsplit("|", 1)[1]
            problems.append(
                f"Segment {seg} has {len(rows)} cards; eBay allows {MAX_VARIATIONS} per listing"
            )
    if not problems and not groups:
        problems.append("No cards in the Checklist sheet")
    if problems:
        raise TemplateError(problems)

    def _order(gid: str) -> tuple[str, int]:
        subset_id, seg = gid.rsplit("|", 1)
        return (subset_id, int(seg))

    return {gid: groups[gid] for gid in sorted(groups, key=_order)}
