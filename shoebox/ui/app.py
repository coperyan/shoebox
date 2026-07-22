from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import streamlit as st

from shoebox.models.listing_queue import ListingQueueRow
from shoebox.settings import ensure_runtime_dirs, get_settings
from shoebox.transforms.queue_enrichment import (
    build_enriched_json,
    normalize_nulls,
)
from shoebox.transforms.queue_enrichment import (
    load_checklist_and_parallels as _load_checklist_and_parallels,
)
from shoebox.ui.helpers import handle_image_path
from shoebox.utils.jsonl import read_jsonl, write_jsonl

load_checklist_and_parallels = st.cache_data(show_spinner=True, ttl=3600)(
    _load_checklist_and_parallels
)

# Load settings lazily (Streamlit runs this module as the entrypoint).
settings = get_settings()
ensure_runtime_dirs()


# Where your enriched JSONL is saved today
jsonl_path = Path(settings.paths.exports_dir) / "jsonl/listing_queue_enriched.jsonl"


# -----------------------------
# OPTIONAL: Windows file picker
# -----------------------------
def pick_file_windows(title: str) -> str | None:
    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title=title,
            filetypes=(
                ("PNG files", "*.png"),
                ("JPG files", "*.jpg;*.jpeg"),
                ("All files", "*.*"),
            ),
            initialdir=settings.paths.scans_dir,
        )
        return path or None
    finally:
        if root is not None:
            root.destroy()


# -----------------------------
# Helpers
# -----------------------------
# -----------------------------
# BigQuery: load ONCE (cached)
# -----------------------------


def build_indexes(cdf: pd.DataFrame, pdf: pd.DataFrame) -> dict[str, object]:
    """
    Precompute lookups so dropdowns are fast and in-memory.
    Sort Card # by derived_card_number (numbers first as ints, then text),
    but display/return the actual card_number.
    """

    def _cardno_sort_key(derived: object) -> tuple:
        if derived is None or (isinstance(derived, float) and pd.isna(derived)):
            return (2, float("inf"), "")
        s = str(derived).strip()
        if not s:
            return (2, float("inf"), "")
        if re.fullmatch(r"\d+", s):
            return (0, int(s), "")
        return (1, float("inf"), s.upper())

    # Sets from checklist (authoritative)
    sets = sorted([x for x in cdf["set_name"].dropna().unique().tolist()])

    subsets_by_set: dict[str, list[str]] = {}
    cardnos_by_set_subset: dict[tuple[str, str], list[str]] = {}
    parallels_by_set_subset: dict[tuple[str, str], list[str]] = {}

    # Subsets from checklist
    for s, g in cdf.groupby("set_name", dropna=False):
        if pd.isna(s):
            continue
        subsets_by_set[str(s)] = sorted([x for x in g["subset_name"].dropna().unique().tolist()])

    # Card numbers: sort by derived_card_number, return/display card_number
    for (s, sub), g in cdf.groupby(["set_name", "subset_name"], dropna=False):
        if pd.isna(s) or pd.isna(sub):
            continue
        key = (str(s), str(sub))

        tmp = (
            g.loc[:, ["card_number", "derived_card_number"]]
            .dropna(subset=["card_number"])
            .drop_duplicates(subset=["card_number"])
        )

        pairs = list(
            zip(tmp["derived_card_number"].tolist(), tmp["card_number"].tolist(), strict=False)
        )
        pairs.sort(key=lambda t: _cardno_sort_key(t[0]))

        cardnos_by_set_subset[key] = [card_no for _, card_no in pairs]

    # Parallels come from parallels view
    for (s, sub), g in pdf.groupby(["set_name", "subset_name"], dropna=False):
        if pd.isna(s) or pd.isna(sub):
            continue
        key = (str(s), str(sub))
        parallels_by_set_subset[key] = sorted(
            [x for x in g["parallel_variety"].dropna().unique().tolist()]
        )

    return {
        "sets": sets,
        "subsets_by_set": subsets_by_set,
        "cardnos_by_set_subset": cardnos_by_set_subset,
        "parallels_by_set_subset": parallels_by_set_subset,
    }


def reset_dependents(row: ListingQueueRow, changed: str) -> ListingQueueRow:
    if changed == "set_name":
        row.subset_name = ""
        row.parallel_variety = ""
        row.card_number = ""
    elif changed == "subset_name":
        row.parallel_variety = ""
        row.card_number = ""
    return row


