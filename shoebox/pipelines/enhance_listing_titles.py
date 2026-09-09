"""Enhance the titles of listings already live on eBay.

Works from a dataframe of listings: by default the BigQuery view
``<ebay_dataset>.v_active_listing_details``, or any file passed to ``--input``
carrying ``item_id``/``sku``/``title``/``team`` columns. Works out the better
title for each, and -- only when asked -- pushes the changes to eBay.

    shoebox enhance-listing-titles                       # preview from BigQuery
    shoebox enhance-listing-titles --input pull.csv      # preview a local export
    shoebox enhance-listing-titles --apply --limit 25    # push the first 25

Nothing is sent to eBay without ``--apply``. The report CSV is written on every
run, so the preview is the thing to read before applying.
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from shoebox.clients.bigquery import BigQueryClient
from shoebox.clients.ebay.client import EbayClient
from shoebox.services.listings import ListingService
from shoebox.settings import get_settings
from shoebox.transforms.title_enhancer import (
    MAX_TITLE_LENGTH,
    enhance_title,
    has_illegal_characters,
)
from shoebox.utils.logging_setup import setup_logging
from shoebox.utils.render_table import render_table
from shoebox.utils.slack import notify_best_effort

logger = logging.getLogger(__name__)

# Multi-variation "You Pick" listings have no single card to tag and are edited
# as a group, so they are never candidates for a retitle.
_VARIATION_TITLE_MARKER = "complete your set"

_REQUIRED_COLUMNS = ("title",)

# The default source: one row per active listing, already joined to its item
# specifics. Lives in configs/bigquery/queries/.
_LISTINGS_SQL = "active_listing_details.sql"


def _check_columns(df: pd.DataFrame, source: str) -> pd.DataFrame:
    missing = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{source} is missing required column(s): {', '.join(missing)}")
    return df


def load_listings_from_bigquery(bq_client: BigQueryClient | None = None) -> pd.DataFrame:
    """Read the active-listing view -- the default source when no file is given."""
    settings = get_settings()
    bq_client = bq_client or BigQueryClient(settings)
    df = bq_client.run_query(
        sql=_LISTINGS_SQL,
        params={"dataset": settings.bigquery.ebay_dataset},
        return_df=True,
    )
    return _check_columns(df, f"{settings.bigquery.ebay_dataset}.v_active_listing_details")


def load_listings(path: Path) -> pd.DataFrame:
    """Load listings from a .jsonl or .csv export into a dataframe."""
    if not path.exists():
        raise FileNotFoundError(f"No listing export at {path}.")

    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path, dtype=str)
    else:
        df = pd.read_json(path, lines=True, dtype=False)

    return _check_columns(df, str(path))


def _clean_str(value: object) -> str:
    """A trimmed string, treating a missing value as the empty string it means.

    ``str(nan or "")`` is ``"nan"`` -- truthy, and enough to send a float where
    a SKU belongs -- so missing values have to be tested, not coerced. Covers
    ``None``, ``NaN`` and ``pd.NA``, which is what a nullable BigQuery column
    hands back.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass  # arrays and lists aren't scalar-missing; fall through
    return str(value).strip()


