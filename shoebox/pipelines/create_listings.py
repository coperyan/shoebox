from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
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
from shoebox.settings import (
    Settings,
    ensure_runtime_dirs,
    get_settings,
)
from shoebox.transforms.listing_builder import build_draft
from shoebox.utils.ad_campaign import get_ad_campaign
from shoebox.utils.image_refs import MaterializedImage, archive_scan, materialize_scan, scan_path
from shoebox.utils.jsonl import append_jsonl, read_jsonl
from shoebox.utils.pricing import parse_price_reply, round_up_to_nine
from shoebox.utils.sku import build_sku_50
from shoebox.utils.slack import notify_and_wait, notify_best_effort

logger = logging.getLogger(__name__)


def _utc_ts() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


QUEUE_FILENAME = "listing_queue_enriched.jsonl"

# Scans are fetched this many at a time (OneDrive / GCS downloads), for the
# rows up to PREFETCH_AHEAD past the one being listed: far enough ahead that
# the loop never waits on a download, close enough that a long queue does not
# hold every scan on Cloud Run's in-memory disk at once.
PREFETCH_WORKERS = 8
PREFETCH_AHEAD = 12
# OneDrive "move to Listed" requests run in the background while later rows list.
ARCHIVE_WORKERS = 4

RowImages = tuple[MaterializedImage | None, MaterializedImage | None]


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


def _resolve_side(
    q: ListingQueueRow,
    side: str,
    ref: str,
    fetch: Callable[[], MaterializedImage],
    *,
    img_client: ImageLogClient | None,
    settings: Settings | None,
) -> MaterializedImage:
    """``fetch()`` a row's scan. A scan that is gone but whose card, side and
    file name are already in the image log (a retry of a row that uploaded its
    pictures and then failed on eBay) comes back as ``cached``: the earlier
    upload is reused and no file is needed."""
    try:
        return fetch()
    except FileNotFoundError:
        path = scan_path(ref, settings=settings)
        if img_client is None or not img_client.is_cached(
            file_path=path,
            set_name=q.set_name,
            subset_name=q.subset_name,
            card_number=q.card_number,
            parallel_variety=q.parallel_variety,
            side=side,
        ):
            raise
        logger.info(
            "%s scan %s is gone; reusing its earlier upload for %s %s",
            side,
            path.name,
            q.set_name,
            q.card_number,
        )
        return MaterializedImage(path, "cached")


def _materialize_row(
    q: ListingQueueRow,
    settings: Settings | None = None,
    img_client: ImageLogClient | None = None,
) -> RowImages:
    """Resolve a row's front / back references to local files (see
    ``utils.image_refs`` and :func:`_resolve_side`); a blank side is None."""
    return tuple(  # type: ignore[return-value]
        _resolve_side(
            q,
            side,
            ref,
            lambda ref=ref: materialize_scan(ref, settings=settings),
            img_client=img_client,
            settings=settings,
        )
        if ref
        else None
        for side, ref in (("front", q.image_front), ("back", q.image_back))
    )


class _ImagePrefetch:
    """Materialise the queue's scans on a thread pool ahead of the listing loop.

    One download per distinct reference, so two rows naming the same scan never
    write the same file at once. A failed download surfaces from :meth:`get`
    for its row, which the loop records as that row's failure.
    """

    def __init__(
        self,
        rows: list[ListingQueueRow],
        *,
        settings: Settings,
        pool: ThreadPoolExecutor,
        img_client: ImageLogClient | None = None,
    ):
        self._rows = rows
        self._settings = settings
        self._img_client = img_client
        self._pool = pool
        self._futures: dict[str, Future[MaterializedImage]] = {}
        self._queued = 0

    def ensure(self, upto: int) -> None:
        """Start the downloads for every row before index ``upto``."""
        upto = min(upto, len(self._rows))
        while self._queued < upto:
            q = self._rows[self._queued]
            self._queued += 1
            for ref in (q.image_front, q.image_back):
                if ref and ref.strip() not in self._futures:
                    self._futures[ref.strip()] = self._pool.submit(
                        materialize_scan, ref, settings=self._settings
                    )

    def get(self, index: int) -> RowImages:
        self.ensure(index + 1)
        q = self._rows[index]
        return tuple(  # type: ignore[return-value]
            _resolve_side(
                q,
                side,
                ref,
                self._futures[ref.strip()].result,
                img_client=self._img_client,
                settings=self._settings,
            )
            if ref
            else None
            for side, ref in (("front", q.image_front), ("back", q.image_back))
        )


