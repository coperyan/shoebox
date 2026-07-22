from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from shoebox.clients.bigquery import BigQueryClient
from shoebox.clients.ebay_rest.client import EbayClient
from shoebox.clients.gcs import GCSClient
from shoebox.clients.image_log import ImageLogClient
from shoebox.clients.price_scraper import PriceScraper, search_helper
from shoebox.models.ebay_listing import EbayListingResult
from shoebox.models.listing_queue import ListingQueueRow
from shoebox.pipelines.load_listing_queue_from_excel import create_queue_file
from shoebox.settings import ensure_runtime_dirs, get_settings
from shoebox.transforms.listing_builder import build_draft
from shoebox.utils.ad_campaign import get_ad_campaign
from shoebox.utils.jsonl import append_jsonl, read_jsonl
from shoebox.utils.pricing import parse_price_reply, round_up_to_nine
from shoebox.utils.sku import build_sku_50
from shoebox.utils.slack import notify_and_wait

logger = logging.getLogger(__name__)


def _utc_ts() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def read_queue(path: Path) -> list[ListingQueueRow]:
    if not path.exists():
        raise FileNotFoundError(f"Listing queue not found: {path}")
    return [ListingQueueRow(**r) for r in read_jsonl(path)]


def sync_results_to_bq(
    *,
    delete_local: bool,
    local_jsonl: Path,
    gcs_bucket: str,
    bq_dataset: str,
    bq_table: str,
    schema_path: Path,
    write_disposition: str = "WRITE_APPEND",
    gcs_prefix: str = "logs/ebay_listings",
    gcs: GCSClient | None = None,
    bq: BigQueryClient | None = None,
) -> str:
    gcs = gcs or GCSClient()
    bq = bq or BigQueryClient()

    if not local_jsonl.exists():
        raise FileNotFoundError(f"No results JSONL found: {local_jsonl}")

    object_name = f"{gcs_prefix.rstrip('/')}/ebay_listings_{_utc_ts()}.jsonl"
    gcs.upload_text(
        bucket=gcs_bucket,
        object_name=object_name,
        text=local_jsonl.read_text(encoding="utf-8"),
        content_type="application/json",
    )
    bq.load_jsonl_from_gcs(
        bucket=gcs_bucket,
        object_name=object_name,
        dataset=bq_dataset,
        table=bq_table,
        schema_path=schema_path,
        write_disposition=write_disposition,
    )
    if delete_local:
        local_jsonl.unlink()
    return object_name


