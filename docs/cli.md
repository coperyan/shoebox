# CLI Reference

The console script `shoebox` (equivalently `python -m shoebox.cli`)
is the single entrypoint for all workflows.

On startup the CLI:
1. Configures logging via `shoebox.utils.logging_setup.setup_logging()` — a
   console handler plus a timestamped file under `logs/`.
2. Loads settings and creates the runtime directories (`ensure_runtime_dirs()`).

Run it from the repo root: pipelines read config and SQL via relative paths
(`configs/…`), so the working directory must contain your `configs/`.

```
shoebox <command> [flags]
```

## Metadata & queue building

### `sync-metadata`
Reads the master workbook `tools/checklist_parallel_metadata.xlsm`
(sheets `Checklist`, `Parallels`), normalizes to `data/checklist.csv` /
`data/parallels.csv`, and loads both into BigQuery
(`<checklist_dataset>.checklist` / `.parallels`) via GCS staging with
`WRITE_TRUNCATE` — each run fully replaces the tables.

### `create-queue-excel`
Reads `data/Inputs (Param).xlsm` (sheet `Inputs`), builds `ListingQueueRow`s
(rows missing front/back images are dropped; a bare scan number is resolved to
`scans_dir/<scan_prefix><zero-padded><scan_extension>` per the configured scan
naming convention — see [configuration.md](configuration.md#paths)), enriches
each row against the BigQuery metadata views, and writes
`exports/jsonl/listing_queue_enriched.jsonl`.

### `ui`
Launches the Streamlit queue-builder (`streamlit run shoebox/ui/app.py`).
Cascading Set → Subset → Parallel → Card # dropdowns backed by the metadata
views, image pickers, price/quantity entry; "Save Enriched JSONL" writes the
same `listing_queue_enriched.jsonl` the Excel path produces.

## Listing creation

### `create-listings`
Consumes `exports/jsonl/listing_queue_enriched.jsonl` and creates eBay
listings (inventory item → offer → publish → promote). Results are appended to
`exports/jsonl/ebay_listings.jsonl`, then synced to GCS + BigQuery and deleted
locally.

| Flag | Effect |
|---|---|
| `--dry-run` | Build payloads and log results without calling eBay |
| `--publish` | Publish offers after creation (otherwise offers are created unpublished) |
| `--schedule` | Set `listingStartDate` ≈ now + 19 days on each listing |
| `--scrape-prices` | Scrape 130point sold comps per card, then ask the Slack pricing channel to confirm/override before listing (see [slack.md](slack.md)) |

Offers are created **unpublished** unless `--publish` is passed.

### `create-variation-listings`
Builds multi-variation ("You Pick") listings from an inventory workbook. Each
`Subset ID` group in the sheet becomes one listing whose variations are the
individual cards; per-card scans and a hero image are optional.

| Flag | Effect |
|---|---|
| `--excel-path PATH` | **Required.** Inventory workbook (`.xlsx`) to read |
| `--sheet-name NAME` | Worksheet to read (default `Checklist`) |
| `--dry-run` | Build payloads and log results without calling eBay |
| `--publish` | Publish after creation (otherwise created unpublished) |
| `--schedule` | Set `listingStartDate` ≈ now + 19 days |
| `--in-stock-only` | Skip cards with no quantity |
| `--images-dir DIR` | Directory of per-card scans named by card number |
| `--default-image-path PATH` | Hero image shown before a variation is selected |

## Lifecycle

### `relist-listings`
Finds aged listings (≥ 90 days, price > $2, ≥ 100 impressions, no watchers by
default), re-hosts their images in GCS, reprices, and relists via
`refresh_listing_flow` (old offer withdrawn, new offer published, promotion
re-created). For listings over $1.99 the new price requires **Slack approval**
(Approve button, or threaded reply to override); timeout or an unrecognized
reply skips that listing. Cheaper/no-view listings are auto-repriced by rule.

| Flag | Effect |
|---|---|
| `--schedule` | Spread relist start dates over the next 1–20 days |
| `--no-scrape` | Skip 130point price scraping (approval prompt still fires, proposing the current price) |
| `--dry-run` | Log the repricing decision for each listing without contacting eBay or Slack |

### `end-oos-listings`
Ends every active "out of stock" listing (quantity − sold = 0) via the Trading
API and notifies the Slack notify channel with the count.

### `send-offers`
Finds listings with interested buyers (Negotiation API), applies the standard
markdown matrix to compute the offer price, and sends each eligible buyer a
24-hour offer.

| Flag | Effect |
|---|---|
| `--dry-run` | Log the offers that would be sent without contacting eBay |
| `--max-price N` | Only send offers on listings priced at or below `N` (default `19.99`) |

## Monitoring

### `sync-active-listings`
Snapshots all active listings (Trading API) merged with 90-day
impression/view metrics (Analytics API) into
`ebay.active_listings` (JSONL → GCS → BigQuery, `WRITE_APPEND`). Sends
start/complete notifications to Slack.

### `sync-active-listing-details`
Fetches full `GetItem` detail (item specifics, pictures, condition) for every
active non-variation listing into `ebay.active_listing_details`. Slower —
one Trading API call per listing.

### `sync-orders`
Pulls up to ~2 years of FULFILLED orders (Fulfillment API, windowed into
85-day chunks), flattens to one row per line item, and appends to
`ebay.orders`.

### `orders-awaiting-shipment`
Displays and/or sends to Slack the pull list of unshipped orders (last 30
days, `NOT_STARTED`).

| Flag | Effect |
|---|---|
| `--display` | Render tables in the terminal (interactive, Enter to advance) |
| `--message` | Send to the Slack notify channel: one parent summary message with each section (non-variation pull list, per-variation-listing lists, per-buyer lists) as a thread reply |
| `--pull-order` | Include the pull-list sections |
| `--buyer-order` | Include the per-buyer sections |

## Slack & calendar

### `slack-bot`
Starts the long-running Slack command bot (Socket Mode). Watches the command
channel for messages like `sync-orders` or `/create-listings --dry-run`,
validates them against a whitelist, runs the corresponding CLI command in a
subprocess (10-minute cap), and replies in a thread with the output in a code
block. Optionally restricted by `slack.allowed_user_ids`. See
[slack.md](slack.md) for the whitelist and security notes. Run it under a
process supervisor (systemd, launchd, pm2) for always-on operation.

### `sync-topps-calendar`
Scrapes topps.com/release-calendar (real Chrome; the site 403s plain HTTP) and
upserts each upcoming release as a 9:00 AM PT Google Calendar event (reminders
1 day + 30 min). Idempotent — events carry a stable key in extended
properties; re-runs patch instead of duplicate. New events are announced to
the Slack notify channel.

Note: the CLI invocation runs with `dry_run=False, headless=False` (a visible
browser). For a headless/dry-run invocation use the module directly:
`python -m shoebox.pipelines.sync_topps_calendar --dry-run`.

## Not exposed via the CLI

- **`services/orders_awaiting_shipment.display_orders`** defaults differ when
  imported directly (display on, message off).
