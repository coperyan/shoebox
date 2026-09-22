"""Export the account's entire eBay watch list, enriched with full item details.

Two passes over the Trading API:

1. ``GetMyeBayBuying`` (WatchList) -- every watched listing, paged 200 at a
   time. Gives price, bids, seller, time left, but no item specifics.
2. ``GetItem`` per listing, run concurrently -- adds item specifics (aspects),
   category, condition, and picture URLs.

Both rows are merged per item. Listings whose GetItem call fails (usually one
that ended and aged out) keep their watch-list fields and record the error.

    python scripts/export_watch_list.py
    python scripts/export_watch_list.py --sort CurrentPrice --workers 12 --out watch.csv

Writes a JSONL file (aspects kept as ``{name: [values]}``) and a CSV with one
``aspect:<Name>`` column per aspect seen, multiple values joined with " | ".
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pandas as pd

from shoebox.clients.ebay.trading import TradingClient
from shoebox.settings import get_settings
from shoebox.utils.logging_setup import setup_logging

# Fields only GetItem returns; always copied onto the watch-list row. For fields
# both calls return (price, status, end time) the watch-list value is kept and
# GetItem only fills in blanks.
DETAIL_FIELDS = (
    "category_id",
    "category_name",
    "condition_id",
    "condition_display_name",
    "quantity_sold",
    "picture_urls",
    "item_specifics",
)

ASPECT_PREFIX = "aspect:"
ASPECT_JOIN = " | "


def fetch_watch_list(
    client: TradingClient, sort: str = "EndTime", max_pages: int | None = None
) -> list[dict[str, Any]]:
    watched = client.get_watch_list(sort=sort, max_pages=max_pages)
    print(f"Watch list: {len(watched)} listing(s)")

    item_ids = [str(row["item_id"]) for row in watched if row.get("item_id")]

    def progress(done: int, total: int) -> None:
        if done == total or done % 25 == 0:
            print(f"  GetItem {done}/{total}")

    details, failures = client.get_item_details_bulk(item_ids, on_progress=progress)
    by_id = {str(d["item_id"]): d for d in details}

    rows: list[dict[str, Any]] = []
    for row in watched:
        item_id = str(row.get("item_id") or "")
        merged = dict(row)
        detail = by_id.get(item_id)
        if detail:
            for field in DETAIL_FIELDS:
                merged[field] = detail.get(field)
            # Fall back to GetItem for anything the watch-list node left blank.
            for key, value in detail.items():
                if merged.get(key) in (None, "") and value not in (None, ""):
                    merged[key] = value
        else:
            merged["item_specifics"] = {}
            merged["detail_error"] = failures.get(item_id, "no details returned")
        rows.append(merged)

    if failures:
        print(f"GetItem failed for {len(failures)} listing(s); kept their watch-list fields.")
    return rows


def write_jsonl(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    base_cols: list[str] = []
    aspect_names: list[str] = []
    for row in rows:
        for key in row:
            if key != "item_specifics" and key not in base_cols:
                base_cols.append(key)
        for name in row.get("item_specifics") or {}:
            if name not in aspect_names:
                aspect_names.append(name)

    fieldnames = base_cols + [f"{ASPECT_PREFIX}{name}" for name in sorted(aspect_names)]

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = {k: v for k, v in row.items() if k != "item_specifics"}
            if isinstance(out.get("picture_urls"), list):
                out["picture_urls"] = ASPECT_JOIN.join(out["picture_urls"])
            for name, values in (row.get("item_specifics") or {}).items():
                out[f"{ASPECT_PREFIX}{name}"] = ASPECT_JOIN.join(values)
            writer.writerow(out)


setup_logging()
settings = get_settings()


client = TradingClient(token_path=settings.ebay.trading_token_path)
rows = fetch_watch_list(client)
df = pd.json_normalize(rows)
