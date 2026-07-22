# Pipelines & Services

Detailed reference for each workflow. CLI flags are covered in [cli.md](cli.md);
this document explains what each one actually does internally.

---

## Metadata

### `pipelines/sync_metadata.py`

**Master workbook → BigQuery.**

1. `update_csvs()` reads `tools/checklist_parallel_metadata.xlsm` (sheets
   `Checklist`, `Parallels`), normalizes column names, drops any column not in
   the corresponding BigQuery schema JSON, coerces `print_run` to nullable
   int, and `unidecode`s player names. Writes `data/checklist.csv` and
   `data/parallels.csv`.
2. Two `storage.TableAsset`s then run the standard load: CSV → transform per
   row (`transforms/checklist_normalize.py` / `parallels_normalize.py`, which
   validate through the `ChecklistRow` / `ParallelRow` models) → JSONL →
   `gs://<metadata_bucket>/metadata/{checklist,parallels}/…` → BigQuery
   `<checklist_dataset>.checklist` / `.parallels` with **`WRITE_TRUNCATE`**.

Downstream consumers never read the tables directly — they query the views
`cards.v_checklist` / `cards.v_parallels` (see [data-storage.md](data-storage.md)).

---

## Queue building

Both entry points end at the same file: `exports/jsonl/listing_queue_enriched.jsonl`.

### `ui/app.py` (Streamlit)

- Loads the two metadata views once per hour (`st.cache_data(ttl=3600)`) via
  `transforms/queue_enrichment.load_checklist_and_parallels()`.
- `build_indexes` precomputes cascading dropdowns: Set → Subset → (Parallel) →
  Card #, with card numbers sorted numerically first.
