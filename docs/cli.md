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

## Saved searches

### `watch-searches`
Runs the saved eBay searches defined in `configs/searches.yaml` that are **due**,
and posts net-new listings to Slack — one parent message per search with each
listing as a thread reply. See
[configuration.md](configuration.md#saved-searches-configssearchesyaml) for the
YAML schema and [pipelines.md](pipelines.md#saved-search-watcher) for the
internals.

| Flag | Effect |
|---|---|
| `--force` | Ignore intervals; run every enabled search now |
| `--dry-run` | Log what *would* be posted. No Slack, no state writes, no GCS/BigQuery |
| `--only NAME` | Restrict to one search (repeatable). Still interval-gated unless combined with `--force` |
| `--reseed NAME` | Discard that search's seen-cache and silently re-seed (repeatable) |
| `--config PATH` | Override `paths.searches_file` |
| `--no-flush` | Skip the GCS/BigQuery flush this run |
| `--list` | Validate the config and list searches; run nothing |

A search's **first run seeds silently**: every current match is recorded and a
single confirmation line is posted, but no per-item alerts. Only listings that
appear afterwards alert. The same silent re-seed happens if the seen-cache is
lost or a search has been idle for more than 6× its interval — in both cases the
matches are stale and alerting on them would just be noise.

### `preview-search`
Shows **every** listing a saved search returns — including the ones your
post-filters rejected, and which config key rejected each. This is the tool for
tuning a search; `watch-searches --dry-run` only prints a 15-row sample.

| Flag | Effect |
|---|---|
| `--csv PATH` | Write the full result set, all columns, for spreadsheet work |
| `--passed-only` | Hide rejected listings (they're shown by default — usually the interesting ones) |
| `--max-results N` | Cap the fetch. Defaults to the search's `seed_max_results`, so you see the breadth a seed would |
| `--rows N` | Rows to print (default 40; `0` prints all) |
| `--config PATH` | Override `paths.searches_file` |

```bash
shoebox preview-search matt_cain_autos --csv exports/preview.csv
```

Output is the exact request sent to eBay, a pass/reject count, a breakdown of
which filters rejected how many, and the listing table. Writes nothing — no
Slack, no seen-cache, no BigQuery — so it's safe to run repeatedly while tuning.

It's also importable, which is the better tool for real analysis:

```python
from shoebox.pipelines.preview_search import preview_search

df = preview_search("matt_cain_autos")
df[df.passed].sort_values("total_price")        # what would alert, cheapest first
df[~df.passed].dropped_by.value_counts()        # what the filters are costing
```

The frame has one row per listing: `passed`, `dropped_by`, `title`, `price`,
`shipping`, `total_price`, `buying`, `condition`, `seller`, `feedback`,
`country`, `listed`, `item_id`, `url`.

### `search-aspects`
Lists the eBay **aspects** (structured item attributes — Player/Athlete, Season,
Parallel/Variety, Grade, Set…) available to filter a saved search on, with the
match count for every value.

Aspects are category-specific *and* result-set-specific, so this is scoped to a
saved search rather than being a static list.

| Flag | Effect |
|---|---|
| `--top N` | Values shown per aspect (default 8) |
| `--csv PATH` | Write every aspect/value pair |
| `--config PATH` | Override `paths.searches_file` |

```bash
shoebox search-aspects matt_cain_autos --top 5
```

Copy a name/value pair straight into the search's `aspects:` block. Aspect
filtering happens on eBay's side, so unlike `title_exclude` it costs you no
result slots — for one real search, adding `Parallel/Variety: ["Gold"]` took 184
results down to 11.

Also importable: `aspect_options(name)` returns a DataFrame of `aspect`,
`value`, `count`, `values_in_aspect`.

> Note: `ebay_rest`'s paginating wrapper lists `refinement` in its internal
> `page_controls` and discards it, so no `fieldgroups` argument to
> `browse.search()` can surface aspects. `BrowseClient.aspect_refinements()`
> goes through the SDK's single-response path instead.

**Scheduling.** One cron entry drives everything; each search's own `interval`
decides whether it actually fires, so adding a search means editing YAML only.

```bash
*/5 * * * * cd /Users/you/shoebox && ./.venv/bin/shoebox watch-searches >> logs/cron_watch_searches.log 2>&1
```

The `cd` is required — pipelines resolve `configs/…` relative to the working
directory. Concurrent runs are prevented by a lock file
(`exports/jsonl/searches/.lock`): if a previous run is still posting, the new
tick logs and exits 0.

On macOS, `cron` needs Full Disk Access granted to `/usr/sbin/cron`. The
supported alternative is a `launchd` agent with `StartInterval 300`, a
`WorkingDirectory` of the repo root, and `StandardOutPath` under `logs/`.

**API budget.** Each due search costs one Browse call per run (a single call
returns up to 200 items). Ten searches on a 15-minute interval is roughly 960
calls/day against a default Browse ceiling of ~5,000/day. Dropping every search
to a 5-minute interval would be ~2,900/day — still under, but tighten
`max_results` before adding many more searches at that cadence.

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
