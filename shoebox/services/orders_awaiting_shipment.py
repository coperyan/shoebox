import json
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from rich.console import Console

from shoebox.clients.ebay.client import EbayClient
from shoebox.settings import get_settings
from shoebox.utils.render_table import render_table
from shoebox.utils.slack import notify
from shoebox.utils.slack_formatting import table_messages

DISPLAY_COLS = [
    "title",
    "buyer_username",
    "is_variation",
    "variation_group",
    "variation_card_number_derived",
    "variation_card_name_cleaned",
    "quantity",
]

# Variation aspect names treated as the card dimension; every other aspect
# (e.g. "Insert") becomes part of the row's variation_group.
CARD_ASPECT_NAMES = ("Card", "Card #")


def derived_card_number(card: str) -> str:
    if " " in card:
        card = card.split(" ")[0]
    if "-" in card:
        card = card.split("-")[1]
    if "USC" in card:
        card = card.replace("USC", "")
    if "US" in card:
        card = card.replace("US", "")
    return card


def cleanse_card_name(card: str) -> str:
    if " - " in card:
        card = card.replace(f"{card.split(' - ')[0]} ", "")
    if " " in card:
        card = card.replace(f"{card.split(' ')[0]} ", "")
    return card


def derive_variation_group(aspects_json) -> str | None:
    """Join every non-Card variation aspect value (e.g. Insert set) into a
    group label; None when the listing only varies by Card."""
    if pd.isnull(aspects_json) or not aspects_json:
        return None
    try:
        aspects = json.loads(aspects_json)
    except (TypeError, ValueError):
        return None
    others = [str(v) for k, v in aspects.items() if k not in CARD_ASPECT_NAMES and v]
    return " - ".join(others) if others else None


def iter_listing_groups(df_slice: pd.DataFrame):
    """Yield (group_label, rows) per extra-variation group of one listing.

    Listings that only vary by Card yield a single (None, all rows) so the
    existing single-table behavior is unchanged. Rows without a group in a
    listing that has them are collected under "(other)".
    """
    if df_slice["variation_group"].notna().any():
        for grp in sorted(df_slice["variation_group"].dropna().unique()):
            yield grp, df_slice[df_slice["variation_group"] == grp]
        ungrouped = df_slice[df_slice["variation_group"].isnull()]
        if not ungrouped.empty:
            yield "(other)", ungrouped
    else:
        yield None, df_slice


def sort_mixed_vals_df(df: pd.DataFrame, mixed_col: str, initial_cols: list) -> pd.DataFrame:
    df["sort_key"] = pd.to_numeric(df[mixed_col], errors="coerce")
    sort_order = initial_cols + ["sort_key", mixed_col]
    df_sorted = df.sort_values(by=sort_order, key=lambda x: np.where(x.notna(), x, np.inf)).drop(
        columns=["sort_key"]
    )
    return df_sorted


