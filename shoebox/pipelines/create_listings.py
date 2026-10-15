from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from shoebox.clients.bigquery import BigQueryClient
from shoebox.clients.ebay.client import EbayClient
from shoebox.clients.gcs import GCSClient, is_gs_uri, read_gs_text
from shoebox.clients.image_log import ImageLogClient
from shoebox.clients.price_scraper import PriceScraper, search_helper
from shoebox.models.ebay_listing import EbayListingResult
from shoebox.models.listing_queue import ListingQueueRow
from shoebox.pipelines.load_listing_queue_from_excel import create_queue_file
from shoebox.services.listings import ListingService
from shoebox.settings import Settings, ensure_runtime_dirs, get_settings
from shoebox.transforms.listing_builder import build_draft
from shoebox.utils.ad_campaign import get_ad_campaign
from shoebox.utils.image_refs import materialize_image
from shoebox.utils.jsonl import append_jsonl, read_jsonl
from shoebox.utils.pricing import parse_price_reply, round_up_to_nine
from shoebox.utils.sku import build_sku_50
from shoebox.utils.slack import notify_and_wait, notify_best_effort

logger = logging.getLogger(__name__)


def _utc_ts() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


QUEUE_FILENAME = "listing_queue_enriched.jsonl"


def default_queue_source(settings: Settings) -> str | Path:
    """Where the queue is read from unless ``--queue`` says otherwise: the
    ``paths.queue_uri`` GCS object when set (the iOS app's API writes it),
    else the local file the Streamlit UI / Excel loader write."""
    if settings.paths.queue_uri:
        return settings.paths.queue_uri
    return Path(settings.paths.exports_dir) / "jsonl" / QUEUE_FILENAME


def read_queue(source: str | Path) -> list[ListingQueueRow]:
    """Load queue rows from a local JSONL path or a ``gs://`` object.

    Rows carrying an ``error`` key (``build_enriched_json``'s "No checklist
    match" records) are rejected up front: as ``ListingQueueRow`` they would
    have no ids or player and only fail deep inside the eBay calls.
    """
    if is_gs_uri(source):
        text = read_gs_text(str(source))
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Listing queue not found: {path}")
        records = read_jsonl(path)

    bad = [i for i, r in enumerate(records) if not isinstance(r, dict) or "error" in r]
    if bad:
        raise ValueError(
            f"{len(bad)} queue row(s) carry an enrichment error (rows {bad[:10]}); "
            "fix the queue before listing"
        )
    return [ListingQueueRow(**r) for r in records]


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


def _upload_combined_image(
    q: ListingQueueRow,
    img_client: ImageLogClient,
    *,
    front: Path | None,
    back: Path | None,
) -> str | None:
    """Upload a front+back composite for a two-image row; return its URL.

    ``front`` / ``back`` are the materialised local files (see
    ``_create_one_listing``), never the raw queue references, which may be
    scan numbers or ``gs://`` photos. Best-effort: the listing goes ahead with
    just the two images if this fails.
    """
    if front is None or back is None:
        return None
    try:
        return img_client.upload_combined_image(
            front=front,
            back=back,
            set_name=q.set_name,
            subset_name=q.subset_name,
            card_number=q.card_number,
            parallel_variety=q.parallel_variety,
        ).public_url
    except Exception:
        logger.warning(
            "Could not build the combined image for %s; listing without it",
            q.card_number,
            exc_info=True,
        )
        return None