- Selecting a card autofills player/team/year/notes/print-run from metadata.
- Image paths can be typed or picked via a native (tkinter) file dialog rooted
  at `scans_dir`; `ui/helpers.handle_image_path` resolves a bare scan number to
  `scans_dir/<scan_prefix><zero-padded><scan_extension>` using the configured
  scan naming convention (see [configuration.md](configuration.md#paths)).
- Rows accumulate in session state with add/edit; preview modes show raw queue
  fields, per-row enriched JSON, or a flattened table.
- "Save Enriched JSONL" enriches every row (`build_enriched_json`) and writes
  the queue file. A sidebar action can re-hydrate the session from the last
  saved file.

### `pipelines/load_listing_queue_from_excel.py`

- `get_input_df()` reads `data/Inputs (Param).xlsm` sheet `Inputs`, dropping
  rows without both images. (The workbook's dropdowns are fed from the master
  metadata workbook via Power Query.)
- Each row becomes a `ListingQueueRow`; enrichment and output are identical to
  the UI path.

### Enrichment (`transforms/queue_enrichment.py`)

`build_enriched_json(row, checklist_df, parallels_df)` joins on
(set, subset, card #) and, when a parallel is selected, (set, subset,
parallel). It attaches ids (`card_id`, `checklist_id`, `subset_id`,
`parallel_id`), year, player/team, notes, and print run. A row with no
checklist match yields an `{"error": "No checklist match", …}` record instead
of a queue row — check the output file for `error` keys before listing.

---

## Listing creation

### `pipelines/create_listings.py`

Per queue row:

1. **Pricing (only with `--scrape-prices`)** — build a 130point search query
   (`price_scraper.search_helper`, excluding graded/PSA), scrape sold comps,
   take the outlier-trimmed mean, round up to an `x.x9` price point. Then ask
   the Slack pricing channel to confirm: reply `Y`/`YES` (or let it time out)
   to accept, or reply a number to override (tolerant parsing — `$4.99` etc.).
   If scraping fails, the prompt proposes the original queue price instead.
2. **Images** — front/back scans upload through `ImageLogClient` (dedup cache;
   see [data-storage.md](data-storage.md)), producing public GCS URLs.
3. **Payload** — `transforms/listing_builder.build_draft` assembles the title
   (80-char limit with progressive element-stripping), HTML description,
   full item-specifics dict, store category, condition, package, and policy
   IDs into an `EbayListingDraft` with ready-to-send `inventory_item` and
   `offer` dicts. `--schedule` sets a listing start date ~19 days out.
4. **eBay** — `EbayClient.create_listing_from_inventory_flow`: upsert
   inventory item (retries transient error 25001), delete-or-update any
   existing offer for the SKU, create offer, publish (if `--publish`), promote
   into the campaign chosen by `utils/ad_campaign.get_ad_campaign`.
5. **Logging** — every row appends an `EbayListingResult` (full request +
   response payloads) to `exports/jsonl/ebay_listings.jsonl`; after the loop
   the image-log is flushed and the results file is uploaded to GCS, loaded
   into BigQuery, and deleted locally.

### `pipelines/create_variation_listing.py` (CLI: `create-variation-listings`)

Multi-variation "You Pick / Complete Your Set" listings:

- Reads an inventory Excel (sheet `Checklist`; columns incl. Subset ID, Card
  Number, Display Name, Qty, Price), groups rows by Subset ID.
- Per group: uploads a default hero image plus any per-card images discovered
  by filename match in an images dir; builds one inventory item per card
  (variation dimension `Card` = display name), an inventory-item group, and
  one offer per SKU (per-variation pricing; distinct free-shipping fulfillment
  policy); then `create_variation_listing_flow` publishes by group and
  promotes (default 20% rate).
- Optional **volume-discount promotion** (e.g. buy 2 → 15% off) — creation
  retries error 38227 up to 10× while eBay's marketing API catches up to the
  new listing.
- SKUs/group keys are SHA1-derived (`build_variation_sku`, `build_group_key`)
  so re-runs address the same objects.

### SKU scheme

Single-card SKUs are deterministic:
`utils/sku.build_sku_50` = SHA1 of
`set_year|set_name|subset_name|parallel_variety|card_number|player`,
uppercased and padded to 50 chars. The same card always maps to the same SKU,
which is what lets relist flows find existing inventory items and offers.

---

## Lifecycle

### `pipelines/relist_listings.py`

- Candidate set: active non-variation listings ≥ 90 days old, price > $2,
  ≥ 100 impressions, no watchers; processed high-price-first. `--schedule`
  spreads new start dates over 1–20 days.
- Images are copied from the live listing to GCS (`$_57` high-res variant).
- Pricing: listings > $1.99 optionally get a scraped comp price, then a
  **Slack approval** (Approve button / threaded override). Timeout or an
  unparseable reply **skips the listing** — it stays live untouched. Cheap
  no-view listings step down a fixed ladder ($1.99 → … → $0.99); everything
  else gets the standard markdown matrix.
- Relist: `refresh_listing_flow` — delete old ad, replace inventory item,
  withdraw + delete old offer, create + publish new offer, re-promote (7%).
- Failures are collected per row and printed; the loop continues.

### `pipelines/end_oos_listings.py`

Trading API: find active listings with zero remaining quantity, end them all
(`NotAvailable`), notify Slack with the count.

### `pipelines/send_offers.py`

Negotiation API: `find_eligible_items()` → per listing, skip if price >
`max_price` (default $19.99) → offer price = standard markdown of current
price → send a 1-day offer with a fixed message to interested buyers.
`--dry-run` logs the offers without sending them.

---

## Monitoring

All three follow: eBay → normalize → `exports/jsonl/<name>.jsonl` →
`gs://<ebay_bucket>/logs/<name>/<name>_<timestamp>.jsonl` → BigQuery
`ebay.<table>` (`WRITE_APPEND`, snapshot keyed by `file_date`).

| Pipeline | Source | Table | Notes |
|---|---|---|---|
| `sync_active_listings.py` | Trading `GetMyeBaySelling` + Analytics traffic report (90 days) | `active_listings` | Sends start/complete Slack notifications |
| `sync_active_listing_details.py` | Trading `GetItem` per listing | `active_listing_details` | Skips "complete your set" variation listings; 1 API call per listing |
| `sync_orders.py` | Fulfillment API, FULFILLED, ~2 years windowed | `orders` | One row per line item via `Order.flattened_line_items`; constructs clients at import time |

---

## Services

### `services/orders_awaiting_shipment.py`

Pulls `NOT_STARTED` orders from the last 30 days and derives a pull list:
variation cards get parsed card numbers/names; rows sort numerically. Two
outputs:

- `display_df` — interactive rich tables in the terminal (Enter to advance).
- `message_df` — Slack: one parent summary message
  (`📦 Orders Awaiting Shipment — N buyer(s), M item(s)`) with each section
  posted as a thread reply (non-variation pull list incl. buyer, one table per
  variation listing, one table per buyer). Tables auto-degrade to a stacked
  layout on mobile-width content (see [slack.md](slack.md)).

### `services/slack_bot_service.py`

The chat-command bot; documented in [slack.md](slack.md).

---

## Running pipelines

Pipelines are import-safe: none construct clients, read config, or configure
logging at module import, so you can import them from notebooks or other code
without triggering network or credential access. Clients and settings are built
inside each pipeline function when it runs.

Run pipelines from the repo root — they resolve `configs/` and the BigQuery
schema/SQL files via relative paths, so the working directory must contain your
`configs/`. Logging is set up by the entrypoint (the `shoebox` CLI, or the
`__main__` block when a pipeline is run as a module).
