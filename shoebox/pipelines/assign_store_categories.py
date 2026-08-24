"""Move live listings into their planned store categories.

Reads the working plan from ``shoebox plan-store-categories`` and pushes each
listing's team category onto the live listing. Defaults to baseball, which is
the branch broken out by team.

    shoebox assign-store-categories                       # preview baseball
    shoebox assign-store-categories --apply --limit 25    # push the first 25
    shoebox assign-store-categories --sport Basketball    # a different branch
    shoebox assign-store-categories --with-hits           # push planned Hits too

Nothing is sent to eBay without ``--apply``.

**On the second category slot.** ``storeCategoryNames`` is a full replacement,
not a merge, so pushing just the team category would wipe anything else the
listing is filed under -- including Hits categories set by hand. By default the
listing's *current* second category is read off the offer and carried across
untouched, so hand-curated Hits survive. ``--with-hits`` instead pushes the
Hits category the plan worked out, overwriting whatever is there.

Listings with no SKU go through Trading, which addresses categories by numeric
ID rather than name; those IDs are resolved from the live store tree, and a
listing whose category does not exist yet is skipped rather than guessed at.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from shoebox.clients.ebay_rest.client import EbayClient
from shoebox.clients.ebay_rest.stores import flatten_store_categories
from shoebox.pipelines.plan_store_categories import WORKING_REPORT
from shoebox.settings import get_settings
from shoebox.transforms.store_category_builder import HITS_PARENT
from shoebox.utils.logging_setup import setup_logging
from shoebox.utils.render_table import render_table
from shoebox.utils.slack import notify_best_effort

logger = logging.getLogger(__name__)

# eBay caps a listing at two store categories.
_MAX_CATEGORIES = 2

# Branch holding the secondary categories, used to spot the ones already on a
# listing so they can be carried across. Derived from the builder rather than
# spelled out again: the two silently drifted apart once already, which made
# every preservation lookup miss and quietly drop the second category.
_HITS_PREFIX = f"/{HITS_PARENT}/"


def load_plan(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"No category plan at {path}. Run `shoebox plan-store-categories` first."
        )
    plan = pd.read_csv(path, dtype=str).fillna("")
    missing = [c for c in ("primary_category",) if c not in plan.columns]
    if missing:
        raise ValueError(f"{path} is missing required column(s): {', '.join(missing)}")
    return plan


def category_ids(ebay_api: EbayClient) -> dict[str, str]:
    """Live store tree as ``{'/path': category_id}``, matched case-insensitively."""
    flat = flatten_store_categories(ebay_api.stores.get_store_categories())
    return {
        "/" + "/".join(s.strip() for s in entry["path"]).casefold(): str(entry["category_id"])
        for entry in flat
    }


def _path_key(path: str) -> str:
    return path.strip().casefold()


def build_assignments(
    plan: pd.DataFrame,
    *,
    sport: str | None = "Baseball",
    with_hits: bool = False,
) -> pd.DataFrame:
    """One row per listing to move, with the categories it should end up in.

    Variation listings are left out: they are already in the You Pick branch
    and have no team.
    """
    rows = plan
    if sport:
        rows = rows[rows["sport"].str.casefold() == sport.casefold()]
    rows = rows[rows["is_variation"].astype(str).str.lower() != "true"]

    records = []
    for _, row in rows.iterrows():
        primary = str(row.get("primary_category") or "").strip()
        if not primary:
            continue
        planned_secondary = str(row.get("secondary_category") or "").strip()
        records.append(
            {
                "item_id": str(row.get("item_id") or "").strip(),
                "sku": str(row.get("sku") or "").strip(),
                "title": str(row.get("title") or "").strip(),
                "sport": str(row.get("sport") or "").strip(),
                "primary_category": primary,
                "planned_secondary": planned_secondary,
                # Filled in per listing at apply time when not --with-hits,
                # because it depends on what the listing currently carries.
                "secondary_category": planned_secondary if with_hits else "",
                "update_method": ("inventory" if str(row.get("sku") or "").strip() else "trading"),
            }
        )
    return pd.DataFrame.from_records(records)


def _current_secondary(ebay_api: EbayClient, sku: str) -> str:
    """The Hits category a listing already carries, if any.

    Read straight off the offer so hand-curated categories survive a run that
    is only meant to set the team.
    """
    offers = ebay_api.api.sell_inventory_get_offers(sku=sku)
    records = [x["record"] for x in offers if "record" in x]
    if not records:
        return ""
    for name in records[0].get("store_category_names") or []:
        if str(name).startswith(_HITS_PREFIX):
            return str(name)
    return ""


def apply_assignments(
    assignments: pd.DataFrame,
    *,
    ebay_api: EbayClient,
    with_hits: bool = False,
    limit: int | None = None,
) -> pd.DataFrame:
    """Push each listing's categories, carrying on past individual failures."""
    targets = assignments.copy()
    if limit is not None:
        targets = targets.head(limit)

    ids = category_ids(ebay_api)
    statuses: list[str] = []
    errors: list[str] = []
    finals: list[str] = []
    total = len(targets)

    for idx, (_, row) in enumerate(targets.iterrows(), 1):
        sku = row["sku"]
        item_id = row["item_id"]
        categories = [row["primary_category"]]

        try:
            secondary = row["secondary_category"]
            if not with_hits and sku:
                # Preserve whatever Hits category is already on the listing.
                secondary = _current_secondary(ebay_api, sku)
            if secondary and secondary not in categories:
                categories.append(secondary)
            categories = categories[:_MAX_CATEGORIES]

            resolved = [ids.get(_path_key(c)) for c in categories]
            if not sku and any(r is None for r in resolved):
                missing = [c for c, r in zip(categories, resolved, strict=True) if r is None]
                raise ValueError(f"store category not found: {', '.join(missing)}")

            outcome = ebay_api.update_listing_store_categories(
                categories=categories,
                category_ids=[r for r in resolved if r],
                sku=sku or None,
                item_id=item_id or None,
            )
            statuses.append("skipped" if outcome.get("skipped") else "updated")
            errors.append("")
            finals.append(" | ".join(categories))
        except Exception as e:
            logger.warning(
                "Failed to recategorize sku=%s item_id=%s: %s",
                sku or "-",
                item_id or "-",
                e,
            )
            statuses.append("failed")
            errors.append(str(e))
            finals.append("")

        if idx % 25 == 0 or idx == total:
            logger.info("Processed %d of %d listing(s)", idx, total)

    return targets.assign(status=statuses, applied_categories=finals, error=errors)


