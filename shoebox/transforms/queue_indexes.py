"""Dropdown indexes and the metadata payload built from the checklist/parallels views.

``build_indexes`` used to live in the Streamlit module (``shoebox/ui/app.py``);
it moved here so the listing-queue API (``shoebox/api``) can share it without
importing Streamlit. The cascading order it produces -- Set -> Subset ->
(Parallel) -> Card #, card numbers sorted numerically by ``derived_card_number``
first -- is what both the Streamlit UI and the iOS app show.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import pandas as pd


def cardno_sort_key(derived: object) -> tuple:
    """Sort key for card numbers: pure integers first (as ints), then text
    (case-insensitive), then blanks. Mirrored in the iOS app (``CardSort``);
    change both together."""
    if derived is None or (isinstance(derived, float) and pd.isna(derived)):
        return (2, float("inf"), "")
    s = str(derived).strip()
    if not s:
        return (2, float("inf"), "")
    if re.fullmatch(r"\d+", s):
        return (0, int(s), "")
    return (1, float("inf"), s.upper())


def build_indexes(cdf: pd.DataFrame, pdf: pd.DataFrame) -> dict[str, object]:
    """
    Precompute lookups so dropdowns are fast and in-memory.
    Sort Card # by derived_card_number (numbers first as ints, then text),
    but display/return the actual card_number.
    """

    # Sets from checklist (authoritative)
    sets = sorted([x for x in cdf["set_name"].dropna().unique().tolist()])

    subsets_by_set: dict[str, list[str]] = {}
    cardnos_by_set_subset: dict[tuple[str, str], list[str]] = {}
    parallels_by_set_subset: dict[tuple[str, str], list[str]] = {}

    # Subsets from checklist
    for s, g in cdf.groupby("set_name", dropna=False):
        if pd.isna(s):
            continue
        subsets_by_set[str(s)] = sorted([x for x in g["subset_name"].dropna().unique().tolist()])

    # Card numbers: sort by derived_card_number, return/display card_number
    for (s, sub), g in cdf.groupby(["set_name", "subset_name"], dropna=False):
        if pd.isna(s) or pd.isna(sub):
            continue
        key = (str(s), str(sub))

        tmp = (
            g.loc[:, ["card_number", "derived_card_number"]]
            .dropna(subset=["card_number"])
            .drop_duplicates(subset=["card_number"])
        )

        pairs = list(
            zip(tmp["derived_card_number"].tolist(), tmp["card_number"].tolist(), strict=False)
        )
        pairs.sort(key=lambda t: cardno_sort_key(t[0]))

        cardnos_by_set_subset[key] = [card_no for _, card_no in pairs]

    # Parallels come from parallels view
    for (s, sub), g in pdf.groupby(["set_name", "subset_name"], dropna=False):
        if pd.isna(s) or pd.isna(sub):
            continue
        key = (str(s), str(sub))
        parallels_by_set_subset[key] = sorted(
            [x for x in g["parallel_variety"].dropna().unique().tolist()]
        )

    return {
        "sets": sets,
        "subsets_by_set": subsets_by_set,
        "cardnos_by_set_subset": cardnos_by_set_subset,
        "parallels_by_set_subset": parallels_by_set_subset,
    }


def json_scalar(value: Any) -> str | int | float | bool | None:
    """A pandas/numpy cell as a JSON-safe scalar; nulls and null-like strings -> None."""
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        # numpy scalar (np.int64, np.float64, np.bool_)
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float):
        if pd.isna(value):
            return None
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, str):
        t = value.strip()
        if t.lower() in {"", "none", "null", "nan", "<na>", "na", "n/a"}:
            return None
        return t
    return value


def _int_or_none(value: Any) -> int | None:
    v = json_scalar(value)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


CARD_FIELDS = (
    "card_number",
    "derived_card_number",
    "card_id",
    "subset_id",
    "set_year",
    "subset_type",
    "player",
    "team",
    "note",
    "note_check",
)


def build_metadata_payload(cdf: pd.DataFrame, pdf: pd.DataFrame) -> dict[str, Any]:
    """The ``GET /metadata`` body: sets -> subsets -> cards + parallels, JSON-safe.

    Ordering is exactly ``build_indexes``' (sets, subsets and parallels sorted;
    cards numeric-first by ``derived_card_number``), and the per-card detail is
    the first checklist row for that card number, matching the UI's
    ``cg.iloc[0]`` autofill. Clients keep this order; they never re-sort.
    """
    idx = build_indexes(cdf, pdf)

    card_detail: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for (s, sub), g in cdf.groupby(["set_name", "subset_name"], dropna=False):
        if pd.isna(s) or pd.isna(sub):
            continue
        by_number: dict[str, dict[str, Any]] = {}
        for rec in g.to_dict("records"):
            number = json_scalar(rec.get("card_number"))
            if number is None or number in by_number:
                continue
            by_number[str(number)] = {
                f: (json_scalar(rec.get(f)) if f != "card_number" else str(number))
                for f in CARD_FIELDS
            }
        card_detail[(str(s), str(sub))] = by_number

    parallel_detail: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for (s, sub), g in pdf.groupby(["set_name", "subset_name"], dropna=False):
        if pd.isna(s) or pd.isna(sub):
            continue
        by_name: dict[str, dict[str, Any]] = {}
        for rec in g.to_dict("records"):
            name = json_scalar(rec.get("parallel_variety"))
            if name is None or name in by_name:
                continue
            by_name[str(name)] = {
                "parallel_variety": str(name),
                "parallel_id": json_scalar(rec.get("parallel_id")),
                "print_run": _int_or_none(rec.get("print_run")),
                "other_note": json_scalar(rec.get("other_note")),
            }
        parallel_detail[(str(s), str(sub))] = by_name

    sets_out: list[dict[str, Any]] = []
    for set_name in idx["sets"]:
        subsets_out: list[dict[str, Any]] = []
        for subset_name in idx["subsets_by_set"].get(set_name, []):  # type: ignore[union-attr]
            key = (set_name, subset_name)
            cards_by_number = card_detail.get(key, {})
            cards = [
                cards_by_number[n]
                for n in idx["cardnos_by_set_subset"].get(key, [])  # type: ignore[union-attr]
                if n in cards_by_number
            ]
            parallels_by_name = parallel_detail.get(key, {})
            parallels = [
                parallels_by_name[n]
                for n in idx["parallels_by_set_subset"].get(key, [])  # type: ignore[union-attr]
                if n in parallels_by_name
            ]
            subsets_out.append({"subset_name": subset_name, "cards": cards, "parallels": parallels})
        sets_out.append({"set_name": set_name, "subsets": subsets_out})

    return {
        "counts": {
            "checklist_rows": int(len(cdf)),
            "parallel_rows": int(len(pdf)),
            "sets": len(sets_out),
        },
        "sets": sets_out,
    }


def metadata_version(payload: dict[str, Any]) -> str:
    """Stable fingerprint of the metadata content, used as the ETag / ``version``."""
    blob = json.dumps(payload.get("sets", []), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
