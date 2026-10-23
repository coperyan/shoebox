"""Ad hoc: re-file active listings into the store categories the rules say.

Reads the active listings from BigQuery (``<ebay_dataset>.v_active_listing_details``
via ``store_category_plan.sql``), runs each through
:func:`shoebox.transforms.store_category_builder.plan_store_category` -- the
same rules ``create-listings`` now uses -- and compares the result with what the
listing is *currently* filed under. Only listings whose categories differ are
touched.

    python scripts/recategorize_active_listings.py                    # preview everything
    python scripts/recategorize_active_listings.py --since 2026-10-14 # recent runs only
    python scripts/recategorize_active_listings.py --sport Baseball --apply
    python scripts/recategorize_active_listings.py --apply --limit 50
    python scripts/recategorize_active_listings.py --input exports/csv/active.csv
    python scripts/recategorize_active_listings.py --from-plan exports/csv/recategorize_active_listings_working.csv --apply

Nothing is sent to eBay without ``--apply``. Every run writes
``exports/csv/recategorize_active_listings_<stamp>.csv`` with one row per
listing: current categories, planned categories, and (with ``--apply``) what
happened -- and refreshes ``recategorize_active_listings_working.csv``, the
copy to hand-edit and apply later with ``--from-plan``. A saved plan is applied
as written: nothing is re-planned or re-read from eBay, so delete or edit rows
to control exactly what moves (``change`` must be ``True`` for a row to be
pushed; ``store_category_names`` is what gets sent, ``category_ids`` is the
Trading route for SKU-less rows).

**Where "current" comes from.** For a listing with a SKU the categories are
read off its live offer (``getOffers``), which is cheap and exact; a legacy
name like ``Parallels`` that no longer exists in the store tree is what eBay
shows as "Other". A listing without a SKU has no offer, so its current
categories come from the BigQuery snapshot's ``store_category_id`` /
``store_category2_id`` (captured by ``sync-active-listing-details`` from
GetItem's ``Storefront`` node); when the snapshot predates that capture they
are unknown and the listing is pushed regardless.

**What is held back.** A listing whose title names a different sport than its
Team / Sport specifics resolve to (``title_sport_conflict`` in ``notes``) is
reported, not pushed: the rule would file it by the wrong team. Fix the item
specific, then re-run.

**What gets pushed.** The full replacement list, primary then Hits, so a stale
or hand-set second category is overwritten. Use ``shoebox
assign-store-categories`` instead if hand-curated Hits must survive.
"""

from __future__ import annotations

import argparse
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from shoebox.clients.ebay.client import EbayClient
from shoebox.clients.ebay.stores import flatten_store_categories
from shoebox.pipelines.plan_store_categories import (
    build_category_plan,
    load_listings,
    load_listings_from_bigquery,
)
from shoebox.services.listings import ListingService
from shoebox.settings import get_settings
from shoebox.transforms.store_category_builder import MAX_STORE_CATEGORIES
from shoebox.utils.logging_setup import setup_logging
from shoebox.utils.render_table import render_table
from shoebox.utils.slack import notify_best_effort

logger = logging.getLogger(__name__)

# eBay's catch-all store category, where unmatched names end up.
_OTHER_CATEGORY_ID = "1"

# getOffers is one call per SKU and pure network wait; overlap them.
_DEFAULT_WORKERS = 8

_SEP = " | "

# How getOffers reports an empty second slot.
_EMPTY_NAME = "none"


def _key(path: str) -> str:
    """Comparable form of a category path.

    getOffers hands names back without the leading slash the Inventory API
    wants on the way in (``Baseball Singles/Chicago Cubs``), so the slash,
    case and stray whitespace are all ignored.
    """
    parts = [s.strip() for s in str(path).strip().split("/") if s.strip()]
    return "/".join(parts).casefold()


def _names(joined: str) -> list[str]:
    """The category names in a ``" | "``-joined string, minus empty slots."""
    out = []
    for name in str(joined).split(_SEP):
        if name.strip() and name.strip().casefold() != _EMPTY_NAME:
            out.append(name.strip())
    return out


def live_store_tree(ebay_api: EbayClient) -> tuple[dict[str, str], dict[str, str]]:
    """``({path_key: id}, {id: '/path'})`` from the live store category tree."""
    flat = flatten_store_categories(ebay_api.stores.get_store_categories())
    by_path: dict[str, str] = {}
    by_id: dict[str, str] = {}
    for entry in flat:
        path = "/" + "/".join(s.strip() for s in entry["path"])
        cid = str(entry["category_id"])
        by_path[_key(path)] = cid
        by_id[cid] = path
    return by_path, by_id


