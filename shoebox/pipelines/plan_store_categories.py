"""Work out the new eBay store category for every active listing.

Reads the active listings -- by default the BigQuery view
``<ebay_dataset>.v_active_listing_details``, or any export passed to
``--input`` -- runs each through the store-category rules, and writes a report
CSV of what each listing should be filed under.

    shoebox plan-store-categories                                  # from BigQuery
    shoebox plan-store-categories --input exports/jsonl/active_listing_details.jsonl
    shoebox plan-store-categories --sport Baseball                 # one branch only

This reports; it does not write to eBay. Applying the plan means pushing
``storeCategoryNames`` on each offer, and creating the categories themselves
first via ``StoresClient`` -- neither of which happens here.
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from shoebox.clients.bigquery import BigQueryClient
from shoebox.settings import get_settings
from shoebox.transforms.store_category_builder import plan_store_category
from shoebox.utils.logging_setup import setup_logging
from shoebox.utils.render_table import render_table

logger = logging.getLogger(__name__)

# Lives in configs/bigquery/queries/.
_PLAN_SQL = "store_category_plan.sql"

_REQUIRED_COLUMNS = ("title",)

# View column -> the item specific to fall back to when reading a raw export
# rather than the flattened view.
_SPECIFIC_FALLBACKS = {
    "team": "Team",
    "sport": "Sport",
    "features": "Features",
    "print_run": "Print Run",
    "autographed": "Autographed",
    "insert_set": "Insert Set",
    "set_name": "Set",
}


def _check_columns(df: pd.DataFrame, source: str) -> pd.DataFrame:
    missing = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"{source} is missing required column(s): {', '.join(missing)}"
        )
    return df


def load_listings_from_bigquery(
    bq_client: BigQueryClient | None = None,
) -> pd.DataFrame:
    """Read the active-listing view -- the default source when no file is given."""
    settings = get_settings()
    bq_client = bq_client or BigQueryClient(settings)
    df = bq_client.run_query(
        sql=_PLAN_SQL,
        params={"dataset": settings.bigquery.ebay_dataset},
        return_df=True,
    )
    return _check_columns(
        df, f"{settings.bigquery.ebay_dataset}.v_active_listing_details"
    )


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
    """A trimmed string, treating a missing value as the empty string it means."""
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


def _joined(value: object) -> str:
    """Flatten a repeated item specific the way the BigQuery view does."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " | ".join(_clean_str(v) for v in value if _clean_str(v))
    return _clean_str(value)


def read_fields(row: pd.Series) -> dict:
    """The rule inputs for one listing, from either shape of export.

    The flattened view columns win when present; otherwise the values come out
    of ``item_specifics``, so the raw JSONL export from
    ``sync-active-listing-details`` works as an ``--input`` without a round
    trip through BigQuery.
    """
    specifics = None
    fields: dict[str, str] = {"title": _clean_str(row.get("title"))}

    for column, specific in _SPECIFIC_FALLBACKS.items():
        value = _joined(row.get(column)) if column in row.index else ""
        if not value:
            if specifics is None:
                specifics = _parse_specifics(row.get("item_specifics"))
            value = _joined(specifics.get(specific))
        fields[column] = value

    # subset_name is a view-only derivation; fall back to the insert set, which
    # is what it is built from.
    subset = _clean_str(row.get("subset_name")) if "subset_name" in row.index else ""
    fields["subset_name"] = subset or fields.get("insert_set", "")
    return fields


def build_category_plan(df: pd.DataFrame) -> pd.DataFrame:
    """Plan a category for every listing carrying a Sport item specific.

    Listings with no Sport are skipped rather than guessed at: the sport picks
    the top-level branch, and a card filed under the wrong one is worse than a
    card left where it is until the item specific is fixed. The caller reports
    how many were dropped by comparing lengths.
    """
    records = []
    for _, row in df.iterrows():
        fields = read_fields(row)
        if not fields["sport"]:
            continue

        plan = plan_store_category(
            title=fields["title"],
            team=fields["team"],
            sport=fields["sport"],
            features=fields["features"],
            subset_name=fields["subset_name"],
            print_run=fields["print_run"],
            autographed=fields["autographed"],
        )

        records.append(
            {
                "item_id": _clean_str(row.get("item_id")).removesuffix(".0"),
                "sku": _clean_str(row.get("sku")),
                "title": fields["title"],
                "sport": plan.sport,
                "team_specific": fields["team"],
                "team_category": plan.team or "",
                "hit": plan.hit or "",
                "is_variation": plan.is_variation,
                "primary_category": plan.primary,
                "secondary_category": plan.secondary or "",
                # The shape the Inventory API wants, ready to eyeball.
                "store_category_names": " | ".join(plan.categories),
                "category_count": len(plan.categories),
                "notes": ",".join(plan.notes),
            }
        )

    return pd.DataFrame.from_records(records)


