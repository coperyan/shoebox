from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from shoebox.settings import ensure_runtime_dirs
from shoebox.utils.logging_setup import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="shoebox")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ## Sync Metadata
    sub.add_parser("sync-metadata", help="Load checklist/parallels into GCS & BigQuery")

    sub.add_parser("end-oos-listings", help="Ends active listings with no stock available.")

    ## Create listing queue from excel
    sub.add_parser(
        "create-queue-excel",
        help="Converts Inputs.xlsm into JSONL format created by UI..",
    )

    ## UI
    sub.add_parser("ui", help="Opens streamlit UI to create new listings..")

    ## Create Listings
    p_list = sub.add_parser("create-listings", help="Create/update offers and publish listings")
    p_list.add_argument("--dry-run", action="store_true")
    p_list.add_argument("--publish", action="store_true")
    p_list.add_argument("--schedule", action="store_true")
    p_list.add_argument("--scrape-prices", action="store_true")

    ## Orders Awaiting Shipment
    orders = sub.add_parser("orders-awaiting-shipment", help="Display orders awaiting shipment")
    orders.add_argument("--pull-order", action="store_true")
    orders.add_argument("--buyer-order", action="store_true")
    orders.add_argument("--display", action="store_true")
    orders.add_argument("--message", action="store_true")

    ##Active Listings
    sub.add_parser("sync-active-listings", help="Update active listings in GCS/BigQuery..")

    # Active Listing Details
    p_details = sub.add_parser(
        "sync-active-listing-details",
        help="Update active listing details in GCS/BigQuery..",
    )
    p_details.add_argument(
        "--workers",
        type=int,
        help="Concurrent GetItem calls (default 12); lower it if eBay starts throttling",
    )

    # Orders
    sub.add_parser("sync-orders", help="Sync orders in GCS/BigQuery..")

    # Slack bot
    sub.add_parser(
        "slack-bot",
        help="Start the Slack bot service to run CLI commands via chat messages",
    )

    # Create variation ("You Pick") listings from an inventory workbook
    p_var = sub.add_parser(
        "create-variation-listings",
        help="Create multi-variation (You Pick) listings from an inventory Excel workbook",
    )
    p_var.add_argument("--excel-path", required=True, help="Inventory workbook (.xlsx)")
    p_var.add_argument("--sheet-name", default="Checklist")
    p_var.add_argument("--dry-run", action="store_true")
    p_var.add_argument("--publish", action="store_true")
    p_var.add_argument("--schedule", action="store_true")
    p_var.add_argument("--in-stock-only", action="store_true")
    p_var.add_argument("--images-dir", help="Directory of per-card scans (optional)")
    p_var.add_argument("--default-image-path", help="Hero image for the listing (optional)")

    # Relist listings
    p_relist = sub.add_parser(
        "relist-listings", help="Relist aged listings with Slack price confirmation"
    )
    p_relist.add_argument("--schedule", action="store_true")
    p_relist.add_argument("--no-scrape", action="store_true")
    p_relist.add_argument("--dry-run", action="store_true")

    # Send offers to watchers
    p_offers = sub.add_parser(
        "send-offers", help="Send negotiation offers to eligible watchers via Slack prompts"
    )
    p_offers.add_argument("--dry-run", action="store_true")
    p_offers.add_argument(
        "--auto",
        action="store_true",
        help="Send discount-matrix offers headlessly instead of prompting via Slack",
    )
    p_offers.add_argument(
        "--timeout-s",
        type=int,
        default=900,
        help="Seconds to wait for Slack replies before unanswered prompts expire (default 900)",
    )

    # Enhance titles on live listings
    p_titles = sub.add_parser(
        "enhance-listing-titles",
        help="Add the team and expand RC/AU shorthand in existing listing titles",
    )
    p_titles.add_argument(
        "--input",
        help=(
            "Listings export to read (.jsonl or .csv); "
            "defaults to the BigQuery view ebay.v_active_listing_details"
        ),
    )
    p_titles.add_argument(
        "--apply", action="store_true", help="Push the new titles to eBay (default: preview only)"
    )
    p_titles.add_argument("--limit", type=int, help="Cap how many listings are updated")

    # Plan the store category tree for live listings
    p_cats = sub.add_parser(
        "plan-store-categories",
        help="Report the store category each active listing should be filed under",
    )
    p_cats.add_argument(
        "--input",
        help=(
            "Listings export to read (.jsonl or .csv); "
            "defaults to the BigQuery view ebay.v_active_listing_details"
        ),
    )
    p_cats.add_argument("--sport", help="Only report one sport branch, e.g. Baseball")

    # Create the planned store categories on eBay
    p_mkcats = sub.add_parser(
        "create-store-categories",
        help="Create the planned store categories in your eBay store (no listings move)",
    )
    p_mkcats.add_argument("--plan", help="Category plan CSV; defaults to the working copy")
    p_mkcats.add_argument(
        "--apply", action="store_true", help="Create them (default: preview only)"
    )

    # Move live listings into their planned store categories
    p_assign = sub.add_parser(
        "assign-store-categories",
        help="Move live listings into their planned store categories",
    )
    p_assign.add_argument("--plan", help="Category plan CSV; defaults to the working copy")
    p_assign.add_argument(
        "--sport", default="Baseball", help="Sport branch to move; 'all' for every sport"
    )
    p_assign.add_argument(
        "--with-hits",
        action="store_true",
        help="Also push the planned Hits category, overwriting any set by hand",
    )
    p_assign.add_argument(
        "--apply", action="store_true", help="Push to eBay (default: preview only)"
    )
    p_assign.add_argument("--limit", type=int, help="Cap how many listings are moved")

    # Sync Topps Calendar
    sub.add_parser("sync-topps-calendar")

    # Saved eBay searches -> Slack
    p_watch = sub.add_parser(
        "watch-searches",
        help="Run due saved eBay searches and alert Slack about new listings",
    )
    p_watch.add_argument(
        "--force", action="store_true", help="Ignore intervals; run every enabled search"
    )
    p_watch.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be posted; no Slack, no state writes, no GCS/BigQuery",
    )
    p_watch.add_argument(
        "--only", action="append", metavar="NAME", help="Restrict to this search (repeatable)"
    )
    p_watch.add_argument(
        "--reseed",
        action="append",
        metavar="NAME",
        help="Discard the seen-cache and silently re-seed this search (repeatable)",
    )
    p_watch.add_argument("--config", help="Path to searches.yaml (overrides paths.searches_file)")
    p_watch.add_argument(
        "--no-flush", action="store_true", help="Skip the GCS/BigQuery flush this run"
    )
    p_watch.add_argument(
        "--list", action="store_true", help="Validate the config and list searches; run nothing"
    )

    # Inspect one saved search's results without alerting or writing state
    p_prev = sub.add_parser(
        "preview-search",
        help="Show every listing a saved search returns, and why any were filtered out",
    )
    p_prev.add_argument("name", help="Search name from searches.yaml")
    p_prev.add_argument("--csv", help="Also write the full result set (all columns) to this path")
    p_prev.add_argument(
        "--passed-only", action="store_true", help="Hide listings rejected by post-filters"
    )
    p_prev.add_argument("--max-results", type=int, help="Cap the fetch (default: seed_max_results)")
    p_prev.add_argument("--rows", type=int, default=40, help="Rows to print (default 40; 0 = all)")
    p_prev.add_argument("--config", help="Path to searches.yaml (overrides paths.searches_file)")

    # Which eBay aspects a saved search could filter on
    p_asp = sub.add_parser(
        "search-aspects",
        help="List the eBay aspects available to filter a saved search on, with counts",
    )
    p_asp.add_argument("name", help="Search name from searches.yaml")
    p_asp.add_argument("--top", type=int, default=8, help="Values shown per aspect (default 8)")
    p_asp.add_argument("--csv", help="Write every aspect/value pair to this path")
    p_asp.add_argument("--config", help="Path to searches.yaml (overrides paths.searches_file)")

    # Interactive TCDB advanced search (real browser, manual login once per session)
    p_tcdb = sub.add_parser(
        "tcdb-search",
        help="Search tcdb.com by card number with saved defaults; results open in Chrome",
    )
    p_tcdb.add_argument(
        "card_numbers",
        nargs="*",
        help="Card numbers to search right away; the interactive prompt follows",
    )
    p_tcdb.add_argument("--category", help="Sport/category (default Baseball or config)")
    p_tcdb.add_argument("--year")
    p_tcdb.add_argument("--set-name", dest="set_name")
    p_tcdb.add_argument(
        "--set-type", dest="set_type", help="Code or label, e.g. M or 'Minor League'"
    )
    p_tcdb.add_argument("--name", help="Player name, e.g. Bonds")
    p_tcdb.add_argument("--team")
    p_tcdb.add_argument("--note")
    p_tcdb.add_argument(
        "--no-login", action="store_true", help="Skip the login check (search anonymously)"
    )
    p_tcdb.add_argument(
        "--rows", type=int, default=0, help="Max result rows to print per search (0 = all)"
    )
    p_tcdb.add_argument("--profile-dir", help="Chrome profile dir (overrides tcdb.profile_dir)")

    args = parser.parse_args()

    # Argument parsing (incl. --help) never touches config; do config-dependent
    # setup only once a real command has been selected.
    setup_logging()
    ensure_runtime_dirs()

    if args.cmd == "end-oos-listings":
        from shoebox.pipelines.end_oos_listings import end_oos_listings

        end_oos_listings()
        return

    if args.cmd == "sync-metadata":
        from shoebox.pipelines.sync_metadata import sync_metadata

        sync_metadata()
        return

    if args.cmd == "create-queue-excel":
        from shoebox.pipelines.load_listing_queue_from_excel import (
            create_queue_file,
        )

        create_queue_file()
        return

    if args.cmd == "create-listings":
        from shoebox.pipelines.create_listings import run_listings

        run_listings(
            publish=args.publish,
            dry_run=args.dry_run,
            schedule=args.schedule,
            scrape_prices=args.scrape_prices,
        )
        return

    if args.cmd == "create-variation-listings":
        from shoebox.pipelines.create_variation_listing import run_variation_listing

        run_variation_listing(
            excel_path=Path(args.excel_path),
            sheet_name=args.sheet_name,
            publish=args.publish,
            dry_run=args.dry_run,
            schedule=args.schedule,
            in_stock_only=args.in_stock_only,
            images_dir=Path(args.images_dir) if args.images_dir else None,
            default_image_path=(Path(args.default_image_path) if args.default_image_path else None),
        )
        return

    if args.cmd == "orders-awaiting-shipment":
        from shoebox.services.orders_awaiting_shipment import display_orders

        display_orders(
            pull_order=args.pull_order,
            buyer_order=args.buyer_order,
            display=args.display,
            message=args.message,
        )
        return

    if args.cmd == "ui":
        subprocess.call(["streamlit", "run", "shoebox/ui/app.py"])

    if args.cmd == "sync-active-listings":
        from shoebox.pipelines.sync_active_listings import sync_active_listings

        sync_active_listings()
        return

    if args.cmd == "sync-orders":
        from shoebox.pipelines.sync_orders import sync_orders

        sync_orders()
        return

    if args.cmd == "sync-active-listing-details":
        from shoebox.pipelines.sync_active_listing_details import (
            sync_active_listing_details,
        )

        kwargs = {"max_workers": args.workers} if args.workers else {}
        sync_active_listing_details(**kwargs)
        return

    if args.cmd == "enhance-listing-titles":
        from shoebox.pipelines.enhance_listing_titles import enhance_listing_titles

        enhance_listing_titles(
            input_path=Path(args.input) if args.input else None,
            apply=args.apply,
            limit=args.limit,
        )
        return

    if args.cmd == "plan-store-categories":
        from shoebox.pipelines.plan_store_categories import plan_store_categories

        plan_store_categories(
            input_path=Path(args.input) if args.input else None,
            sport=args.sport,
        )
        return

    if args.cmd == "create-store-categories":
        from shoebox.pipelines.create_store_categories import create_store_categories

        create_store_categories(
            plan_path=Path(args.plan) if args.plan else None,
            apply=args.apply,
        )
        return

    if args.cmd == "assign-store-categories":
        from shoebox.pipelines.assign_store_categories import assign_store_categories

        assign_store_categories(
            plan_path=Path(args.plan) if args.plan else None,
            sport=None if str(args.sport).casefold() == "all" else args.sport,
            with_hits=args.with_hits,
            apply=args.apply,
            limit=args.limit,
        )
        return

    if args.cmd == "slack-bot":
        from shoebox.services.slack_bot_service import start_bot_service

        start_bot_service()
        return

    if args.cmd == "relist-listings":
        from shoebox.pipelines.relist_listings import main as relist_listings

        relist_listings(
            schedule=args.schedule,
            price_scrape=not args.no_scrape,
            dry_run=args.dry_run,
        )
        return

    if args.cmd == "send-offers":
        from shoebox.pipelines.send_offers import main as send_offers

        send_offers(dry_run=args.dry_run, auto=args.auto, timeout_s=args.timeout_s)
        return

    if args.cmd == "watch-searches":
        from shoebox.pipelines.watch_searches import watch_searches

        watch_searches(
            force=args.force,
            dry_run=args.dry_run,
            only=args.only,
            reseed=args.reseed,
            config_path=args.config,
            flush=not args.no_flush,
            list_only=args.list,
        )
        return

    if args.cmd == "preview-search":
        from shoebox.pipelines.preview_search import run_preview

        run_preview(
            args.name,
            config_path=args.config,
            max_results=args.max_results,
            passed_only=args.passed_only,
            csv_path=args.csv,
            limit_rows=args.rows or None,
        )
        return

    if args.cmd == "search-aspects":
        from shoebox.pipelines.preview_search import run_aspects

        run_aspects(args.name, config_path=args.config, top=args.top, csv_path=args.csv)
        return

    if args.cmd == "sync-topps-calendar":
        from shoebox.pipelines.sync_topps_calendar import (
            run as sync_topps_calendar,
        )

        sync_topps_calendar(dry_run=False, headless=False)
        return

    if args.cmd == "tcdb-search":
        from shoebox.pipelines.tcdb_search import run_tcdb_search

        overrides = {
            k: getattr(args, k)
            for k in ("category", "year", "set_name", "set_type", "name", "team", "note")
            if getattr(args, k)
        }
        raise SystemExit(
            run_tcdb_search(
                overrides=overrides,
                card_numbers=args.card_numbers,
                login=not args.no_login,
                max_rows=args.rows,
                profile_dir=args.profile_dir,
            )
        )


if __name__ == "__main__":
    main()