def _filter(plan: pd.DataFrame, df: pd.DataFrame, *, sport: str | None, since: str | None):
    """Narrow the plan to one sport branch and/or listings started on or after a date."""
    out = plan[plan["is_variation"].astype(str).str.lower() != "true"]
    if sport:
        out = out[out["sport"].str.casefold() == sport.casefold()]
    if since and "start_date" in df.columns:
        started = pd.to_datetime(df["start_date"], errors="coerce")
        recent = set(df.loc[started >= pd.Timestamp(since), "item_id"].astype(str).str.strip())
        out = out[out["item_id"].astype(str).isin(recent)]
    return out.reset_index(drop=True)


def _snapshot_categories(df: pd.DataFrame) -> dict[str, list[str]]:
    """``{item_id: [category_id, ...]}`` from the BigQuery snapshot, where known."""
    if "store_category_id" not in df.columns:
        return {}
    out: dict[str, list[str]] = {}
    for _, row in df.iterrows():
        ids = [
            str(row.get(col)).removesuffix(".0").strip()
            for col in ("store_category_id", "store_category2_id")
            if pd.notna(row.get(col)) and str(row.get(col)).strip()
        ]
        if ids:
            out[str(row.get("item_id")).removesuffix(".0").strip()] = ids
    return out


def read_current_categories(
    plan: pd.DataFrame,
    *,
    ebay_api: EbayClient,
    snapshot: dict[str, list[str]],
    id_to_path: dict[str, str],
    workers: int = _DEFAULT_WORKERS,
) -> tuple[list[str], list[str]]:
    """Each listing's current categories and where that came from.

    Returns ``(current, source)`` aligned with ``plan``; ``current`` is the
    ``" | "``-joined path list, empty when unknown. ``source`` is ``offer``,
    ``snapshot`` or ``unknown``.
    """

    def from_offer(sku: str) -> list[str] | None:
        offer = ebay_api.inventory.find_offer(sku)
        if offer is None:
            return None
        return [str(n) for n in offer.store_category_names]

    skus = [str(s).strip() for s in plan["sku"]]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        offer_names = list(pool.map(lambda s: from_offer(s) if s else None, skus))

    current: list[str] = []
    source: list[str] = []
    for (_, row), names in zip(plan.iterrows(), offer_names, strict=True):
        if names is not None:
            current.append(_SEP.join(names))
            source.append("offer")
            continue
        ids = snapshot.get(str(row["item_id"]))
        if ids:
            current.append(_SEP.join(id_to_path.get(i, f"#{i}") for i in ids))
            source.append("snapshot")
        else:
            current.append("")
            source.append("unknown")
    return current, source


def _needs_update(current: str, planned: str) -> bool:
    if not _names(current):
        return True  # unknown: push to be sure
    return [_key(c) for c in _names(current)] != [_key(c) for c in _names(planned)]


def build_changes(
    plan: pd.DataFrame,
    *,
    ebay_api: EbayClient,
    snapshot: dict[str, list[str]],
    workers: int = _DEFAULT_WORKERS,
) -> pd.DataFrame:
    """The plan with current categories, a ``change`` flag and resolved IDs."""
    path_to_id, id_to_path = live_store_tree(ebay_api)
    current, source = read_current_categories(
        plan, ebay_api=ebay_api, snapshot=snapshot, id_to_path=id_to_path, workers=workers
    )
    out = plan.assign(current_categories=current, current_source=source)
    out["change"] = [
        _needs_update(c, p)
        for c, p in zip(out["current_categories"], out["store_category_names"], strict=True)
    ]

    # A planned path that does not exist in the store yet cannot be assigned;
    # flag it rather than let eBay file the listing under Other again.
    missing: list[str] = []
    ids: list[str] = []
    for names in out["store_category_names"]:
        paths = _names(names)[:MAX_STORE_CATEGORIES]
        resolved = [path_to_id.get(_key(p)) for p in paths]
        missing.append(_SEP.join(p for p, r in zip(paths, resolved, strict=True) if r is None))
        ids.append(_SEP.join(r for r in resolved if r))
    out["missing_categories"] = missing
    out["category_ids"] = ids
    # "In Other" means eBay shows the listing there: every name it carries is
    # either the Other category itself or a name the tree no longer has.
    out["in_other"] = out["current_categories"].map(
        lambda c: (
            bool(_names(c))
            and all(
                path_to_id.get(_key(p), _OTHER_CATEGORY_ID) == _OTHER_CATEGORY_ID for p in _names(c)
            )
        )
    )
    return out


