import logging
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import requests

from shoebox.clients.ebay_rest.client import EbayClient
from shoebox.clients.gcs import GCSClient
from shoebox.clients.price_scraper import PriceScraper, search_helper
from shoebox.settings import get_settings
from shoebox.transforms.listing_builder import rebuild_inventory_item_body, rebuild_offer_body
from shoebox.utils.pricing import calculate_new_price, parse_price_reply, round_up_to_nine
from shoebox.utils.slack import notify_and_wait_approval

logger = logging.getLogger(__name__)

# Markdown ladder for cheap listings with zero views: (max current price, new price).
# Prices above the ladder go through scrape + Slack approval instead.
_ZERO_VIEW_PRICE_LADDER: list[tuple[float, float]] = [
    (1.29, 0.99),
    (1.49, 1.29),
    (1.69, 1.49),
    (1.89, 1.69),
    (1.99, 1.79),
]

# Promoted-listings ad rate (percent) applied when refreshing a promoted listing.
_RELIST_PROMOTE_RATE = 7


def get_active_listings(ebay_api: EbayClient) -> pd.DataFrame:
    active_listings = ebay_api.legacy_api.get_active_listings()

    ## Drop variation & non-SKU listings
    active_listings = [
        x
        for x in active_listings
        if all([x["sku"] is not None, "Complete Your Set" not in x["title"]])
    ]
    listing_df = pd.json_normalize(active_listings)
    listing_df["start_time"] = pd.to_datetime(listing_df["start_time"])
    listing_df["listing_age"] = listing_df.apply(
        lambda x: (datetime.now(UTC) - x["start_time"]).days,
        axis=1,
    )

    return listing_df


def get_traffic(listing_ids: list[str], ebay_api: EbayClient) -> pd.DataFrame:
    ## Get traffic report
    max_date = datetime.now().strftime("%Y%m%d")
    min_date = (datetime.now() - timedelta(days=89)).strftime("%Y%m%d")

    traffic_report = ebay_api.analytics.get_traffic_report(
        date_from=min_date,
        date_to=max_date,
        listing_ids=listing_ids,
    )
    traffic_df = pd.json_normalize(traffic_report)
    return traffic_df


def get_listing_df(ebay_api: EbayClient) -> pd.DataFrame:
    listing_df = get_active_listings(ebay_api)
    try:
        traffic_df = get_traffic(listing_df["item_id"].values.tolist(), ebay_api)
        listing_df = listing_df.merge(
            traffic_df, how="left", left_on="item_id", right_on="LISTING_ID"
        )
    except Exception as e:
        if "Too Many Requests" in str(e):
            logger.warning("Reached traffic-report request limit; proceeding without traffic data")
            listing_df[["LISTING_IMPRESSION_TOTAL", "LISTING_VIEWS_TOTAL"]] = 0

    listing_df["price"] = listing_df["price"].astype(float)
    return listing_df


def ebay_image_to_gcs(
    picture_urls: list[str], sku: str, gcs_client: GCSClient, settings
) -> list[str]:
    picture_urls = [p.replace("$_1", "$_57") for p in picture_urls]
    bucket = gcs_client.client.bucket(settings.gcs.image_bucket)
    gcs_urls = []
    for url in picture_urls:
        side = "front" if url == picture_urls[0] else "back"
        resp = requests.get(url)
        tmp_img = resp.content
        blob_path = f"other/{sku}_{side}.png"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(tmp_img, content_type="image/png")
        gcs_urls.append(blob.public_url)
    return gcs_urls


def get_campaign_ads(ebay_api: EbayClient) -> pd.DataFrame:
    campaigns = ebay_api.api.sell_marketing_get_campaigns()
    campaigns = [x.get("record") for x in campaigns if "record" in x]

    results = []
    for campaign in campaigns:
        if campaign.get("campaign_status") == "RUNNING":
            ads = ebay_api.api.sell_marketing_get_ads(campaign_id=campaign.get("campaign_id"))
            iter_results = [x.get("record") for x in ads if "record" in x]
            results.extend(
                [
                    {
                        **{
                            "campaign_id": campaign.get("campaign_id"),
                            "campaign_name": campaign.get("campaign_name"),
                            "campaign_status": campaign.get("campaign_status"),
                        },
                        **x,
                    }
                    for x in iter_results
                ]
            )
    return pd.json_normalize(results)


def add_campaign_info(df: pd.DataFrame, ebay_api: EbayClient) -> pd.DataFrame:
    campaign_ad_df = get_campaign_ads(ebay_api)
    campaign_ad_df = campaign_ad_df[["campaign_id", "ad_id", "listing_id"]]
    df = pd.merge(df, campaign_ad_df, how="left", left_on="item_id", right_on="listing_id")
    return df


