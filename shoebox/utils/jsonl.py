from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic_core import to_jsonable_python


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, records: list[dict], *, default: Any = None) -> None:
    """Write records to JSONL, creating parent dirs.

    ``default`` is passed to ``json.dumps`` for values it can't serialize
    natively (e.g. Decimal).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=default) + "\n")


def append_jsonl(path: Path, obj: Any) -> None:
    """Append one record; uses pydantic's to_jsonable_python to handle datetimes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(to_jsonable_python(obj), ensure_ascii=False) + "\n")
