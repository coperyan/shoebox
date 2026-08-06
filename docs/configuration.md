# Configuration Reference

All runtime configuration lives in **`configs/app.yaml`** (gitignored;
template: `configs/app.example.yml`). It is loaded and validated by
`shoebox/settings.py` using Pydantic with `extra="forbid"` at the top
level — unknown top-level keys are rejected, so typos fail fast.

Access pattern throughout the codebase:

```python
from shoebox.settings import get_settings

settings = get_settings()  # cached singleton; no side effects
settings.slack.notify_channel
```

`ensure_runtime_dirs()` (called by CLI entrypoints) creates the directories
named in `paths`.

## Environment variables

| Variable | Effect |
|---|---|
| `SHOEBOX_CONFIG_PATH` | Path to the app YAML (default `configs/app.yaml`) |
| `EBAY_REST_CONFIG_PATH` | Full path to `ebay_rest.json`, overriding `ebay.path` |
| `GOOGLE_APPLICATION_CREDENTIALS` | Standard ADC override, used when `gcp.service_account_json` doesn't exist |

## Settings schema

### `gcp`

| Key | Type | Description |
|---|---|---|
| `project_id` | str | GCP project for GCS and BigQuery |
| `service_account_json` | str | Path to a service-account key file. If the file doesn't exist, clients fall back to Application Default Credentials (a warning is logged) |

### `bigquery`

| Key | Type | Description |
|---|---|---|
| `checklist_dataset` | str | Dataset for `checklist` / `parallels` tables (metadata sync target) |
| `ebay_dataset` | str | Dataset for eBay snapshots: `orders`, `active_listings`, `active_listing_details`, `ebay_listings` |
| `images_dataset` | str | Dataset for the `image_log` table |

> Note: the metadata **views** queried by the UI/enrichment
> (`v_checklist`, `v_parallels`) live in a `cards` dataset per the shipped SQL
> (`configs/bigquery/queries/*.sql`); adjust the SQL if your dataset naming
> differs.

### `gcs`

| Key | Type | Description |
|---|---|---|
| `image_bucket` | str | Card image uploads (`images/<set>/<subset>/…`) |
| `metadata_bucket` | str | Metadata JSONL staging (`metadata/checklist`, `metadata/parallels`) |
| `ebay_bucket` | str | All eBay log staging (`logs/orders`, `logs/active_listings`, `logs/ebay_listings`, …) |
| `image_log_bucket` | str | Image-log append staging (`logs/image_log`) |

### `paths`

All local working directories; created by `ensure_runtime_dirs()`.

| Key | Typical value | Used by |
|---|---|---|
| `data_dir` | `data` | Metadata CSVs, `Inputs (Param).xlsm` |
| `query_dir` | `configs/bigquery/queries` | `BigQueryClient.run_query` resolves `.sql` filenames here — **must point at the repo's SQL directory** (the pydantic default in older configs was `queries`) |
| `exports_dir` | `exports` | All JSONL outputs (`exports/jsonl/…`) |
| `scans_dir` | (your scans folder) | Source card images; uploaded copies are mirrored to `scans_dir/GCS/…` |
| `scan_prefix` | `SCAN_` | Filename prefix your scanner/camera emits (e.g. `IMG`) |
| `scan_number_padding` | `4` | Zero-pad width for the scan number (`123` → `0123`; set `0` to disable) |
| `scan_extension` | `.jpg` | Scan file extension (leading dot optional) |
| `set_images_dir` | (folder) | Hero/default images for variation listings |
| `tools_dir` | `tools` | Master metadata workbook location |
| `searches_file` | `configs/searches.yaml` | Saved eBay search definitions (a **file**, not a directory — not created by `ensure_dirs()`) |

A bare scan number typed in the UI or listed in the queue workbook is resolved
to `scans_dir/<scan_prefix><zero-padded number><scan_extension>` — e.g. with the
defaults, `123` → `scans/SCAN_0123.jpg`; with `scan_prefix: IMG`,
`scan_number_padding: 5`, `scan_extension: .png`, `123` → `scans/IMG00123.png`.
An entry that is already an existing file path is used as-is.

