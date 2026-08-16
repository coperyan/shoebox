"""Create the planned eBay store categories, without moving any listings.

Reads the working plan written by ``shoebox plan-store-categories``, works out
which of its category paths the store does not have yet, and creates them
parent-first.

    shoebox create-store-categories                 # preview what would be created
    shoebox create-store-categories --apply         # create them

Nothing is created without ``--apply``, and nothing is ever deleted or
renamed -- categories already in the store are left exactly as they are, so
this is safe to re-run. Listings keep whatever categories they have now:
assigning them is a separate step.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import pandas as pd

from shoebox.clients.ebay_rest.client import EbayClient
from shoebox.clients.ebay_rest.stores import MAX_CATEGORY_DEPTH, flatten_store_categories
from shoebox.pipelines.plan_store_categories import WORKING_REPORT
from shoebox.settings import get_settings
from shoebox.utils.logging_setup import setup_logging
from shoebox.utils.render_table import render_table

logger = logging.getLogger(__name__)

# eBay assigns category IDs asynchronously, so a category just created is not
# in the tree immediately. Re-read a few times before giving up on it.
_REFRESH_TRIES = 6
_REFRESH_WAIT_SECONDS = 5


def load_plan(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"No category plan at {path}. Run `shoebox plan-store-categories` first."
        )
    plan = pd.read_csv(path, dtype=str)
    if "store_category_names" not in plan.columns:
        raise ValueError(f"{path} has no store_category_names column.")
    return plan


def desired_paths(plan: pd.DataFrame) -> list[tuple[str, ...]]:
    """Every category path the plan needs, ancestors included, shallowest first.

    ``/Baseball Singles/Chicago Cubs`` implies ``/Baseball Singles``, which has
    to exist before the child can be hung off it.
    """
    paths: set[tuple[str, ...]] = set()
    for names in plan["store_category_names"].dropna():
        for full in str(names).split(" | "):
            segments = tuple(s for s in full.split("/") if s.strip())
            for depth in range(1, len(segments) + 1):
                paths.add(segments[:depth])
    return sorted(paths, key=lambda p: (len(p), p))


def existing_paths(ebay_api: EbayClient) -> dict[tuple[str, ...], str]:
    """Current store tree as ``{path: category_id}``.

    Names are compared case-insensitively: eBay preserves the casing it was
    given, and re-creating a category that differs only in case would split it.
    """
    flat = flatten_store_categories(ebay_api.stores.get_store_categories())
    return {
        tuple(segment.strip().casefold() for segment in entry["path"]): str(entry["category_id"])
        for entry in flat
    }


def _key(path: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(segment.strip().casefold() for segment in path)


def plan_creations(
    plan: pd.DataFrame, existing: dict[tuple[str, ...], str]
) -> list[tuple[str, ...]]:
    """Paths the store is missing, shallowest first so parents come first."""
    missing = [path for path in desired_paths(plan) if _key(path) not in existing]

    too_deep = [p for p in missing if len(p) > MAX_CATEGORY_DEPTH]
    if too_deep:
        raise ValueError(
            f"eBay allows {MAX_CATEGORY_DEPTH} category levels; "
            f"plan wants deeper: {['/'.join(p) for p in too_deep]}"
        )
    return missing


def _refresh_until_present(
    ebay_api: EbayClient, expected: list[tuple[str, ...]]
) -> dict[tuple[str, ...], str]:
    """Re-read the tree until every expected path shows up, or tries run out."""
    existing = existing_paths(ebay_api)
    for attempt in range(1, _REFRESH_TRIES + 1):
        pending = [p for p in expected if _key(p) not in existing]
        if not pending:
            return existing
        logger.info(
            "Waiting for %d new categor(ies) to appear (attempt %d/%d)",
            len(pending),
            attempt,
            _REFRESH_TRIES,
        )
        time.sleep(_REFRESH_WAIT_SECONDS)
        existing = existing_paths(ebay_api)
    return existing


def create_categories(
    missing: list[tuple[str, ...]],
    *,
    ebay_api: EbayClient,
    existing: dict[tuple[str, ...], str],
) -> pd.DataFrame:
    """Create the missing categories one level at a time, parents first.

    Each level is created, then the tree is re-read so the next level can
    address its parents by the IDs eBay assigned. A failure on one category is
    recorded and the run continues -- one bad name should not strand the rest.
    """
    records: list[dict[str, object]] = []
    by_depth: dict[int, list[tuple[str, ...]]] = {}
    for path in missing:
        by_depth.setdefault(len(path), []).append(path)

    for depth in sorted(by_depth):
        level = by_depth[depth]
        logger.info("Creating %d categor(ies) at level %d", len(level), depth)

        created_here: list[tuple[str, ...]] = []
        for path in level:
            name = path[-1]
            parent_id = existing.get(_key(path[:-1])) if depth > 1 else None
            if depth > 1 and parent_id is None:
                # Its parent failed earlier in this run; skip rather than
                # creating an orphan at the top level.
                logger.warning("Skipping /%s: parent was not created", "/".join(path))
                records.append(
                    {"path": "/" + "/".join(path), "status": "skipped", "error": "no parent"}
                )
                continue
            try:
                ebay_api.stores.add_store_category(name, parent_category_id=parent_id)
                records.append({"path": "/" + "/".join(path), "status": "created", "error": ""})
                created_here.append(path)
            except Exception as e:
                logger.warning("Failed to create /%s: %s", "/".join(path), e)
                records.append(
                    {"path": "/" + "/".join(path), "status": "failed", "error": str(e)}
                )

        if created_here:
            existing = _refresh_until_present(ebay_api, created_here)

    return pd.DataFrame.from_records(
        records, columns=["path", "status", "error"]
    )


def create_store_categories(
    *,
    plan_path: Path | None = None,
    apply: bool = False,
    ebay_api: EbayClient | None = None,
) -> pd.DataFrame:
    settings = get_settings()
    plan_path = plan_path or Path(settings.paths.exports_dir) / WORKING_REPORT
    plan = load_plan(plan_path)
    logger.info("Read %d planned listing(s) from %s", len(plan), plan_path)

    ebay_api = ebay_api or EbayClient()
    existing = existing_paths(ebay_api)
    logger.info("Store currently has %d categor(ies)", len(existing))

    missing = plan_creations(plan, existing)
    wanted = desired_paths(plan)
    logger.info(
        "Plan needs %d categor(ies); %d already exist, %d to create",
        len(wanted),
        len(wanted) - len(missing),
        len(missing),
    )

    preview = pd.DataFrame(
        {"path": ["/" + "/".join(p) for p in missing], "level": [len(p) for p in missing]}
    )
    if not preview.empty:
        from rich.console import Console

        Console().print(render_table(preview, title="Store categories to create"))

    if not apply:
        logger.info("Preview only — re-run with --apply to create these in your store.")
        return preview

    if not missing:
        logger.info("Nothing to create; the store already has every planned category.")
        return preview

    results = create_categories(missing, ebay_api=ebay_api, existing=existing)
    counts = results["status"].value_counts().to_dict()
    logger.info("Category creation: %s", counts)
    logger.info("No listings were moved — assigning them is a separate step.")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan",
        help=f"Category plan CSV; defaults to <exports_dir>/{WORKING_REPORT}",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Create the categories (default: preview only)"
    )
    args = parser.parse_args()

    setup_logging()

    create_store_categories(
        plan_path=Path(args.plan) if args.plan else None,
        apply=args.apply,
    )


if __name__ == "__main__":
    main()
