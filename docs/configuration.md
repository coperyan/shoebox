# Configuration Reference

All runtime configuration lives in **`configs/app.yaml`** (gitignored;
template: `configs/app.yaml.example`). It is loaded and validated by
`shoebox/settings.py` using Pydantic with `extra="forbid"` at the top
level — unknown top-level keys are rejected, so typos fail fast.

Access pattern throughout the codebase:

```python
from shoebox.settings import get_settings
settings = get_settings()          # cached singleton; no side effects
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