### `ebay`

| Key | Type | Description |
|---|---|---|
| `application` | str | Entry name in `ebay_rest.json` → `applications` (e.g. `production_1`) |
| `user` | str | Entry name in `ebay_rest.json` → `users` |
| `header` | str | Entry name in `ebay_rest.json` → `headers` (e.g. `US`) |
| `path` | str | **Directory** containing `ebay_rest.json` (default `configs`) |
| `campaign_id` | str | Default promoted-listings campaign used when a flow doesn't choose one via `utils/ad_campaign.py` |

### `slack`

| Key | Type | Description |
|---|---|---|
| `bot_token` | str | Bot User OAuth Token (`xoxb-…`); scopes: `chat:write`, `channels:history`/`groups:history`, `reactions:read` |
| `app_token` | str | App-level token (`xapp-…`) with `connections:write`, for Socket Mode |
| `notify_channel` | str | Channel **ID** for pipeline status + order summaries |
| `pricing_channel` | str | Channel ID for price confirmation/approval prompts |
| `command_channel` | str | Channel ID watched by the `slack-bot` command service |
| `search_channel` | str | Channel ID for saved-search hits (`watch-searches`). Optional — falls back to `notify_channel` when unset, so existing configs keep validating |
| `allowed_user_ids` | list[str] | Optional allowlist for the command bot. Empty (default) = anyone in the channel; non-empty = only these member IDs may run commands (others are ignored and logged) |

### `google_calendar`

| Key | Type | Description |
|---|---|---|
| `calendar_id` | str | Target calendar for the Topps release sync (`…@group.calendar.google.com`). The service account must be granted "Make changes to events" on it |

### `store`

Store-specific identity and eBay-account values used when building listings.
Every field has a placeholder default in `settings.py`, so the section is
optional for the app to *load* — but real values are required before creating
live listings (eBay rejects offers that reference bogus policy IDs).

| Key | Type | Description |
|---|---|---|
| `name` | str | Public store name rendered into the listing description footer |
| `merchant_location_key` | str | eBay inventory location key (Account → Business info → Locations) |
| `category_id` | str | eBay leaf category ID (`261328` = Sports Trading Cards → Singles) |
| `condition` | str | Item condition enum for single-card listings (e.g. `USED_VERY_GOOD`) |
| `offer_message` | str | Message attached to seller-initiated best offers (negotiation API) |
| `fulfillment_low_max_price` | float | Single cards ≤ this price use the "low" fulfillment policy |
| `policies.payment_policy_id` | str | eBay payment business-policy ID |
| `policies.return_policy_id` | str | eBay return business-policy ID |
| `policies.fulfillment_policy_id_low` | str | Fulfillment policy for single cards ≤ `fulfillment_low_max_price` |
| `policies.fulfillment_policy_id_high` | str | Fulfillment policy for single cards above it |
| `policies.fulfillment_policy_id_variation` | str | Fulfillment policy for "You Pick" / variation listings |
| `ad_campaigns.default` | str | Fallback promoted-listings campaign ID |
| `ad_campaigns.current_year` / `current_year_default` | str? | Optional: sets whose name starts with `current_year` use `current_year_default` |
| `ad_campaigns.by_sport` | dict | Campaign IDs keyed by sport (`Basketball`, `Football`) |
| `ad_campaigns.by_set` | dict | Campaign IDs keyed by exact set name |

Routing in `utils/ad_campaign.get_ad_campaign`: `by_sport` → `by_set` →
current-year default → `default`.

## Saved searches (`configs/searches.yaml`)

