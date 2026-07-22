from __future__ import annotations

import hashlib

from shoebox.models.checklist import ChecklistRow


def _normalize_keys(row: dict[str, str]) -> dict[str, str]:
    # handles BOM on first column + spaces/case differences
    out: dict[str, str] = {}
    for k, v in row.items():
        if k is None:
            continue
        key = k.lstrip("\ufeff").strip().lower().replace(" ", "_")
        out[key] = v
    return out


def _clean_to_none(s: str | None) -> str | None:
    v = (s or "").strip()
    return v if v else None


def _hash_for_row(values: dict[str, str]) -> str:
    payload = "|".join(values[k] for k in sorted(values))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def checklist_transform(csv_row: dict[str, str], source_file: str) -> dict:
    row_obj = ChecklistRow(
        set_name=_clean_to_none(csv_row.get("set_name")) or "",  # required -> force non-null
        subset_name=_clean_to_none(csv_row.get("subset_name")) or "",
        subset_type=_clean_to_none(csv_row.get("subset_type")) or "",
        card_number=_clean_to_none(csv_row.get("card_number")) or "",
        player=_clean_to_none(csv_row.get("player")) or "",
        team=_clean_to_none(csv_row.get("team")) or "",
        note=_clean_to_none(csv_row.get("note")),  # nullable
    )

    return row_obj.to_bq_json()