def hydrate_rows_from_enriched_jsonl(path: Path) -> None:
    recs = read_jsonl(path)
    if not recs:
        return

    rows: list[ListingQueueRow] = []
    for r in recs:
        # Rebuild only what your UI needs to resume
        rows.append(
            ListingQueueRow(
                set_name=r.get("set_name", "") or "",
                subset_name=r.get("subset_name", "") or "",
                parallel_variety=r.get("parallel_variety", "") or "",
                card_number=r.get("card_number", "") or "",
                quantity=r.get("quantity", 1) or 1,
                price=r.get("price", 0.0) or 0.0,
                image_front=r.get("image_front", "") or "",
                image_back=r.get("image_back", "") or "",
            )
        )

    st.session_state["rows"] = rows
    st.session_state["edit_index"] = None
    # optional: keep draft empty, or set to first row, etc.


# -----------------------------
# App
# -----------------------------
st.set_page_config(page_title="Sports Card Listing UI", layout="wide")
st.title("Sports Card Listing UI (Single Form → Queue → JSON/JSONL Export)")

cdf, pdf = load_checklist_and_parallels()
idx = build_indexes(cdf, pdf)

with st.sidebar:
    st.subheader("Cache / Dimensions")
    st.write(f"v_checklist rows: **{len(cdf):,}**")
    st.write(f"v_parallels rows: **{len(pdf):,}**")
    st.write(f"Sets: **{len(idx['sets']):,}**")
    if st.button("Clear cached data + rerun"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.subheader("Resume")
    if st.button("Load last saved JSONL"):
        hydrate_rows_from_enriched_jsonl(jsonl_path)
        st.success("Loaded queue from last saved JSONL.")
        st.rerun()


# --- Single-form state ---
if "rows" not in st.session_state:
    st.session_state["rows"] = []  # saved queue rows

if "draft" not in st.session_state:
    st.session_state["draft"] = ListingQueueRow()  # the one form

if "edit_index" not in st.session_state:
    st.session_state["edit_index"] = None  # None = add mode


# # Auto-resume if app restarted and queue is empty
# if not st.session_state["rows"] and jsonl_path.exists():
#     hydrate_rows_from_enriched_jsonl(jsonl_path)


def sync_widget_keys_from_draft(force: bool = False) -> None:
    """
    Copy draft -> widget keys BEFORE widgets are instantiated.
    Only call this at the top of the script (pre-render).
    """
    d: ListingQueueRow = st.session_state["draft"]

    mapping = {
        "d_set": d.set_name,
        "d_subset": d.subset_name,
        "d_parallel": d.parallel_variety,
        "d_cardno": d.card_number,
        "d_qty": int(d.quantity or 1),
        "d_price": float(d.price or 0.0),
        "d_front": d.image_front,
        "d_back": d.image_back,
    }

    for k, v in mapping.items():
        if force or (k not in st.session_state):
            st.session_state[k] = v


if st.session_state.get("_sync_widgets", False):
    sync_widget_keys_from_draft(force=True)
    st.session_state["_sync_widgets"] = False
else:
    sync_widget_keys_from_draft(force=False)


def reset_draft_form(keep_metadata: bool = True) -> None:
    prev: ListingQueueRow = st.session_state.get("draft", ListingQueueRow())

    st.session_state["draft"] = ListingQueueRow(
        set_name=prev.set_name if keep_metadata else "",
        subset_name=prev.subset_name if keep_metadata else "",
        parallel_variety=prev.parallel_variety if keep_metadata else "",
        card_number="",
        quantity=1,
        price=prev.price if keep_metadata else "",
        image_front="",
        image_back="",
    )
    st.session_state["edit_index"] = None

    # Trigger widget-key sync on next rerun (BEFORE widgets instantiate)
    st.session_state["_sync_widgets"] = True


def metadata_update_draft() -> None:
    row = st.session_state["draft"]
    set_name = st.session_state.get("d_set")
    subset_name = st.session_state.get("d_subset")
    parallel_variety = st.session_state.get("d_parallel", None)
    card_number = st.session_state.get("d_cardno", None)

    if card_number:
        cg = cdf[
            (cdf["set_name"] == set_name)
            & (cdf["subset_name"] == subset_name)
            & (cdf["card_number"] == card_number)
        ]
        if not cg.empty:
            c = cg.iloc[0].to_dict()
            row.player = c.get("player")
            row.team = c.get("team")
            row.set_year = c.get("set_year")
            row.subset_type = c.get("subset_type")
            row.note = c.get("note")
            row.note_check = c.get("note_check")

    if parallel_variety:
        pg = pdf[
            (pdf["set_name"] == set_name)
            & (pdf["subset_name"] == subset_name)
            & (pdf["parallel_variety"] == parallel_variety)
        ]
        if not pg.empty:
            p = pg.iloc[0].to_dict()
            row.print_run = p.get("print_run", None)
            row.parallel_note = p.get("other_note", None)

    st.session_state["draft"] = row


def on_pick_front_draft() -> None:
    picked = pick_file_windows("Select FRONT image")
    if picked:
        picked_str = str(picked)
        st.session_state["d_front"] = picked_str
        st.session_state["draft"].image_front = picked_str


def on_pick_back_draft() -> None:
    picked = pick_file_windows("Select BACK image")
    if picked:
        picked_str = str(picked)
        st.session_state["d_back"] = picked_str
        st.session_state["draft"].image_back = picked_str


def on_change_front_draft() -> None:
    path = handle_image_path(str(st.session_state.get("d_front", "")))
    if path:
        path_str = str(path)
        st.session_state["d_front"] = path_str
        st.session_state["draft"].image_front = path_str


def on_change_back_draft() -> None:
    path = handle_image_path(str(st.session_state.get("d_back", "")))
    if path:
        path_str = str(path)
        st.session_state["d_back"] = path_str
        st.session_state["draft"].image_back = path_str


# -----------------------------
# Main form (only one)
# -----------------------------
st.markdown("### Add / Edit Row")

draft = st.session_state["draft"]
set_options = [""] + idx["sets"]
mode = "Edit" if st.session_state["edit_index"] is not None else "Add"
st.caption(f"Mode: **{mode}**")

c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
c5, c6 = st.columns([1, 1])
c7, c8 = st.columns([4, 4])

chosen_set = c1.selectbox(
    "Set Name",
    options=set_options,
    index=set_options.index(draft.set_name) if draft.set_name in set_options else 0,
    key="d_set",
)
if chosen_set != draft.set_name:
    draft.set_name = chosen_set
    draft = reset_dependents(draft, "set_name")
    st.session_state["draft"] = draft

subsets = idx["subsets_by_set"].get(draft.set_name, []) if draft.set_name else []
subset_options = [""] + subsets
chosen_subset = c2.selectbox(
    "Subset Name",
    options=subset_options,
    index=(subset_options.index(draft.subset_name) if draft.subset_name in subset_options else 0),
    key="d_subset",
)
if chosen_subset != draft.subset_name:
    draft.subset_name = chosen_subset
    draft = reset_dependents(draft, "subset_name")
    st.session_state["draft"] = draft

parallels = (
    idx["parallels_by_set_subset"].get((draft.set_name, draft.subset_name), [])
    if (draft.set_name and draft.subset_name)
    else []
)
parallel_options = [""] + parallels
draft.parallel_variety = c3.selectbox(
    "Parallel / Variety (optional)",
    options=parallel_options,
    index=(
        parallel_options.index(draft.parallel_variety)
        if draft.parallel_variety in parallel_options
        else 0
    ),
    key="d_parallel",
    on_change=metadata_update_draft,
)

cardnos = (
    idx["cardnos_by_set_subset"].get((draft.set_name, draft.subset_name), [])
    if (draft.set_name and draft.subset_name)
    else []
)
cardno_options = [""] + cardnos
draft.card_number = c4.selectbox(
    "Card #",
    options=cardno_options,
    index=(cardno_options.index(draft.card_number) if draft.card_number in cardno_options else 0),
    key="d_cardno",
    on_change=metadata_update_draft,
)

draft.quantity = c5.number_input(
    "Quantity",
    min_value=1,
    max_value=999,
    value=int(draft.quantity or 1),
    step=1,
    key="d_qty",
)
draft.price = c6.number_input(
    "Price",
    min_value=0.0,
    value=float(draft.price or 0.0),
    step=0.50,
    format="%.2f",
    key="d_price",
)

# Images
front_cols = c7.columns([6, 1])
draft.image_front = front_cols[0].text_input(
    "Image Front (path or file name)",
    key="d_front",
    on_change=on_change_front_draft,
)
front_cols[1].button("Pick", on_click=on_pick_front_draft, key="d_front_pick")

back_cols = c8.columns([6, 1])
draft.image_back = back_cols[0].text_input(
    "Image Back (path or file name)",
    key="d_back",
    on_change=on_change_back_draft,
)
back_cols[1].button("Pick", on_click=on_pick_back_draft, key="d_back_pick")

st.session_state["draft"] = draft

left, right = st.columns([1, 1])

if left.button("✅ Save (Add/Update)", use_container_width=True):
    draft = st.session_state["draft"]
    if st.session_state["edit_index"] is None:
        st.session_state["rows"].append(draft)
    else:
        st.session_state["rows"][st.session_state["edit_index"]] = draft

    reset_draft_form(keep_metadata=True)  # keep set/subset/parallel
    st.rerun()

if right.button("↩ Cancel / New", use_container_width=True):
    reset_draft_form(keep_metadata=False)  # full reset
    st.rerun()


# -----------------------------
# Preview + Edit/Delete buttons
# -----------------------------
st.markdown("### Preview (Queue Rows)")

if not st.session_state["rows"]:
    st.info("No rows added yet.")
else:
    preview_mode = st.radio(
        "Preview mode",
        ["Queue fields", "Enriched JSON (per row)", "Enriched JSON (flattened table)"],
        horizontal=True,
        key="preview_mode",
    )

    if preview_mode == "Queue fields":
        preview_df = pd.DataFrame(
            [r.model_dump(exclude_none=False) for r in st.session_state["rows"]]
        )
        st.dataframe(preview_df, use_container_width=True)

    elif preview_mode == "Enriched JSON (per row)":
        # Build enriched per row and show full detail using st.json (no truncation)
        for i, r in enumerate(st.session_state["rows"]):
            rec = build_enriched_json(r, cdf, pdf)
            rec = normalize_nulls(rec) if rec is not None else None

            with st.expander(
                f"{i + 1}. {r.set_name} | {r.subset_name} | {r.parallel_variety} | {r.card_number}",
                expanded=False,
            ):
                if rec is None:
                    st.warning("No enriched record returned for this row.")
                else:
                    st.json(rec)

    else:  # "Enriched JSON (flattened table)"
        enriched_records: list[dict] = []
        for r in st.session_state["rows"]:
            rec = build_enriched_json(r, cdf, pdf)
            if rec is not None:
                enriched_records.append(normalize_nulls(rec))

        if not enriched_records:
            st.warning("No enriched records to preview.")
        else:
            # Flatten nested JSON into columns (great for scanning/filtering)
            flat_df = pd.json_normalize(enriched_records, sep="__")
            st.dataframe(flat_df, use_container_width=True)

    st.divider()

    # Keep your edit/delete controls (still operates on queue rows)
    for i, r in enumerate(st.session_state["rows"]):
        a, b, c = st.columns([8, 1, 1])
        with a:
            st.caption(
                f"{i + 1}. {r.set_name} | {r.subset_name} | {r.parallel_variety} | {r.card_number}"
            )
        with b:
            if st.button("Edit", key=f"edit_{i}"):
                st.session_state["draft"] = ListingQueueRow(**r.model_dump())
                st.session_state["edit_index"] = i
                st.session_state["_sync_widgets"] = True
                st.rerun()

        with c:
            if st.button("Delete", key=f"del_{i}"):
                st.session_state["rows"].pop(i)
                if st.session_state["edit_index"] == i:
                    reset_draft_form()
                st.rerun()


# -----------------------------
# Output JSON + Downloads
# -----------------------------
st.markdown("### Output JSON (Enriched)")

enriched_records: list[dict] = []
for r in st.session_state["rows"]:
    rec = build_enriched_json(r, cdf, pdf)
    if rec is not None:
        enriched_records.append(normalize_nulls(rec))

json_text = json.dumps(enriched_records, indent=2, ensure_ascii=False)
st.code(json_text, language="json")

st.download_button(
    "Download Enriched JSON",
    data=json_text.encode("utf-8"),
    file_name="listing_queue_enriched.json",
    mime="application/json",
)

# Save JSONL to disk
jsonl_path = Path(settings.paths.exports_dir) / "jsonl/listing_queue_enriched.jsonl"

if st.button("💾 Save Enriched JSONL"):
    write_jsonl(jsonl_path, enriched_records)
    st.success(f"Saved JSONL to: {jsonl_path.resolve()}")
