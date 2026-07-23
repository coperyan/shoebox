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

    sub.add_parser(
        "end-oos-listings", help="Ends active listings with no stock available."
    )

    ## Create listing queue from excel
    sub.add_parser(
        "create-queue-excel",
        help="Converts Inputs.xlsm into JSONL format created by UI..",
    )

    ## UI
    sub.add_parser("ui", help="Opens streamlit UI to create new listings..")

    ## Create Listings
    p_list = sub.add_parser(
        "create-listings", help="Create/update offers and publish listings"
    )
    p_list.add_argument("--dry-run", action="store_true")
    p_list.add_argument("--publish", action="store_true")
    p_list.add_argument("--schedule", action="store_true")
    p_list.add_argument("--scrape-prices", action="store_true")

    ## Orders Awaiting Shipment
    orders = sub.add_parser(
        "orders-awaiting-shipment", help="Display orders awaiting shipment"
    )
    orders.add_argument("--pull-order", action="store_true")
    orders.add_argument("--buyer-order", action="store_true")
    orders.add_argument("--display", action="store_true")
    orders.add_argument("--message", action="store_true")

    ##Active Listings
    sub.add_parser(
        "sync-active-listings", help="Update active listings in GCS/BigQuery.."
    )

    # Active Listing Details
    sub.add_parser(
        "sync-active-listing-details",
        help="Update active listing details in GCS/BigQuery..",
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
    p_var.add_argument(
        "--default-image-path", help="Hero image for the listing (optional)"
    )

    # Relist listings
    p_relist = sub.add_parser(
        "relist-listings", help="Relist aged listings with Slack price confirmation"
    )
    p_relist.add_argument("--schedule", action="store_true")
    p_relist.add_argument("--no-scrape", action="store_true")
    p_relist.add_argument("--dry-run", action="store_true")

    # Send offers to watchers
    p_offers = sub.add_parser(
        "send-offers", help="Send negotiation offers to eligible watchers"
    )
    p_offers.add_argument("--dry-run", action="store_true")
    p_offers.add_argument(
        "--max-price",
        type=float,
        default=19.99,
        help="Only send offers on listings priced at or below this (default 19.99)",
    )

    # Sync Topps Calendar
    sub.add_parser("sync-topps-calendar")

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
            default_image_path=(
                Path(args.default_image_path) if args.default_image_path else None
            ),
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

        sync_active_listing_details()
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

        send_offers(dry_run=args.dry_run, max_price=args.max_price)
        return

    if args.cmd == "sync-topps-calendar":
        from shoebox.pipelines.sync_topps_calendar import (
            run as sync_topps_calendar,
        )

        sync_topps_calendar(dry_run=False, headless=False)
        return


if __name__ == "__main__":
    main()
