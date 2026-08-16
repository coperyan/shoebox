"""Scratch harness: apply the store category plan, then copy the log to OneDrive.

Kept out of ``shoebox/pipelines/assign_store_categories.py`` on purpose. A live
``apply=True`` call at a module's top level fires on *import*, which means the
CLI, pytest, and any ad-hoc ``import`` all push changes to eBay as a side
effect. Here it only runs when this file is executed directly.

    python scripts/run_store_categories.py --sport all --with-hits --apply

Same flags as ``shoebox assign-store-categories``; the only thing this adds is
the OneDrive copy at the end.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from shoebox.pipelines.assign_store_categories import assign_store_categories
from shoebox.settings import get_settings
from shoebox.utils.logging_setup import setup_logging

ONEDRIVE_COPY = Path.home() / "OneDrive" / "store_categories_working_applied.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sport",
        default="Baseball",
        help="Sport branch to move (default: Baseball); pass 'all' for every sport",
    )
    parser.add_argument("--with-hits", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    setup_logging()

    assign_store_categories(
        sport=None if str(args.sport).casefold() == "all" else args.sport,
        with_hits=args.with_hits,
        apply=args.apply,
        limit=args.limit,
    )

    if not args.apply:
        return

    applied = Path(get_settings().paths.exports_dir) / "csv/store_categories_working_applied.csv"
    if applied.exists():
        shutil.copy(applied, ONEDRIVE_COPY)
        print(f"Copied {applied} -> {ONEDRIVE_COPY}")


if __name__ == "__main__":
    main()