# Overwritten every run: the working copy to edit and feed to
# `create-store-categories`, alongside the timestamped history.
WORKING_REPORT = "csv/store_categories_working.csv"


def write_report(plan: pd.DataFrame, exports_dir: Path) -> tuple[Path, Path]:
    """Write the timestamped report and refresh the working copy.

    Returns ``(timestamped, working)``. The timestamped file is the record of
    what a given run decided; the working file is the one to hand-edit and feed
    downstream, so it always holds the newest full plan.
    """
    stamp = datetime.now(UTC).strftime("%Y_%m_%d_%H_%M_%S")
    out_path = exports_dir / f"csv/store_categories_{stamp}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plan.to_csv(out_path, index=False)

    working_path = exports_dir / WORKING_REPORT
    plan.to_csv(working_path, index=False)
    return out_path, working_path


def category_counts(plan: pd.DataFrame) -> pd.DataFrame:
    """Every category the plan uses, with how many listings land in it.

    Both slots are counted, so a baseball hit shows up under its team and under
    Hits -- which is the point of the second slot.
    """
    counts: Counter[str] = Counter()
    for names in plan["store_category_names"]:
        for category in str(names).split(" | "):
            if category:
                counts[category] += 1
    return (
        pd.DataFrame(sorted(counts.items()), columns=["category", "listings"])
        .sort_values("listings", ascending=False)
        .reset_index(drop=True)
    )


def _log_summary(plan: pd.DataFrame, counts: pd.DataFrame) -> None:
    from rich.console import Console

    console = Console()
    logger.info("%d listing(s) planned into %d categor(ies)", len(plan), len(counts))
    logger.info("sport branches: %s", plan["sport"].value_counts().to_dict())

    hits = plan[plan["hit"].astype(bool)]
    if not hits.empty:
        logger.info(
            "secondary Hits categories: %s", hits["hit"].value_counts().to_dict()
        )

    notes = plan["notes"]
    note_counts = (
        notes[notes.astype(bool)].str.split(",").explode().value_counts().to_dict()
    )
    if note_counts:
        logger.info("notes: %s", note_counts)

    thin = counts[counts["listings"] < 10]
    if not thin.empty:
        logger.info(
            "%d categor(ies) hold fewer than 10 listings: %s",
            len(thin),
            dict(zip(thin["category"], thin["listings"], strict=True)),
        )

    console.print(
        render_table(counts.head(40), title="Planned store categories (top 40)")
    )


def plan_store_categories(
    *,
    input_path: Path | None = None,
    sport: str | None = None,
) -> pd.DataFrame:
    settings = get_settings()

    if input_path:
        df = load_listings(input_path)
        source = str(input_path)
    else:
        df = load_listings_from_bigquery()
        source = f"BigQuery {settings.bigquery.ebay_dataset}.v_active_listing_details"
    logger.info("Loaded %d listing(s) from %s", len(df), source)

    plan = build_category_plan(df)
    skipped = len(df) - len(plan)
    if skipped:
        logger.info("Skipped %d listing(s) with no Sport item specific", skipped)

    if sport:
        plan = plan[plan["sport"].str.casefold() == sport.casefold()].reset_index(
            drop=True
        )
        logger.info("Filtered to %d %s listing(s)", len(plan), sport)

    counts = category_counts(plan)
    report_path, working_path = write_report(plan, Path(settings.paths.exports_dir))
    _log_summary(plan, counts)
    logger.info("Report written to %s", report_path)
    logger.info("Working copy refreshed at %s", working_path)
    logger.info("Report only — nothing was sent to eBay.")
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="Listings export (.jsonl or .csv)")
    parser.add_argument("--sport", help="Only report one sport branch, e.g. Baseball")
    args = parser.parse_args()

    # `shoebox plan-store-categories` configures logging for us; running this
    # module directly has to do it itself or the summary goes nowhere.
    setup_logging()

    plan_store_categories(
        input_path=Path(args.input) if args.input else None,
        sport=args.sport,
    )


if __name__ == "__main__":
    main()