def live_prices(ebay_api: EbayClient) -> dict[str, float]:
    """``{item_id: price}`` as eBay shows each active listing right now.

    Read once per run from GetMyeBaySelling. The inventory route PUTs the
    whole offer, and an offer's own price is stale for any listing repriced
    in Seller Hub, so every update carries this instead.
    """
    out: dict[str, float] = {}
    for listing in ebay_api.trading.get_active_listings():
        try:
            out[str(listing["item_id"])] = float(listing["price"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def apply_changes(
    changes: pd.DataFrame, *, ebay_api: EbayClient, limit: int | None = None
) -> pd.DataFrame:
    """Push every flagged listing, carrying on past individual failures."""
    targets = changes[
        changes["change"] & (changes["missing_categories"] == "") & ~_sport_conflict(changes)
    ]
    if limit is not None:
        targets = targets.head(limit)

    prices = live_prices(ebay_api)
    logger.info("Read live prices for %d active listing(s)", len(prices))
    listings = ListingService(ebay_api)
    status: dict[int, str] = {}
    error: dict[int, str] = {}
    total = len(targets)
    for n, (idx, row) in enumerate(targets.iterrows(), 1):
        sku = str(row["sku"]).strip()
        item_id = str(row["item_id"]).strip()
        categories = _names(row["store_category_names"])
        category_ids = [i for i in str(row["category_ids"]).split(_SEP) if i.strip()]
        try:
            live_price = prices.get(item_id)
            if sku and live_price is None:
                raise ValueError("no live price from GetMyeBaySelling; is the listing active?")
            outcome = listings.update_store_categories(
                categories=categories,
                category_ids=category_ids,
                sku=sku or None,
                item_id=item_id or None,
                live_price=live_price,
            )
            status[idx] = "skipped" if outcome.get("skipped") else "updated"
            error[idx] = ""
        except Exception as e:  # noqa: BLE001 - one bad listing must not stop the sweep
            logger.warning("Failed sku=%s item_id=%s: %s", sku or "-", item_id or "-", e)
            status[idx] = "failed"
            error[idx] = str(e)
        if n % 25 == 0 or n == total:
            logger.info("Processed %d of %d listing(s)", n, total)

    out = changes.copy()
    out["status"] = pd.Series(status).reindex(out.index).fillna("")
    out["error"] = pd.Series(error).reindex(out.index).fillna("")
    out.loc[out["status"] == "", "status"] = out.loc[out["status"] == "", "change"].map(
        lambda c: "pending" if c else "current"
    )
    out.loc[out["missing_categories"] != "", "status"] = "missing_category"
    out.loc[_sport_conflict(out) & out["change"], "status"] = "sport_conflict"
    return out


def _sport_conflict(changes: pd.DataFrame) -> pd.Series:
    """Rows whose title names a different sport than their team/Sport resolved to.

    The rule would file them by team (a Bowman Football "New York Giants" card
    lands with the MLB Giants); fix the listing's Sport or Team specific
    instead of pushing a wrong category.
    """
    return changes["notes"].astype(str).str.contains("title_sport_conflict")


def _summarize(changes: pd.DataFrame) -> None:
    from rich.console import Console

    to_change = changes[changes["change"]]
    logger.info(
        "%d active listing(s): %d already current, %d to change (%d currently in Other, "
        "%d with unknown current categories)",
        len(changes),
        int((~changes["change"]).sum()),
        len(to_change),
        int(changes["in_other"].sum()),
        int((changes["current_source"] == "unknown").sum()),
    )
    conflicts = changes[_sport_conflict(changes) & changes["change"]]
    if not conflicts.empty:
        logger.warning(
            "%d listing(s) have a title/sport conflict and will not be pushed; fix their Sport "
            "or Team item specific: %s",
            len(conflicts),
            "; ".join(
                f"{i} {t[:60]}"
                for i, t in zip(conflicts["item_id"], conflicts["title"], strict=True)
            ),
        )
    missing = changes[changes["missing_categories"] != ""]
    if not missing.empty:
        logger.warning(
            "%d listing(s) plan into categories missing from the store tree: %s",
            len(missing),
            missing["missing_categories"].value_counts().to_dict(),
        )
    if to_change.empty:
        return
    summary = (
        to_change.groupby(["current_categories", "store_category_names"])
        .size()
        .reset_index(name="listings")
        .sort_values("listings", ascending=False)
        .rename(columns={"current_categories": "from", "store_category_names": "to"})
    )
    Console().print(render_table(summary.head(40), title="Planned moves (top 40)"))


# Overwritten every run: the saved plan to review, edit and apply later.
WORKING_PLAN = "csv/recategorize_active_listings_working.csv"

_PLAN_COLUMNS = ("item_id", "sku", "store_category_names", "change", "category_ids")


def load_saved_plan(path: Path) -> pd.DataFrame:
    """A previous run's CSV, ready for :func:`apply_changes` as written."""
    if not path.exists():
        raise FileNotFoundError(f"No saved plan at {path}")
    plan = pd.read_csv(path, dtype=str).fillna("")
    missing = [c for c in _PLAN_COLUMNS if c not in plan.columns]
    if missing:
        raise ValueError(f"{path} is missing column(s): {', '.join(missing)}")
    plan["change"] = plan["change"].astype(str).str.lower() == "true"
    for col, default in (("missing_categories", ""), ("notes", ""), ("in_other", "False")):
        if col not in plan.columns:
            plan[col] = default
    plan["in_other"] = plan["in_other"].astype(str).str.lower() == "true"
    plan["current_source"] = plan.get("current_source", pd.Series("", index=plan.index))
    # Rows already applied in an earlier pass are not pushed again.
    if "status" in plan.columns:
        done = plan["status"].isin(["updated", "skipped"])
        plan.loc[done, "change"] = False
    return plan


def recategorize_active_listings(
    *,
    input_path: Path | None = None,
    plan_path: Path | None = None,
    sport: str | None = None,
    since: str | None = None,
    apply: bool = False,
    limit: int | None = None,
    workers: int = _DEFAULT_WORKERS,
    ebay_api: EbayClient | None = None,
) -> pd.DataFrame:
    settings = get_settings()
    ebay_api = ebay_api or EbayClient()

    if plan_path:
        changes = load_saved_plan(plan_path)
        if sport:
            changes = changes[changes["sport"].str.casefold() == sport.casefold()]
        logger.info(
            "Loaded saved plan %s: %d listing(s), %d flagged to change",
            plan_path,
            len(changes),
            int(changes["change"].sum()),
        )
    else:
        df = load_listings(input_path) if input_path else load_listings_from_bigquery()
        logger.info("Loaded %d active listing(s)", len(df))

        plan = build_category_plan(df)
        plan = _filter(plan, df, sport=sport, since=since)
        logger.info("%d listing(s) in scope after filters", len(plan))

        changes = build_changes(
            plan, ebay_api=ebay_api, snapshot=_snapshot_categories(df), workers=workers
        )
    _summarize(changes)

    if apply:
        changes = apply_changes(changes, ebay_api=ebay_api, limit=limit)
        counts = changes["status"].value_counts().to_dict()
        logger.info("Applied: %s", counts)
        updated = int((changes["status"] == "updated").sum())
        failed = int((changes["status"] == "failed").sum())
        notify_best_effort(
            settings.slack.notify_channel,
            f"Recategorized {updated} active listing(s){f', {failed} failed' if failed else ''}.",
        )
    else:
        logger.info("Preview only -- re-run with --apply to push the changes.")

    stamp = datetime.now(UTC).strftime("%Y_%m_%d_%H_%M_%S")
    out_path = Path(settings.paths.exports_dir) / f"csv/recategorize_active_listings_{stamp}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    changes.to_csv(out_path, index=False)
    logger.info("Written to %s", out_path)
    working_path = Path(settings.paths.exports_dir) / WORKING_PLAN
    changes.to_csv(working_path, index=False)
    logger.info("Working plan refreshed at %s (apply later with --from-plan)", working_path)
    return changes


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input", help="Listings export (.jsonl or .csv) instead of BigQuery")
    parser.add_argument(
        "--from-plan",
        help="Apply a saved run's CSV as written (no re-planning, no offer reads)",
    )
    parser.add_argument("--sport", help="Only one sport branch, e.g. Baseball")
    parser.add_argument("--since", help="Only listings started on/after this date (YYYY-MM-DD)")
    parser.add_argument("--apply", action="store_true", help="Push the changes to eBay")
    parser.add_argument("--limit", type=int, help="Cap how many listings are updated")
    parser.add_argument("--workers", type=int, default=_DEFAULT_WORKERS)
    args = parser.parse_args()

    setup_logging()
    recategorize_active_listings(
        input_path=Path(args.input) if args.input else None,
        plan_path=Path(args.from_plan) if args.from_plan else None,
        sport=args.sport,
        since=args.since,
        apply=args.apply,
        limit=args.limit,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