def _parse_specifics(value: object) -> dict:
    """Item specifics arrive as a dict, a JSON string, or a python-repr string."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
    return {}


def extract_team(row: pd.Series) -> object:
    """Pull the Team item specific from whichever shape the export carries.

    A flattened ``team`` column wins when present; otherwise it comes out of
    ``item_specifics`` (a dict from the JSONL export, a JSON string from a
    BigQuery CSV). A repeated BigQuery column arrives as a numpy array rather
    than a list, hence the general iterable check.
    """
    team = row.get("team")
    if isinstance(team, str):
        if team.strip():
            return team
    elif isinstance(team, Iterable):
        values = [v for v in team if _clean_str(v)]
        if values:
            return values

    specifics = _parse_specifics(row.get("item_specifics"))
    return specifics.get("Team")


def build_title_changes(df: pd.DataFrame, *, max_length: int = MAX_TITLE_LENGTH) -> pd.DataFrame:
    """Evaluate every listing and return one report row each.

    Every input row comes back -- including the unchanged ones -- so the report
    doubles as a record of what was considered and why it was left alone.
    """
    records = []
    for _, row in df.iterrows():
        title = _clean_str(row.get("title"))
        result = enhance_title(title, extract_team(row), max_length=max_length)

        sku = _clean_str(row.get("sku"))
        item_id = _clean_str(row.get("item_id")).removesuffix(".0")

        skip_reason = ""
        if _VARIATION_TITLE_MARKER in title.lower():
            skip_reason = "variation_listing"
        elif not sku and not item_id:
            # Nothing to address the update to on either API.
            skip_reason = "no_sku_or_item_id"

        records.append(
            {
                "item_id": item_id,
                "sku": sku,
                # SKU-less listings were created outside the Sell Inventory API
                # and can only be retitled through Trading by item ID.
                "update_method": "inventory" if sku else "trading",
                "team": result.team,
                "team_short": result.team_short,
                "old_title": result.original,
                "new_title": result.title,
                "old_length": len(result.original),
                "new_length": result.length,
                "had_illegal_chars": has_illegal_characters(result.original),
                "changed": bool(result.changed and not skip_reason),
                "changes": ",".join(result.changes),
                "notes": ",".join(result.notes),
                "skip_reason": skip_reason,
            }
        )

    return pd.DataFrame.from_records(records)


def write_report(changes: pd.DataFrame, exports_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y_%m_%d_%H_%M_%S")
    out_path = exports_dir / f"csv/title_enhancements_{stamp}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    changes.to_csv(out_path, index=False)
    return out_path


def _log_summary(changes: pd.DataFrame) -> None:
    from rich.console import Console

    total = len(changes)
    updatable = changes[changes["changed"]]
    logger.info("%d listing(s) read; %d with a better title", total, len(updatable))

    illegal = int(changes["had_illegal_chars"].sum())
    if illegal:
        logger.info("%d title(s) carry non-ASCII/mis-encoded characters", illegal)

    for label, series in (
        ("changes", updatable["changes"]),
        ("notes", changes["notes"]),
        ("skipped", changes["skip_reason"]),
    ):
        counts = series[series.astype(bool)].str.split(",").explode().value_counts().to_dict()
        if counts:
            logger.info("%s: %s", label, counts)

    if not updatable.empty:
        logger.info("update route: %s", updatable["update_method"].value_counts().to_dict())

    if not updatable.empty:
        preview = updatable.head(15)[["old_title", "new_title", "new_length"]]
        Console().print(render_table(preview, title="Title changes (first 15)"))


def apply_title_changes(
    changes: pd.DataFrame,
    *,
    listings: ListingService | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """Push the changed titles to eBay, one listing at a time.

    A failure on one listing is recorded and the run continues -- a bad SKU
    partway through a 1,000-listing sweep shouldn't strand the rest.
    """
    listings = listings or ListingService(EbayClient())
    targets = changes[changes["changed"]].copy()
    if limit is not None:
        targets = targets.head(limit)

    statuses: list[str] = []
    methods: list[str] = []
    errors: list[str] = []
    total = len(targets)
    for idx, (_, row) in enumerate(targets.iterrows(), 1):
        sku = _clean_str(row.get("sku"))
        item_id = _clean_str(row.get("item_id"))
        try:
            outcome = listings.update_title(
                new_title=row["new_title"],
                sku=sku or None,
                item_id=item_id or None,
            )
            statuses.append("updated")
            methods.append(str((outcome or {}).get("method") or ""))
            errors.append("")
        except Exception as e:
            logger.warning("Failed to retitle sku=%s item_id=%s: %s", sku or "-", item_id, e)
            statuses.append("failed")
            methods.append("")
            errors.append(str(e))
        if idx % 25 == 0 or idx == total:
            logger.info("Applied %d of %d title update(s)", idx, total)

    # Assign by position, not a merge: SKU is not a usable key here (it is
    # blank for Trading-API listings, and joining on it multiplies those rows).
    return targets.assign(status=statuses, applied_method=methods, error=errors)


def enhance_listing_titles(
    *,
    input_path: Path | None = None,
    apply: bool = False,
    limit: int | None = None,
    max_length: int = MAX_TITLE_LENGTH,
) -> pd.DataFrame:
    settings = get_settings()

    if input_path:
        df = load_listings(input_path)
        source = str(input_path)
    else:
        df = load_listings_from_bigquery()
        source = f"BigQuery {settings.bigquery.ebay_dataset}.v_active_listing_details"
    logger.info("Loaded %d listing(s) from %s", len(df), source)

    changes = build_title_changes(df, max_length=max_length)
    report_path = write_report(changes, Path(settings.paths.exports_dir))
    _log_summary(changes)
    logger.info("Report written to %s", report_path)

    if not apply:
        logger.info("Preview only — re-run with --apply to update eBay.")
        return changes

    applied = apply_title_changes(changes, limit=limit)
    updated = int((applied["status"] == "updated").sum()) if not applied.empty else 0
    failed = int((applied["status"] == "failed").sum()) if not applied.empty else 0

    applied.to_csv(report_path.with_name(report_path.stem + "_applied.csv"), index=False)
    notify_best_effort(
        settings.slack.notify_channel,
        f"Enhanced {updated} listing title(s){f', {failed} failed' if failed else ''}.",
    )
    logger.info("Updated %d listing title(s); %d failed", updated, failed)
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="Listings export (.jsonl or .csv)")
    parser.add_argument("--apply", action="store_true", help="Push the changes to eBay")
    parser.add_argument("--limit", type=int, help="Cap how many listings are updated")
    args = parser.parse_args()

    # `shoebox enhance-listing-titles` configures logging for us; running this
    # module directly has to do it itself or the summary goes nowhere.
    setup_logging()

    enhance_listing_titles(
        input_path=Path(args.input) if args.input else None,
        apply=args.apply,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