def _discard_local(path: Path) -> None:
    """Delete an uploaded scan, as ``upload_image(delete_original=True)`` would."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to delete original file: %s", path)


def _upload_row_images(
    q: ListingQueueRow,
    img_client: ImageLogClient,
    *,
    front: MaterializedImage | None,
    back: MaterializedImage | None,
) -> tuple[list[str], list[MaterializedImage]]:
    """Upload the front, the back and their composite at the same time; return
    the picture URLs front, back, combined, and the scans that were actually
    sent (the ones the caller may archive).

    The combined picture goes third: the front stays picture 1, which is the
    search thumbnail and the image send-offers shows in Slack. The composite
    is built from the scans, so they are deleted only once all three uploads
    are done. A failed front or back upload fails the row; a failed composite
    does not (see ``_upload_combined_image``).

    A scan whose card, side and file name are already in the image log is not
    uploaded (the earlier picture is reused), so it is kept on disk: scan
    numbers restart, and a stale queue row must not delete a new batch's file.
    """
    sides = [(side, image) for side, image in (("front", front), ("back", back)) if image]
    cached = {
        side: img_client.is_cached(
            file_path=image.path,
            set_name=q.set_name,
            subset_name=q.subset_name,
            card_number=q.card_number,
            parallel_variety=q.parallel_variety,
            side=side,
        )
        for side, image in sides
    }
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="img-upload") as pool:
        combined = pool.submit(
            _upload_combined_image,
            q,
            img_client,
            front=front.path if front else None,
            back=back.path if back else None,
        )
        uploads = [
            pool.submit(
                img_client.upload_image,
                file_path=image.path,
                set_name=q.set_name,
                subset_name=q.subset_name,
                card_number=q.card_number,
                parallel_variety=q.parallel_variety,
                side=side,
                delete_original=False,
            )
            for side, image in sides
        ]
        image_urls = [upload.result().public_url for upload in uploads]
        combined_url = combined.result()
    uploaded = []
    for side, image in sides:
        if image.source == "cached":
            continue  # already gone; _resolve_side logged it
        if cached[side]:
            logger.warning(
                "%s for %s %s was uploaded before; reusing that picture and keeping %s",
                side,
                q.set_name,
                q.card_number,
                image.path,
            )
            continue
        _discard_local(image.path)
        uploaded.append(image)
    if combined_url:
        image_urls.append(combined_url)
    return image_urls, uploaded


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
    images: RowImages | None = None,
    archive: Callable[[MaterializedImage], object] | None = None,
    settings: Settings | None = None,
) -> EbayListingResult:
    """Build and (optionally) publish a single listing, returning its result.

    Raises on any failure; the caller is responsible for catching and recording
    it so that one bad item does not abort the whole batch. ``images`` are the
    row's scans already materialised (``run_listings`` prefetches them);
    ``archive`` is handed each uploaded scan in place of an inline
    :func:`archive_scan` (``run_listings`` moves them in the background).
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
    # photo from the iOS app) is resolved to a local file once, up front. Once
    # uploaded, the local scans are deleted and a scan that was fetched from
    # OneDrive is moved into its Listed subfolder.
    front, back = images if images is not None else _materialize_row(q, settings, img_client)
    image_urls, uploaded = _upload_row_images(q, img_client, front=front, back=back)
    for image in uploaded:
        if archive is None:
            archive_scan(image, settings=settings)
        else:
            archive(image)

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
    on_complete: Callable[[RunSummary], None] | None = None,
    settings: Settings | None = None,
) -> RunSummary:
    """List every queue row. ``reporter`` (``services.listing_runs.RunRecordReporter``
    or anything with ``start(total)`` / ``row(**kw)``) is told about progress;
    the image log flush and the results sync run even when the loop is
    interrupted, so a SIGTERM at the job timeout loses nothing already listed.

    Rows list one at a time, but their scans are downloaded ahead of the loop
    and the OneDrive moves run behind it. ``on_complete`` is called once every
    row is done and every move has finished, before the GCS / BigQuery sync,
    so the app can show the run as done without waiting for it."""
    settings = settings or get_settings()

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

    prefetch_pool = ThreadPoolExecutor(PREFETCH_WORKERS, thread_name_prefix="scan-fetch")
    archive_pool = ThreadPoolExecutor(ARCHIVE_WORKERS, thread_name_prefix="scan-archive")
    prefetch = _ImagePrefetch(
        queue_items, settings=settings, pool=prefetch_pool, img_client=img_client
    )

    def archive(image: MaterializedImage) -> None:
        archive_pool.submit(archive_scan, image, settings=settings)

    try:
        for index, q in enumerate(queue_items):
            prefetch.ensure(index + 1 + PREFETCH_AHEAD)
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
                    images=prefetch.get(index),
                    archive=archive,
                    settings=settings,
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
        archive_pool.shutdown(wait=True)
        if on_complete is not None:
            on_complete(summary)
    finally:
        prefetch_pool.shutdown(wait=True, cancel_futures=True)
        archive_pool.shutdown(wait=True)
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

    Claims the newest ``requested`` run record, reads publish/schedule (and
    the OneDrive scans folder, when the app chose one) from it -- prices are
    never scraped here --, reports progress to it, and posts the usual
    start/finish messages to Slack. The record is marked done as soon as the
    last row is listed; the GCS / BigQuery results sync runs after that. Returns the process exit code: 0
    when there was nothing to do or everything ran; a failure re-raises so the
    job-failed alert fires.
    """
    from shoebox.clients.search_state_gcs import _sigterm_as_exit
    from shoebox.services.listing_runs import ListingRunStore, RunRecordReporter
    from shoebox.settings import with_scans_folder

    settings = settings or get_settings()
    in_cloud = bool(os.environ.get("CLOUD_RUN_JOB"))
    if not settings.paths.queue_uri:
        raise RuntimeError("create-listings --run-request needs paths.queue_uri (the queue in GCS)")
    if in_cloud and settings.onedrive.enabled and not settings.onedrive.token_cache_uri:
        raise RuntimeError(
            "create-listings is running in Cloud Run with a local OneDrive token cache; "
            "set onedrive.token_cache_uri (see docs/content/guides/deployment.mdx, Listing from the app)"
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
    folder_note = ""
    if run.scans_folder:
        try:
            settings = with_scans_folder(settings, run.scans_folder)
        except ValueError as exc:
            reporter.fail(f"bad scans folder {run.scans_folder!r}: {exc}")
            raise
        folder_note = f", scans from {settings.onedrive.scans_folder}"
    notify_best_effort(
        channel,
        f"Starting create-listings (app run {run.run_id}, {run.rows_total} rows, "
        f"publish={'on' if run.publish else 'off'}, schedule={'on' if run.schedule else 'off'}"
        f"{folder_note})..",
    )

    def completed(summary: RunSummary) -> None:
        reporter.finish()
        notify_best_effort(
            channel,
            f"Completed create-listings (app run {run.run_id}): "
            f"{summary.listed} listed, {summary.failed} failed..",
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
                on_complete=completed,
                settings=settings,
            )
    except BaseException as exc:
        if run.state == "done":
            # Every row was listed and reported; only the results sync failed.
            notify_best_effort(
                channel,
                f"create-listings (app run {run.run_id}) listed everything, "
                f"but the results sync failed: {exc}",
            )
        else:
            reporter.fail(f"{type(exc).__name__}: {exc}")
            notify_best_effort(channel, f"create-listings (app run {run.run_id}) failed: {exc}")
        raise
    if run.state != "done":
        completed(summary)
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