def run_listings(
    *,
    queue_items: list[ListingQueueRow] | None = None,
    publish: bool = False,
    dry_run: bool = False,
    schedule: bool = False,
    scrape_prices: bool = False,
) -> None:
    settings = get_settings()

    if queue_items is None:
        queue_items = read_queue(
            Path(settings.paths.exports_dir) / "jsonl" / "listing_queue_enriched.jsonl"
        )

    img_client = ImageLogClient(settings=settings)
    price_scrape_client = PriceScraper()
    ebay = EbayClient(settings=settings)

    results_path = Path(settings.paths.exports_dir) / "jsonl" / "ebay_listings.jsonl"

    if scrape_prices:
        price_scrape_client.start()

    for q in queue_items:
        quick_title = (
            f"{q.set_name} "
            + f"{q.subset_name} "
            + (f"{q.parallel_variety} " if q.parallel_variety else "")
            + f"{q.player} "
            + f"{q.card_number}"
        )
        logger.info("Starting to create listing for %s", quick_title)
        sku = build_sku_50(q)

        # Get pricing
        if scrape_prices:
            query, excluded_terms = search_helper(
                set_name=q.set_name,
                subset_type=q.subset_type,
                subset_name=q.subset_name,
                player=q.player,
                parallel_variety=q.parallel_variety,
                print_run=q.print_run,
            )
            excluded_terms.append("PSA")
            new_price = q.price  # fallback when scraping fails
            try:
                ##Scrape and get pricing info
                price_info = price_scrape_client.search_with_averages(
                    query=query, excluded_terms=excluded_terms
                )
                trimmed_mean = price_info.get("averages_filtered").get("trimmed_mean")
                new_price = round_up_to_nine(trimmed_mean)
                logger.info("Found calculated price from scraper: %s", new_price)
            except Exception:
                logger.warning("Error scraping price; falling back to queue price", exc_info=True)
                price_info = "Override"

            ##Prompt confirmation or override from Slack
            try:
                reply_str = notify_and_wait(
                    channel=get_settings().slack.pricing_channel,
                    message=f"Please confirm price: {new_price} - {quick_title}",
                )
            except TimeoutError:
                logger.warning(
                    "Price confirmation timed out; proceeding with %s for %s",
                    new_price,
                    quick_title,
                )
                reply_str = "Y"
            if str(reply_str).strip().upper() not in ("Y", "YES"):
                override = parse_price_reply(str(reply_str))
                if override is not None:
                    logger.info("Overriding %s with %s", new_price, override)
                    new_price = override
                else:
                    logger.warning("Unrecognized price reply %r; keeping %s", reply_str, new_price)

            old_price = q.price
            q.price = new_price
            logger.info(price_info)
            logger.info("Editing price from %s to %s..", old_price, new_price)
            time.sleep(1)

        # Upload images
        image_urls: list[str] = []

        if q.image_front:
            image_urls.append(
                img_client.upload_image(
                    file_path=Path(q.image_front),
                    set_name=q.set_name,
                    subset_name=q.subset_name,
                    card_number=q.card_number,
                    parallel_variety=q.parallel_variety,
                    side="front",
                ).public_url
            )
        if q.image_back:
            image_urls.append(
                img_client.upload_image(
                    file_path=Path(q.image_back),
                    set_name=q.set_name,
                    subset_name=q.subset_name,
                    card_number=q.card_number,
                    parallel_variety=q.parallel_variety,
                    side="back",
                ).public_url
            )

        draft = build_draft(row=q, image_urls=image_urls, sku=sku, schedule=schedule)

        if dry_run:
            result = EbayListingResult(
                card_id=q.card_id,
                sku=sku,
                success=True,
                request={
                    "data": {
                        "inventory_item": draft.inventory_item,
                        "offer": draft.offer,
                    }
                },
                response={"data": {"dry_run": True}},
            )
        else:
            logger.debug("Inventory item: %s", draft.inventory_item)
            logger.debug("Offer: %s", draft.offer)
            resp = ebay.create_listing_from_inventory_flow(
                sku=sku,
                inventory_item=draft.inventory_item,
                offer=draft.offer,
                publish=publish,
                existing_offer_action="delete",
                promote_listing=True,
                campaign_id=get_ad_campaign(
                    title=draft.inventory_item["product"]["title"],
                    set_name=draft.inventory_item["product"]["aspects"]["Set"][0],
                    sport=draft.inventory_item["product"]["aspects"]["Sport"][0],
                ),
            )

            offer_id = None
            if isinstance(resp.get("offer"), dict):
                offer_id = resp["offer"].get("offerId") or resp["offer"].get("offer_id")
            listing_id = None
            if isinstance(resp.get("publish"), dict):
                listing_id = resp["publish"].get("listingId") or resp["publish"].get("listing_id")

            result = EbayListingResult(
                card_id=q.card_id,
                sku=sku,
                offer_id=offer_id,
                listing_id=listing_id,
                success=True,
                request={
                    "data": {
                        "inventory_item": draft.inventory_item,
                        "offer": draft.offer,
                    }
                },
                response={"data": resp},
            )

        append_jsonl(results_path, result.model_dump())

    # Persist image log
    img_client.flush_append_log()

    object_name = sync_results_to_bq(
        delete_local=True,
        local_jsonl=results_path,
        gcs_bucket=settings.gcs.ebay_bucket,
        bq_dataset=settings.bigquery.ebay_dataset,
        bq_table="ebay_listings",
        schema_path=Path("configs/bigquery/schemas/ebay_listings.json"),
    )

    logger.info(
        "Synced results to gs://%s/%s -> %s.%s",
        settings.gcs.ebay_bucket,
        object_name,
        settings.bigquery.ebay_dataset,
        "ebay_listings",
    )


def main(generate_from_excel: bool = False, **listing_kwargs) -> None:
    """Direct-run entrypoint; prefer `shoebox create-listings` for flag handling."""
    ensure_runtime_dirs()
    if generate_from_excel:
        create_queue_file()
    logger.info("Listing kwargs: %s", listing_kwargs)
    run_listings(**listing_kwargs)


if __name__ == "__main__":
    from shoebox.utils.logging_setup import setup_logging

    setup_logging()
    try:
        main(dry_run=True)
    except Exception:
        logger.exception("Failed to upload listings")
        raise