def get_orders() -> pd.DataFrame:
    ebay_api = EbayClient()
    orders = ebay_api.fulfillment.get_orders(
        start_date=(datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"),
        end_date=None,
        status="NOT_STARTED",
    )
    df = pd.json_normalize(np.concatenate([o.flattened_line_items for o in orders]), sep="_")
    return df


def transform_df(df: pd.DataFrame) -> pd.DataFrame:
    df["is_variation"] = df.apply(lambda x: False if pd.isnull(x.variation_card) else True, axis=1)
    df["variation_group"] = df["variation_aspects_json"].apply(derive_variation_group)
    df["variation_card_number_derived"] = df.apply(
        lambda x: derived_card_number(x.variation_card) if x["is_variation"] else None,
        axis=1,
    )
    df["variation_card_name_cleaned"] = df.apply(
        lambda x: cleanse_card_name(x.variation_card) if x["is_variation"] else None,
        axis=1,
    )
    df["card_name_cleaned"] = df.apply(
        lambda x: x["variation_card_name_cleaned"] if x["is_variation"] else x["title"],
        axis=1,
    )
    df = sort_mixed_vals_df(
        df=df,
        mixed_col="variation_card_number_derived",
        initial_cols=["is_variation", "title"],
    )
    return df


def display_df(
    df: pd.DataFrame,
    cols=DISPLAY_COLS,
    pull_order: bool = True,
    buyer_order: bool = True,
):
    console = Console()
    tmp_df = df[cols]

    if pull_order:
        df_slice = tmp_df[tmp_df["is_variation"] == False].drop(  # noqa: E712  (pandas mask)
            columns=[c for c in tmp_df.columns.values if "variation" in c]
        )
        if not df_slice.empty:
            console.clear()
            table = render_table(df_slice, title="Non-Variation Listings")
            console.print(table)
            input("Press Enter to Continue..")

        for var_listing in sorted(
            list(set(tmp_df[tmp_df["is_variation"] == True]["title"].values.tolist()))  # noqa: E712  (pandas mask)
        ):
            listing_slice = tmp_df[tmp_df["title"] == var_listing]
            for group_label, group_rows in iter_listing_groups(listing_slice):
                df_slice = (
                    group_rows.groupby(
                        ["variation_card_number_derived", "variation_card_name_cleaned"],
                        sort=False,
                    )["quantity"]
                    .sum()
                    .reset_index()
                )
                table_title = f"{var_listing} — {group_label}" if group_label else var_listing
                console.clear()
                table = render_table(df_slice, title=table_title)
                console.print(table)
                input("Press Enter to Continue..")

    if buyer_order:
        for buyer_name in sorted(list(set(tmp_df["buyer_username"].values.tolist()))):
            df_slice = tmp_df[tmp_df["buyer_username"] == buyer_name].drop(
                columns=["variation_group"]
            )
            total_qty = df_slice["quantity"].sum()
            df_slice = sort_mixed_vals_df(
                df=df_slice.copy(),
                mixed_col="variation_card_number_derived",
                initial_cols=["is_variation", "title"],
            )
            console.clear()
            table = render_table(df_slice, title=f"{buyer_name} - {total_qty}")
            console.print(table)
            input("Press Enter to Continue..")


def message_df(
    df: pd.DataFrame,
    cols=DISPLAY_COLS,
    pull_order: bool = True,
    buyer_order: bool = True,
):
    tmp_df = df[cols]
    channel = get_settings().slack.notify_channel

    # One parent message in-channel; every section lands in its thread so the
    # channel shows a single line per run.
    n_buyers = tmp_df["buyer_username"].nunique()
    total_qty = int(tmp_df["quantity"].sum())
    parent_ts = notify(
        channel=channel,
        message=f"📦 *Orders Awaiting Shipment* — {n_buyers} buyer(s), {total_qty} item(s)",
    )

    def send_section(msgs: list[str]) -> None:
        for msg in msgs:
            notify(channel=channel, message=msg, thread_ts=parent_ts)

    if pull_order:
        df_slice = tmp_df[tmp_df["is_variation"] == False].drop(  # noqa: E712  (pandas mask)
            columns=[c for c in tmp_df.columns.values if "variation" in c]
        )
        rows = [[r["title"], r["buyer_username"], r["quantity"]] for _, r in df_slice.iterrows()]
        send_section(table_messages("Non-Variation Listings", ["Title", "Buyer", "Qty"], rows))

        for var_listing in sorted(
            list(set(tmp_df[tmp_df["is_variation"] == True]["title"].values.tolist()))  # noqa: E712  (pandas mask)
        ):
            listing_slice = tmp_df[tmp_df["title"] == var_listing]
            for group_label, group_rows in iter_listing_groups(listing_slice):
                df_slice = (
                    group_rows.groupby(
                        ["variation_card_number_derived", "variation_card_name_cleaned"],
                        sort=False,
                    )["quantity"]
                    .sum()
                    .reset_index()
                )
                rows = [
                    [
                        row["variation_card_number_derived"],
                        row["variation_card_name_cleaned"],
                        row["quantity"],
                    ]
                    for _, row in df_slice.iterrows()
                ]
                heading = f"{var_listing} — {group_label}" if group_label else var_listing
                send_section(table_messages(heading, ["Card #", "Name", "Qty"], rows))

    if buyer_order:
        for buyer_name in sorted(list(set(tmp_df["buyer_username"].values.tolist()))):
            df_slice = tmp_df[tmp_df["buyer_username"] == buyer_name]
            total_qty = df_slice["quantity"].sum()
            df_slice = sort_mixed_vals_df(
                df=df_slice.copy(),
                mixed_col="variation_card_number_derived",
                initial_cols=["is_variation", "title"],
            )
            rows = [
                (
                    [r.title, r.quantity]
                    if not r.is_variation
                    else [
                        f"{r['title']} - {r['variation_card_number_derived']} - {r['variation_card_name_cleaned']}",
                        r.quantity,
                    ]
                )
                for _, r in df_slice.iterrows()
            ]
            send_section(table_messages(f"{buyer_name} - {total_qty}", ["Item", "Qty"], rows))


def display_orders(
    pull_order: bool = True,
    buyer_order: bool = True,
    display: bool = True,
    message: bool = False,
):

    df = get_orders()
    df = transform_df(df)
    if message:
        message_df(df=df, pull_order=pull_order, buyer_order=buyer_order)
    if display:
        display_df(df=df, cols=DISPLAY_COLS, pull_order=pull_order, buyer_order=buyer_order)
    else:
        return
