# Data & Storage

How data is laid out across local files, GCS, and BigQuery, and the clients
that move it.

## The universal pattern

Nearly every dataset follows one pipeline shape:

```
source → normalize → local JSONL (exports/jsonl/…) → GCS staging object → BigQuery load job
```

- Loads use explicit schemas from `configs/bigquery/schemas/*.json`
  (name/type/mode per column).
- Monitoring tables load with `WRITE_APPEND` and carry a `file_date` snapshot
  timestamp; "current state" is derived in views by picking the latest
  snapshot. Metadata tables load with `WRITE_TRUNCATE` (full replace).
- After a successful GCS upload + BigQuery load, transient local JSONLs are
  deleted (listing results) or flushed (image append log).

## GCS buckets (`settings.gcs`)

| Bucket setting | Contents | Written by |
|---|---|---|
| `image_bucket` | `images/<set>/<subset>/<parallel|base>/<card#>/<side>__<file>` card scans; `other/<sku>_<side>.png` relist image copies | `ImageLogClient.upload_image`, relist pipelines |
| `metadata_bucket` | `metadata/checklist/…`, `metadata/parallels/…` staging JSONL | `sync-metadata` (`TableAsset`) |
| `ebay_bucket` | `logs/orders/…`, `logs/active_listings/…`, `logs/active_listing_details/…`, `logs/ebay_listings/…` staging JSONL | monitoring pipelines, listing-result sync |
| `image_log_bucket` | `logs/image_log/…` staging JSONL | `ImageLogClient.flush_append_log` |

Card images are made public (their public URLs go into eBay listings).

## BigQuery

Three datasets from `settings.bigquery`, plus the `cards` dataset referenced
by the shipped SQL views/queries:

| Dataset | Table | Loaded by | Schema file | Notes |
|---|---|---|---|---|
| `<checklist_dataset>` | `checklist` | `sync-metadata` | `checklist.json` | Full replace per sync |
| `<checklist_dataset>` | `parallels` | `sync-metadata` | `parallels.json` | Full replace per sync |
| `ebay` | `orders` | `sync-orders` | `orders.json` | One row per order line item (~40 cols from `Order.flattened_line_items`) |
| `ebay` | `active_listings` | `sync-active-listings` | `active_listings.json` | Snapshot incl. impressions/views |
| `ebay` | `active_listing_details` | `sync-active-listing-details` | `active_listing_details.json` | Adds item-specifics + picture URLs as JSON columns |
| `ebay` | `ebay_listings` | `create-listings` / variation pipeline | `ebay_listings.json` | `EbayListingResult` rows: sku, ids, success, full request/response JSON |
| `images` | `image_log` | `ImageLogClient` | `image_log.json` | One row per net-new image upload |

### Views & queries (`configs/bigquery/`)

- `views/v_active_listing_details.sql` — joins the latest `active_listings`
  and `active_listing_details` snapshots (`QUALIFY DENSE_RANK` on
  `file_date`), extracts item-specifics JSON into typed columns
  (player/team/set/parallel/print run/…), and derives a composite `card_id`.
- `queries/checklist_queue.sql`, `queries/parallels_queue.sql` — read the
  `cards.v_checklist` / `cards.v_parallels` views; these power the UI
  dropdowns and queue enrichment.
- `queries/card_metadata.sql` — parameterized single-card lookup
  (`{set_name}` etc., substituted via `format_map` — trusted values only).
- `queries/order_history.sql` — last-30-day non-variation order rollup.

`BigQueryClient.run_query(sql=…)` resolves these **by filename** against
`settings.paths.query_dir`, so the runtime config must point `query_dir` at
`configs/bigquery/queries`.

## Local files (`exports/`, `data/`, `scans_dir`)

| Path | Lifecycle | Purpose |
|---|---|---|
| `exports/jsonl/listing_queue_enriched.jsonl` | Persistent until next queue build | The listing queue (UI or Excel produced) |
| `exports/jsonl/ebay_listings.jsonl` | Deleted after successful GCS+BQ sync | Listing results buffer |
| `exports/jsonl/image_cache.jsonl` | Persistent | Image dedup index — cache key → `ImageLogEntry`; prevents re-uploads across runs |
| `exports/jsonl/image_log_append.jsonl` | Flushed after successful GCS+BQ sync | Net-new uploads awaiting BigQuery |
| `exports/jsonl/orders.jsonl`, `active_listings.jsonl`, `active_listing_details.jsonl` | Overwritten per run | Monitoring staging |
| `data/checklist.csv`, `data/parallels.csv` | Overwritten per sync | Normalized metadata extracts |
| `data/Inputs (Param).xlsm` | User-maintained | Excel queue input |
| `scans_dir/<scan_prefix><id><scan_extension>` | User-maintained | Source card scans (naming set by `scan_prefix`/`scan_number_padding`/`scan_extension`) |
| `scans_dir/GCS/<object path>` | Persistent | Local mirror of every uploaded image |
| `logs/<timestamp>.log` | Per run | File logging via `utils.logging_setup.setup_logging()` |

## Key clients

### `clients/gcs.py` — `GCSClient`
`upload_text`, `upload_file` (optional `make_public` → returns public URL),
`make_public`. Service-account JSON if present, else ADC.

### `clients/bigquery.py` — `BigQueryClient`
`run_query(sql_filename, params)` → DataFrame (reads the SQL file from
`query_dir`, `{param}` substitution); `load_jsonl_from_gcs(bucket,
object_name, dataset, table, schema_path, write_disposition)`.

### `clients/image_log.py` — `ImageLogClient`
The image dedup/upload/log subsystem:

1. `upload_image(file_path, set_name, subset_name, card_number,
   parallel_variety=None, side=None, delete_original=True)`:
   - Computes a cache key (bucket|set|subset|parallel|card|side|filename);
     **cache hit returns the previous entry without uploading**.
   - Uploads to `image_bucket` under a structured object name, makes it
     public, mirrors the file to `scans_dir/GCS/…`, optionally deletes the
     original scan, upserts the cache, and appends to the append-log.
2. `flush_append_log()` — uploads the append-log to `image_log_bucket`,
   loads it into `images.image_log`, and clears it locally on success.
   Called at the end of listing runs.

### `storage/table_asset.py` — `TableAsset`
The metadata loader: CSV rows → per-row transform (validating through the
`ChecklistRow`/`ParallelRow` models) → JSONL → GCS → BigQuery load with an
explicit schema and configurable write disposition.

## Models (`models/`)

Pydantic v2 throughout; the shared base (`models/common.py`) ignores extra
fields, trims strings, and provides `to_bq_json()` (alias-aware,
exclude-None dump used for BigQuery rows).

| Model | Role |
|---|---|
| `ListingQueueRow` | The central queue row: checklist ids + names, parallel info, quantity/price, image paths; `price_scrape_query` property builds the comp-search string |
| `ChecklistRow`, `ParallelRow` | Metadata validation during sync |
| `ImageLogEntry` | One image upload: listing metadata, local source, GCS location, cache key (intentionally no bytes/hash) |
| `EbayListingDraft` | Listing intent: title/description/aspects plus the built `inventory_item` and `offer` payload dicts |
| `EbayListingResult` | Outcome: sku/offer/listing ids, success, error, full request/response blobs |
| `ToppsRelease` | Scraped release with a `stable_key` used for calendar idempotency |
| `models/ebay/*` | Typed `from_api` views of eBay payloads: `InventoryItem` (incl. aspect normalization from stringified dicts), `Offer`, `ItemSummary`, `Order` (with `flattened_line_items` → the `orders` table shape), `NegotiationOffer` |
