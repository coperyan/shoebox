from __future__ import annotations

import pandas as pd

from shoebox.clients.bigquery import BigQueryClient
from shoebox.models.listing_queue import ListingQueueRow


def _clean_str_series(s: pd.Series) -> pd.Series:
    return s.astype(str).str.replace("﻿", "", regex=False).str.strip()


def _standardize_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.replace("﻿", "").strip() for c in df.columns]
    for c in df.columns:
        if df[c].dtype == "object":
            df[c] = _clean_str_series(df[c])
    return df


def _none_if_blank(s: str | None) -> str | None:
    if s is None:
        return None
    t = str(s).strip()
    return t if t else None


def normalize_nulls(d: dict) -> dict:
    """Convert empty strings and common null-like string tokens to None."""
    null_tokens = {"", "none", "null", "nan", "<na>", "na", "n/a"}
    out: dict = {}
    for k, v in d.items():
        if v is None:
            out[k] = None
        elif isinstance(v, float) and pd.isna(v):
            out[k] = None
        elif isinstance(v, str) and v.strip().lower() in null_tokens:
            out[k] = None
        else:
            out[k] = v
    return out


def load_checklist_and_parallels() -> tuple[pd.DataFrame, pd.DataFrame]:
    bq = BigQueryClient()

    cdf = bq.run_query(sql="checklist_queue.sql", return_df=True)
    pdf = bq.run_query(sql="parallels_queue.sql", return_df=True)

    cdf = _standardize_df(cdf)
    pdf = _standardize_df(pdf)

    for col in cdf.columns:
        if cdf[col].dtype == "object":
            cdf[col] = cdf[col].replace({"": pd.NA})
    for col in pdf.columns:
        if pdf[col].dtype == "object":
            pdf[col] = pdf[col].replace({"": pd.NA})

    return cdf, pdf


def build_enriched_json(
    row: ListingQueueRow,
    cdf: pd.DataFrame,
    pdf: pd.DataFrame,
) -> dict | None:
    set_name = _none_if_blank(row.set_name)
    subset_name = _none_if_blank(row.subset_name)
    card_number = _none_if_blank(row.card_number)
    parallel_variety = _none_if_blank(row.parallel_variety)

    if not (set_name and subset_name and card_number):
        return None

    cg = cdf[
        (cdf["set_name"] == set_name)
        & (cdf["subset_name"] == subset_name)
        & (cdf["card_number"] == card_number)
    ]
    if cg.empty:
        return normalize_nulls(
            {
                "error": "No checklist match",
                "set_name": set_name,
                "subset_name": subset_name,
                "card_number": card_number,
                "parallel_variety": parallel_variety,
            }
        )

    c = cg.iloc[0].to_dict()

    p = None
    if parallel_variety:
        pg = pdf[
            (pdf["set_name"] == set_name)
            & (pdf["subset_name"] == subset_name)
            & (pdf["parallel_variety"] == parallel_variety)
        ]
        if not pg.empty:
            p = pg.iloc[0].to_dict()

    if p and p.get("parallel_variety") is not None:
        card_id = f"{set_name}|{subset_name}|{p.get('parallel_variety')}|{card_number}"
    else:
        card_id = c.get("card_id")

    enriched = {
        "card_id": card_id,
        "checklist_id": c.get("card_id"),
        "subset_id": c.get("subset_id"),
        "parallel_id": (p.get("parallel_id") if p else None),
        "set_year": c.get("set_year"),
        "set_name": c.get("set_name"),
        "subset_name": c.get("subset_name"),
        "subset_type": c.get("subset_type"),
        "parallel_variety": (p.get("parallel_variety") if p else None),
        "derived_card_number": c.get("derived_card_number"),
        "card_number": c.get("card_number"),
        "player": c.get("player"),
        "team": c.get("team"),
        "note": c.get("note"),
        "note_check": c.get("note_check"),
        "print_run": (p.get("print_run") if p else None),
        "parallel_note": (p.get("other_note") if p else None),
        "quantity": row.quantity,
        "price": row.price,
        "image_front": row.image_front,
        "image_back": row.image_back,
    }

    return normalize_nulls(enriched)
