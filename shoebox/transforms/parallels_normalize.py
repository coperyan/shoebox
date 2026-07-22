from __future__ import annotations


def _clean_to_none(s: str | None) -> str | None:
    v = (s or "").strip()
    return v if v else None


def _to_int_or_none(val: str | None) -> int | None:
    v = (val or "").strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def parallels_transform(csv_row: dict[str, str], source_file: str) -> dict:
    # tolerate multiple possible column spellings
    other_note_val = (
        csv_row.get("other_note") or csv_row.get("Other Note") or csv_row.get("other note")
    )

    return {
        "set_name": _clean_to_none(csv_row.get("set_name")) or "",
        "subset_name": _clean_to_none(csv_row.get("subset_name")) or "",
        "parallel_variety": _clean_to_none(csv_row.get("parallel_variety")) or "",
        "print_run": _to_int_or_none(csv_row.get("print_run")),
        "other_note": _clean_to_none(other_note_val),  # nullable
    }
