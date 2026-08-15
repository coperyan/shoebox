# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `StoresClient` calls bypass the generated `sell_stores_*` wrappers: in
  `ebay_rest` 1.1.4 (latest) those pass `user_access_token=False`, sending an
  application token to an API that only accepts authorization-code-grant user
  tokens — every call failed with HTTP 403 / errorId 1100 regardless of scopes.
  `StoresClient._invoke` re-issues the same call with the user token; drop it
  once upstream fixes the flag.

### Added

- `scripts/refresh_ebay_token.py` — reset / re-consent / persist the eBay REST
  user token. Needed after any scope change: scopes are fixed at consent time,
  and `ebay_rest` neither re-consents while a refresh token is present nor
  writes new tokens back to `ebay_rest.json`. Includes `--check`, `--reset`,
  `--diagnose` (per-scope endpoint probe), and `--verify`.
- The `sell.stores` OAuth scope in `configs/ebay_rest.example.json` and the
  scope/re-mint documentation in `docs/setup.md`.

### Changed

- **Store category management moved from the Trading API to the REST Sell
  Stores API.** New `StoresClient` (`clients/ebay_rest/stores.py`), reachable as
  `EbayClient.stores`. It uses the same OAuth token as the other REST clients,
  so managing categories no longer requires the Auth'n'Auth token in
  `configs/ebay_legacy.json`.
- The Trading-API store category methods added to `eBayLegacyClient` in 0.2.0
  were removed: `get_store_categories`, `add_store_categories`,
  `delete_store_categories`, `move_store_categories`, `rename_store_category`,
  `rename_store_categories`, `get_store_category_update_status`,
  `wait_for_store_category_update`, and the module-level
  `flatten_store_categories` / `STORE_ROOT_CATEGORY_ID`. They shipped in 0.2.0
  with no callers in the repo; the REST equivalents replace them one-for-one.

Behavior differences worth noting when porting:

- eBay's REST endpoints act on **one category per call**. The batch helpers
  (`add_store_categories`, `delete_store_categories`, …) loop, so a run can fail
  partway; they take `stop_on_error` (default `True`) and return per-item
  outcomes.
- Top-level placement is expressed by **omitting** the parent rather than by the
  Trading API's `-999` sentinel.
- The mutating calls are async and return a taskId, but the swagger-generated
  client in `ebay_rest` discards the response body. Use
  `stores.get_store_tasks()` / `get_failed_store_tasks()` to confirm a
  restructure landed, and re-read `get_store_categories()` to pick up newly
  assigned IDs. This replaces `wait_for_store_category_update`.
- `StoreCategoryType` carries `level` natively, so flattened entries take it
  from the API instead of computing it.

## [0.2.0] - 2026-08-15

Adds saved eBay searches (the largest feature area to date), scheduled task
generation, and eBay Store category management, plus a consolidated Marketing
API client.

### Added

**Saved eBay searches** — YAML-declared eBay Browse queries that run on a
schedule, diff against previously seen results, and post only net-new listings
to Slack with photos. See [`docs/search.md`](docs/search.md).

- Three CLI commands: `watch-searches` (run due searches, alert Slack, record
  state), `preview-search` (read-only; shows every listing a search returns and
  which filter rejected each), and `search-aspects` (read-only; lists eBay
  aspects available to filter on, with counts).
- New modules: `models/saved_search.py`, `models/search_hit.py`,
  `clients/search_state.py`, `pipelines/watch_searches.py`,
  `pipelines/preview_search.py`, `transforms/search_filters.py`,
  `utils/search_formatting.py`.
- Config template `configs/searches.example.yml` and BigQuery schema
  `configs/bigquery/schemas/search_hits.json`.
- Settings: `paths.searches_file`, `paths.searches_git_pull` (pull a private
  searches repo before each run), and `slack.search_channel`.

**Scheduled task generation** — Windows Task Scheduler tasks are now generated
from [`scripts/tasks.yaml`](scripts/tasks.yaml) via
[`scripts/generate_tasks.py`](scripts/generate_tasks.py) instead of being
hand-built in the GUI, so schedules and retry policy are reviewable in the repo.
See [`docs/scheduling.md`](docs/scheduling.md).

**eBay Store categories** — `eBayLegacyClient` can now read and restructure the
store category tree via the Trading API: `get_store_categories`,
`add_store_categories`, `delete_store_categories`, `move_store_categories`,
`rename_store_category` / `rename_store_categories`, plus
`get_store_category_update_status` and `wait_for_store_category_update` for
eBay's asynchronous task processing, and a `flatten_store_categories` helper.

**Other**

- `send-offers` gained a headless mode (`--auto`) that sends discount-matrix
  offers without prompting via Slack, and a configurable prompt expiry
  (`--timeout-s`, default 900).
- BigQuery order view and query: `configs/bigquery/views/v_orders_current.sql`,
  `configs/bigquery/queries/tbl_orders_current.sql`.
- Settings: `slack.offers_channel` and `store.condition_descriptor`.
- Test suite grew from 6 files to 18 (395 tests), covering saved-search config,
  filters, formatting, state, the watch and preview flows, send-offers, the
  Slack bot service, store categories, and the marketing client.

### Changed

- **Config example files were renamed** to a consistent `*.example.*` pattern:
  `configs/app.yaml.example` → `configs/app.example.yml`,
  `configs/gcp.json.example` → `configs/gcp.example.json`,
  `configs/ebay_rest.json.example` → `configs/ebay_rest.example.json`,
  `configs/ebay_legacy.json.example` → `configs/ebay_legacy.example.json`.
  Update any local setup scripts that copy these templates.
- Every `sell_marketing_*` call is now routed through `MarketingClient`
  (`clients/ebay_rest/marketing.py`) rather than being spread across
  `EbayClient` and the relist pipeline. Promoted-listings retry handling and
  eBay's ad error codes are defined in one place.
- `EbayClient.promote_new_listing`, `EbayClient.create_volume_discount_promotion`
  and `EbayClient._promote_variation_listing` were removed; their behavior moved
  to `MarketingClient.promote_by_inventory_reference`,
  `create_volume_discount_promotion` and `promote_by_listing_id`. All three had
  been internal to `EbayClient`, so no pipeline call sites changed.
- `create_listings` and the listing builder were reworked around per-listing
  retry and partial-failure handling.
- Slack helpers and the bot service were extended to support search alerts and
  photo-carrying listing messages.

## [0.1.0] - 2026-07-22

Initial public release.

[Unreleased]: https://github.com/coperyan/shoebox/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/coperyan/shoebox/compare/v.0.1.0...v0.2.0
[0.1.0]: https://github.com/coperyan/shoebox/releases/tag/v.0.1.0