Definitions for the [`watch-searches`](cli.md#watch-searches) watcher; the
feature as a whole is documented in [search.md](search.md). **This
file is gitignored** — the repo is public and your buy criteria, price ceilings
and blocked sellers are not. The committed template is
`configs/searches.example.yml`.

Validated by `shoebox/models/saved_search.py` with `extra="forbid"` at every
level, so a typo fails at load with its exact location (`searches.0.intervl`)
rather than being silently ignored. Validate without touching Slack or eBay:

```bash
shoebox watch-searches --list
```

The document has a `defaults:` block plus a `searches:` list. **Omitting a key
inherits the default; setting it to `[]` explicitly overrides a non-empty
default with "no filter".** Deliberately flat — there is no nested `filters:`
block, because deep-merge semantics are ambiguous.

### Channel aliases

An optional top-level `channels:` block names each Slack channel once so
searches can reference it by a readable alias:

```yaml
channels:
  card_alerts: C0123456789
  high_value:  C0123456780

defaults:
  channel: card_alerts        # everything lands here unless overridden

searches:
  - name: posey_relic_patch_auto
    channel: high_value       # this one goes elsewhere
```

A `channel:` value is resolved as: a key in `channels:` → its ID; otherwise a
literal Slack channel ID (so configs written before aliases existed still work);
otherwise a load-time error listing the aliases that *do* exist. Values must be
IDs (`C…`/`G…`/`D…`), not `#names` — mixing up the alias and the ID is caught too.

Resolution order for a search's destination, first match wins:

1. `channel:` on the search
2. `channel:` under `defaults:`
3. `slack.search_channel` in `app.yaml`
4. `slack.notify_channel` in `app.yaml`

Note that a `defaults.channel` in this file outranks `slack.search_channel` in
`app.yaml`. Invite the bot to every channel you route to, or Slack returns
`not_in_channel`. `shoebox watch-searches --list` prints each search's resolved
destination.

### Scheduling and volume

| Key | Default | Meaning |
|---|---|---|
| `interval` | `30m` | `<number><unit>`, unit `s`/`m`/`h`/`d`, minimum 60s. **Bare numbers are rejected** — `15` is ambiguous between seconds and minutes |
| `sort` | `newlyListed` | `newlyListed`, `endingSoonest`, `price`, `-price`. Best Match is intentionally unavailable: it returns an arbitrary slice of the result set, so new listings could stay invisible for days |
| `max_results` | `200` | Items fetched per poll. One Browse call returns up to 200 |
| `seed_max_results` | `2000` | Items fetched on the silent first run. Must be ≥ `max_results` — anything matching but not seeded surfaces later as a false "new listing" |
| `max_notify` | `10` | Cap on Slack thread replies per run. Slack permits ~1 message/sec/channel, so this is a time budget as much as a noise budget. Overflow is summarized in one line and recorded, never silently dropped |
| `notify_on_seed` | `false` | Post the first `max_notify` matches when the search seeds, rather than only the confirmation line. Applies to a first-ever seed and to `--reseed`; the automatic recovery seeds (lost cache, long-idle search) stay silent either way. See [search.md](search.md#seeing-the-initial-results) |
| `enabled` | `true` | |
| `channel` | `null` | Slack channel ID; falls back to `slack.search_channel` |
| `prune_seen_after_days` | `90` | Seen-cache entries older than this are dropped at end of run |

### Search identity

| Key | Notes |
|---|---|
| `name` | **Required.** `^[a-z0-9][a-z0-9_-]{0,63}$` — used as a filename for the seen-cache and as part of the dedup key |
| `query` | Max 100 chars (eBay truncates beyond that); `*` wildcards are rejected. Space-separated terms are AND; `(a, b)` is OR |
| `category_ids` | A list, but eBay accepts **exactly one** per request. An L1 category also requires a `query` |
| `price` | `{min, max}`; at least one bound, non-negative, `min ≤ max` |
| `aspects` | `{Aspect: [values]}`. Requires exactly one `category_ids` — eBay's `aspect_filter` must repeat the category ID inside the filter string. Discover the valid names and values with [`search-aspects`](cli.md#search-aspects) |

### Filters: server-side vs post-filtered

This split matters. eBay caps the result set at `max_results` **before** you see
it, so every post-filter consumes result slots: fetching 200 items with a
post-filter that rejects 90% yields 20 usable listings. Push everything eBay
supports server-side.

**Server-side** (sent to eBay, free):

| Key | Emitted filter |
|---|---|
| `buying_options` | `buyingOptions:{FIXED_PRICE\|AUCTION}` |
| `price` + `currency` | `price:[25..200]` + `priceCurrency:USD` |
| `conditions` | `conditions:{NEW\|USED}` |
| `item_location_countries` (one) | `itemLocationCountry:US` |
| `delivery_country` | `deliveryCountry:US` |
| `sellers` / `exclude_sellers` | `sellers:{…}` / `excludeSellers:{…}` — mutually exclusive; caps 250 / 100 |
| `free_shipping_only` | `maxDeliveryCost:0` |

**Post-filtered in Python** (costs result slots):

| Key | Why it can't be server-side |
|---|---|
| `title_exclude` | **eBay Browse has no negative-keyword support at all.** This is the unavoidable one |
| `title_must_include_all` / `_any` | `q` matches the whole listing with Best-Match fuzz, not exact title substrings |
| `seller_min_feedback_score` | No seller-quality filter exists. Unknown feedback fails an explicit threshold |
| `item_location_countries` (2+) | `itemLocationCountry` takes a single value |
| `max_total_price` | eBay filters item price and delivery cost independently; their *sum* needs Python |

To see what a given set of filters actually does to your results — including
which key rejected each listing — use
[`preview-search`](cli.md#preview-search) rather than guessing.

> ⚠️ **eBay returns only `FIXED_PRICE` listings when `buyingOptions` is absent.**
> The filter is therefore always emitted and an empty `buying_options` list is
> rejected — otherwise an auction watcher would silently never fire.

## Other configuration surfaces

- **`configs/ebay_rest.json`** — eBay REST credentials consumed by the
  `ebay_rest` library (applications/users/headers/key_pairs). See the
  instructions embedded in the template.
- **`configs/ebay_legacy.json`** — `{"token": "<Trading API auth token>"}`,
  read by `clients/ebay_legacy.py` (path relative to the current working
  directory, so run from the repo root).
- **`configs/gcp.json`** — standard service-account key referenced by
  `gcp.service_account_json`.
- **`configs/bigquery/schemas/*.json`** — column definitions used for BigQuery
  load jobs (see [data-storage.md](data-storage.md)).
- **`configs/bigquery/queries/*.sql`** — SQL files executed by
  `BigQueryClient.run_query`. `card_metadata.sql` uses `{placeholders}`
  substituted via `str.format_map` — trusted values only.
- **Logging** — configured in code by `shoebox.utils.logging_setup.setup_logging()`,
  called from entrypoints (the CLI and the pipeline `__main__` blocks). It adds a
  console handler at INFO plus a timestamped file handler writing
  `logs/<date>_<time>.log`. Importing a library module never configures logging.

## Hardcoded values worth knowing

Store identity (name, merchant location key, category ID, condition, policy IDs,
ad campaigns, offer message) now lives in the **`store`** config section above.
The following are still hardcoded — changing them means editing code:

| Value | Where |
|---|---|
| eBay condition **descriptor** codes (`40001` / `400010`) | `listing_builder.build_inventory_item_payload`, `variation_listing_builder` |
| Package weight/dimensions (1 oz LETTER, 7×5×1 in) | `listing_builder`, `variation_listing_builder` |
| Store category names (Autographs/Relics/…) and the rules that pick them | `listing_builder.store_category`, `variation_listing_builder.variation_store_category` |
| Listing HTML footer boilerplate (shipping terms) — only the store name is templated | `listing_builder._STORE_FOOTER_TEMPLATE` |
| Item-specific/aspect derivation rules (sport, league, features) | `listing_builder`, `variation_listing_builder` |