def _log_summary(assignments: pd.DataFrame) -> None:
    from rich.console import Console

    logger.info("%d listing(s) to move", len(assignments))
    logger.info("update route: %s", assignments["update_method"].value_counts().to_dict())

    counts = (
        assignments["primary_category"]
        .value_counts()
        .rename_axis("category")
        .reset_index(name="listings")
    )
    Console().print(render_table(counts.head(40), title="Listings per category"))


def assign_store_categories(
    *,
    plan_path: Path | None = None,
    sport: str | None = "Baseball",
    with_hits: bool = False,
    apply: bool = False,
    limit: int | None = None,
    ebay_api: EbayClient | None = None,
) -> pd.DataFrame:
    settings = get_settings()
    plan_path = plan_path or Path(settings.paths.exports_dir) / WORKING_REPORT
    plan = load_plan(plan_path)
    logger.info("Read %d planned listing(s) from %s", len(plan), plan_path)

    assignments = build_assignments(plan, sport=sport, with_hits=with_hits)
    _log_summary(assignments)

    if not apply:
        logger.info(
            "Preview only — re-run with --apply to move these listings. "
            "Existing Hits categories are %s.",
            "overwritten from the plan" if with_hits else "preserved",
        )
        return assignments

    ebay_api = ebay_api or EbayClient()
    results = apply_assignments(assignments, ebay_api=ebay_api, with_hits=with_hits, limit=limit)

    counts = results["status"].value_counts().to_dict()
    out_path = plan_path.with_name(plan_path.stem + "_applied.csv")
    results.to_csv(out_path, index=False)
    logger.info("Recategorized: %s", counts)
    logger.info("Results written to %s", out_path)

    updated = int((results["status"] == "updated").sum())
    failed = int((results["status"] == "failed").sum())
    notify_best_effort(
        settings.slack.notify_channel,
        f"Moved {updated} listing(s) into store categories"
        f"{f', {failed} failed' if failed else ''}.",
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", help="Category plan CSV; defaults to the working copy")
    parser.add_argument(
        "--sport",
        default="Baseball",
        help="Sport branch to move (default: Baseball); pass 'all' for every sport",
    )
    parser.add_argument(
        "--with-hits",
        action="store_true",
        help="Also push the planned Hits category, overwriting any set by hand",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Push the changes to eBay (default: preview only)",
    )
    parser.add_argument("--limit", type=int, help="Cap how many listings are moved")
    args = parser.parse_args()

    setup_logging()

    assign_store_categories(
        plan_path=Path(args.plan) if args.plan else None,
        sport=None if str(args.sport).casefold() == "all" else args.sport,
        with_hits=args.with_hits,
        apply=args.apply,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