def _create_one_listing(
    q: ListingQueueRow,
    *,
    sku: str,
    quick_title: str,
    listings: ListingService,
    img_client: ImageLogClient,
    price_scrape_client: PriceScraper,
    publish: bool,
    dry_run: bool,
    schedule: bool,
    scrape_prices: bool,
) -> EbayListingResult:
    """Build and (optionally) publish a single listing, returning its result.

    Raises on any failure; the caller is responsible for catching and recording
    it so that one bad item does not abort the whole batch.
    """
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

    # Upload images. Each reference (local path, bare scan number, or a gs://
    # photo from the iOS app) is resolved to a local file once, up front. The
    # combined front+back picture is built from those files before the scans
    # upload: uploading a scan deletes the local original.
    front_path = materialize_image(q.image_front) if q.image_front else None
    back_path = materialize_image(q.image_back) if q.image_back else None
    combined_url = _upload_combined_image(q, img_client, front=front_path, back=back_path)
    image_urls: list[str] = []

    if front_path is not None:
        image_urls.append(
            img_client.upload_image(
                file_path=front_path,
                set_name=q.set_name,
                subset_name=q.subset_name,
                card_number=q.card_number,
                parallel_variety=q.parallel_variety,
                side="front",
            ).public_url
        )
    if back_path is not None:
        image_urls.append(
            img_client.upload_image(
                file_path=back_path,
                set_name=q.set_name,
                subset_name=q.subset_name,
                card_number=q.card_number,
                parallel_variety=q.parallel_variety,
                side="back",
            ).public_url
        )
    # Third, after the scans: the front stays picture 1, which is the search
    # thumbnail and the image send-offers shows in Slack.
    if combined_url:
        image_urls.append(combined_url)

    draft = build_draft(row=q, image_urls=image_urls, sku=sku, schedule=schedule)

    if dry_run:
        return EbayListingResult(
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

    logger.debug("Inventory item: %s", draft.inventory_item)
    logger.debug("Offer: %s", draft.offer)
    resp = listings.create_listing(
        sku=sku,
        inventory_item=draft.inventory_item,
        offer=draft.offer,
        publish=publish,
        existing_offer_action="delete",
        promote_listing=False,  ##Fix Later
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

    return EbayListingResult(
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


@dataclass
class RunSummary:
    """What a run did: one entry per queue row, in order."""

    listed: int = 0
    failed: int = 0
    results: list[EbayListingResult] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)


def _quick_title(q: ListingQueueRow) -> str:
    return (
        f"{q.set_name} "
        + f"{q.subset_name} "
        + (f"{q.parallel_variety} " if q.parallel_variety else "")
        + f"{q.player} "
        + f"{q.card_number}"
    )


def run_listings(
    *,
    queue_items: list[ListingQueueRow] | None = None,
    queue: str | Path | None = None,
    publish: bool = True,
    dry_run: bool = False,
    schedule: bool = True,
    scrape_prices: bool = False,
    reporter=None,
) -> RunSummary:
    """List every queue row. ``reporter`` (``services.listing_runs.RunRecordReporter``
    or anything with ``start(total)`` / ``row(**kw)``) is told about progress;
    the image log flush and the results sync run even when the loop is
    interrupted, so a SIGTERM at the job timeout loses nothing already listed."""
    settings = get_settings()

    if queue_items is None:
        source = queue or default_queue_source(settings)
        logger.info("Reading listing queue from %s", source)
        queue_items = read_queue(source)

    img_client = ImageLogClient(settings=settings)
    price_scrape_client = PriceScraper()
    listings = ListingService(EbayClient(settings=settings))

    results_path = Path(settings.paths.exports_dir) / "jsonl" / "ebay_listings.jsonl"

    if scrape_prices:
        price_scrape_client.start()

    summary = RunSummary()
    failures = summary.failures
    if reporter is not None:
        reporter.start(len(queue_items))

    try:
        for q in queue_items:
            quick_title = _quick_title(q)
            logger.info("Starting to create listing for %s", quick_title)
            sku = build_sku_50(q)

            try:
                result = _create_one_listing(
                    q,
                    sku=sku,
                    quick_title=quick_title,
                    listings=listings,
                    img_client=img_client,
                    price_scrape_client=price_scrape_client,
                    publish=publish,
                    dry_run=dry_run,
                    schedule=schedule,
                    scrape_prices=scrape_prices,
                )
            except Exception as e:
                # One bad item must not abort the whole batch — note it and move on.
                # Failures are surfaced in the CLI summary below, not written to BigQuery.
                logger.exception(
                    "Failed to create listing for %s (sku=%s); skipping to next item",
                    quick_title,
                    sku,
                )
                failures.append({"sku": sku, "title": quick_title, "error": str(e)})
                summary.failed += 1
                if reporter is not None:
                    reporter.row(sku=sku, title=quick_title, success=False, error=str(e))
                continue

            append_jsonl(results_path, result.model_dump())
            summary.results.append(result)
            summary.listed += 1
            if reporter is not None:
                reporter.row(
                    sku=sku,
                    title=quick_title,
                    success=True,
                    listing_id=result.listing_id,
                    offer_id=result.offer_id,
                )
    finally:
        _finish_run(settings, img_client=img_client, results_path=results_path)

    if failures:
        logger.warning(
            "%d of %d listings failed and were skipped: %s",
            len(failures),
            len(queue_items),
            ", ".join(f["sku"] for f in failures),
        )
    else:
        logger.info("All %d listings processed with no failures", len(queue_items))
    return summary


def _finish_run(settings, *, img_client: ImageLogClient, results_path: Path) -> None:
    """Persist the image log and ship the results that exist so far."""
    img_client.flush_append_log()

    if not results_path.exists():
        # Every item failed (or nothing was queued): there are no results to
        # sync, and letting sync_results_to_bq raise FileNotFoundError here
        # would bury the failure summary logged above.
        logger.warning("No listing results to sync; skipping GCS/BigQuery.")
        return

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


def run_listing_request(*, store=None, settings=None) -> int:
    """Cloud Run job mode (``create-listings --run-request``): list the queue
    the iOS app asked for.

    Claims the newest ``requested`` run record, reads publish/schedule from it
    (prices are never scraped here), reports progress to it, and posts the
    usual start/finish messages to Slack. Returns the process exit code: 0
    when there was nothing to do or everything ran; a failure re-raises so the
    job-failed alert fires.
    """
    from shoebox.clients.search_state_gcs import _sigterm_as_exit
    from shoebox.services.listing_runs import ListingRunStore, RunRecordReporter

    settings = settings or get_settings()
    in_cloud = bool(os.environ.get("CLOUD_RUN_JOB"))
    if not settings.paths.queue_uri:
        raise RuntimeError("create-listings --run-request needs paths.queue_uri (the queue in GCS)")
    if in_cloud and settings.onedrive.enabled and not settings.onedrive.token_cache_uri:
        raise RuntimeError(
            "create-listings is running in Cloud Run with a local OneDrive token cache; "
            "set onedrive.token_cache_uri (see docs/deployment.md, Listing from the app)"
        )
    if store is None:
        store = ListingRunStore.from_settings(settings, gcs=GCSClient(settings=settings))

    run = store.active()
    if run is None or run.state != "requested":
        logger.info("No requested listing run under %s; nothing to do", store.uri)
        return 0
    found = store.read(run.run_id)
    if found is None:
        return 0
    run, generation = found
    if not store.claim(run, generation):
        return 0

    reporter = RunRecordReporter(store, run)
    channel = settings.slack.notify_channel
    notify_best_effort(
        channel,
        f"Starting create-listings (app run {run.run_id}, {run.rows_total} rows, "
        f"publish={'on' if run.publish else 'off'}, schedule={'on' if run.schedule else 'off'})..",
    )
    try:
        with _sigterm_as_exit():
            summary = run_listings(
                queue=run.queue_uri,
                publish=run.publish,
                schedule=run.schedule,
                scrape_prices=False,
                dry_run=False,
                reporter=reporter,
            )
    except BaseException as exc:
        reporter.fail(f"{type(exc).__name__}: {exc}")
        notify_best_effort(channel, f"create-listings (app run {run.run_id}) failed: {exc}")
        raise
    reporter.finish()
    notify_best_effort(
        channel,
        f"Completed create-listings (app run {run.run_id}): "
        f"{summary.listed} listed, {summary.failed} failed..",
    )
    return 0


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
        main()
    except Exception:
        logger.exception("Failed to upload listings")
        raise