def add_schedule_dates(df: pd.DataFrame, min_days: int = 1, max_days: int = 20) -> pd.DataFrame:
    df_len = len(df)
    start_datetime = datetime.now() + timedelta(days=min_days)
    end_datetime = datetime.now() + timedelta(days=max_days)

    df["schedule_datetime"] = pd.to_datetime(
        np.linspace(start_datetime.timestamp(), end_datetime.timestamp(), df_len),
        unit="s",
    )
    df["schedule_datetime"] = df["schedule_datetime"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return df


def get_scrape_query_info(iter_specifics: dict):
    query, excluded_terms = search_helper(
        set_name=iter_specifics.get("Set")[0],
        subset_type=("Insert" if iter_specifics.get("Insert Set") else "Base"),
        subset_name=(
            iter_specifics.get("Insert Set")[0] if iter_specifics.get("Insert Set") else "Base"
        ),
        player=(
            " ".join(iter_specifics.get("Player/Athlete"))
            if iter_specifics.get("Player/Athlete")
            else None
        ),
        parallel_variety=(
            iter_specifics.get("Parallel/Variety")[0]
            if iter_specifics.get("Parallel/Variety")
            and iter_specifics.get("Parallel/Variety")[0] not in ["Base", "[Base]"]
            else None
        ),
        print_run=(iter_specifics.get("Print Run")[0] if iter_specifics.get("Print Run") else None),
    )
    return query, excluded_terms


def relist_listing(
    row: pd.Series,
    ebay_api: EbayClient,
    gcs_client: GCSClient,
    settings,
    scrape_client: PriceScraper | None = None,
    scrape_prices: bool = False,
    dry_run: bool = False,
):
    logger.info("Starting %s", row["title"])

    sku = row["sku"]
    details = ebay_api.legacy_api.get_item_details(item_id=row["item_id"])
    specifics = details.get("item_specifics")
    schedule_datetime = row.get("schedule_datetime")

    if not pd.isnull(row["ad_id"]):
        ad_id = row["ad_id"]
        campaign_id = row["campaign_id"]
    else:
        ad_id = None
        campaign_id = None

    new_price = row["price"]
    if row["price"] > 1.99:
        if scrape_prices and scrape_client:
            query, excluded_terms = get_scrape_query_info(specifics)
            scrape_info = scrape_client.search_with_averages(
                query=query, excluded_terms=excluded_terms
            )
            result = scrape_info.get("averages_filtered_terms").get("trimmed_mean")
            if result:
                logger.info("Scrape result: $%s", result)
                new_price = round_up_to_nine(result)
            else:
                logger.info("No scrape results found..")

        if dry_run:
            logger.info(
                "[dry_run] Would ask Slack to approve $%s -> $%s for %s",
                row["price"],
                new_price,
                row["title"],
            )
        else:
            try:
                resp = notify_and_wait_approval(
                    settings.slack.pricing_channel,
                    message=(
                        f"*Price approval:* {row['title']}\n"
                        f"Old: ${row['price']} → New: ${new_price}"
                    ),
                    approve_label=f"Approve ${new_price}",
                )
            except TimeoutError:
                logger.warning("Price approval timed out; skipping %s..", row["title"])
                return
            if resp.approved:
                new_price = float(new_price)
                logger.info("Pricing approved via Slack..")
            else:
                override = parse_price_reply(resp.reply_text)
                if override is None:
                    logger.warning(
                        "Unrecognized price reply %r; skipping %s..",
                        resp.reply_text,
                        row["title"],
                    )
                    return
                new_price = override
                logger.info("Pricing updated to $%s..", new_price)

    elif row["LISTING_VIEWS_TOTAL"] == 0:
        for max_price, ladder_price in _ZERO_VIEW_PRICE_LADDER:
            if row["price"] <= max_price:
                new_price = ladder_price
                break

    else:
        new_price = calculate_new_price(row["price"])

    new_price = round_up_to_nine(new_price)
    logger.info("Changed price from $%s to $%s", row["price"], new_price)

    if dry_run:
        logger.info("[dry_run] Would relist %s (sku=%s) at $%s", row["title"], sku, new_price)
        return

    image_urls = ebay_image_to_gcs(details["picture_urls"], sku, gcs_client, settings)

    inventory_item = ebay_api.api.sell_inventory_get_inventory_item(sku=sku)
    inventory_item_body = rebuild_inventory_item_body(inventory_item, image_urls)

    offers = ebay_api.api.sell_inventory_get_offers(sku=sku)
    offer = [x["record"] for x in offers if "record" in x][0]
    offer_body = rebuild_offer_body(offer, new_price, schedule_datetime)

    ebay_api.refresh_listing_flow(
        sku=sku,
        inventory_item_body=inventory_item_body,
        offer_body=offer_body,
        existing_offer_id=offer["offer_id"],
        existing_ad_id=ad_id,
        campaign_id=campaign_id,
        promote_listing=bool(campaign_id),
        promote_rate=_RELIST_PROMOTE_RATE,
    )


def main(
    schedule: bool = True,
    min_age: int = 90,
    min_price: float = 2,
    price_scrape: bool = True,
    min_impressions: int = 100,
    dry_run: bool = False,
):
    ebay_api = EbayClient()
    gcs_client = GCSClient()
    settings = get_settings()

    scrape_client = None
    if price_scrape:
        scrape_client = PriceScraper()
        scrape_client.start()

    df = get_listing_df(ebay_api)
    df = add_campaign_info(df, ebay_api)
    df = df[
        (df["listing_age"] >= min_age)
        & (df["price"] > min_price)
        & (df["LISTING_IMPRESSION_TOTAL"] >= min_impressions)
        & (df["watchers"].isnull())
    ].reset_index(drop=True)
    if schedule:
        df = add_schedule_dates(df)

    logger.info("Found %d listings to process fitting criteria..", len(df))

    df = df.sort_values(
        by=["price", "LISTING_IMPRESSION_TOTAL", "LISTING_VIEWS_TOTAL"],
        ascending=[False, False, False],
    ).reset_index(drop=True)

    failures = []
    for index, row in df.iterrows():
        try:
            logger.info("Starting item %d of %d..", index + 1, len(df))
            relist_listing(
                row,
                ebay_api,
                gcs_client,
                settings,
                scrape_client=scrape_client,
                scrape_prices=price_scrape,
                dry_run=dry_run,
            )
            logger.info("Completed item %d of %d..", index + 1, len(df))
        except Exception as e:
            failures.append({"data": row, "exception": str(e)})
            logger.warning("Skipped: %s, error: %s", row["title"], e)
            continue

    if failures:
        logger.warning("%d listing(s) failed to relist", len(failures))
