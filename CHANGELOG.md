# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **iOS form.** *Clear* and *Save* sit in the Queue tab's top bar (*Save* reads
  *Update* while editing a row; *Edit* for reordering moved next to the queue
  heading); the app has an icon; the scan-number field uses the number pad; a
  *Done* button above the keyboard (and a scroll, or Return on the Settings
  fields) dismisses it; a new row's
  price starts at 0.99 and is no longer carried over from the previous row.

### Added

- **Listed scans leave the OneDrive scans folder.** When `create-listings`
  fetches a scan from OneDrive (the Cloud Run job started from the iOS app),
  it now moves that scan into `<scans_folder>/<onedrive.listed_subfolder>`
  (`Listed` by default, created on first use) once the scan has been
  uploaded -- the counterpart of the workstation deleting the local original,
  so the scans folder only holds cards still to be listed. Best-effort: a
  failed move is logged and the listing stands. Needs the `Files.ReadWrite`
  Graph scope, now the `onedrive.scopes` default; add it to the Azure app
  registration and sign in again (`scripts/onedrive_login.py --login --reset`,
  then `--upload`). `utils/image_refs.materialize_scan` reports where each
  image came from; `archive_scan` does the move; `OneDriveClient.move` is the
  Graph call.
- **App pickers and queue loading.** The iOS Set picker is sectioned by year
  (newest first) from a new per-set `set_year` in `GET /metadata`; subsets
  list `Base` first, then A–Z, in the API, the Streamlit UI and the app
  (`subset_sort_key`); pressing Search in a picker selects the best match
  (`PickerSearch`: exact, then prefix, then contains); and *Load last
  submitted queue…* / opening a named draft show a preview of the first 5
  rows before anything replaces the working queue (`QueuePreviewSheet`).
- **iOS listing-queue app and its API.** A SwiftUI app (`ios/ShoeboxQueue`,
  built with XcodeGen) recreates the Streamlit queue builder on an iPhone:
  the same Set → Subset → Parallel → Card # cascade, autofill, quantity and
  price, scan numbers typed as in the Excel sheet or photos picked from the
  library, a locally auto-saved working queue plus named drafts, enriched
  preview and submit. It talks to a new FastAPI service, `shoebox api`
  (`shoebox/api/`), deployed from the same image as a Cloud Run **service**
  (`services:` in `deploy/jobs.yaml`, `scripts/deploy_cloud_run.py
  --services-only`, `allUsers` invoker + `X-API-Key` from the new
  `shoebox-api-key` secret). The API serves the metadata views as JSON
  (cached an hour, `ETag`/`304`), enriches rows with the unchanged
  `build_enriched_json`, writes `listing_queue_enriched.jsonl` and a
  `listing_queue_draft.json` to the new `paths.queue_uri` GCS object, and
  stages phone photos under `uploads/` in the image bucket. See `docs/api.md`.
- **Listing from the app, in the cloud.** `POST /queue/listings` on the API
  writes a run record (`<queue dir>/runs/<run_id>.json`) and starts the new
  Cloud Run job `create-listings` (`shoebox create-listings --run-request`),
  which claims the record with a generation-matched write, lists the queue
  with the publish/schedule choices the app sent, never scrapes prices, and
  reports progress per row to the record the app polls (`GET
  /queue/listings[/{run_id}]`). Scan numbers are fetched from OneDrive through
  Microsoft Graph (`clients/onedrive.py`, MSAL device-code sign-in via
  `scripts/onedrive_login.py`, token cache kept in GCS because refresh tokens
  rotate); phone photos come from GCS as before. New settings sections
  `onedrive` and `cloud_run`; `deploy/jobs.yaml` entries take `bindings:`
  (extra IAM grants) so the API's identity can start the job. `run_listings`
  returns a `RunSummary`, takes a progress `reporter`, and always flushes the
  image log and syncs results, even when interrupted. See `docs/api.md`.
- `create-listings` reads the queue from `paths.queue_uri` when set, or from
  `--queue PATH_OR_GS_URI`, and refuses a queue carrying enrichment error
  rows. Image references of all three kinds (path, bare scan number, `gs://`
  photo) go through the new `utils/image_refs.materialize_image`, so the
  dedup cache, naming, mirror and image log are unchanged.

- **Combined front+back picture.** `create-listings` now uploads a third
  picture, front and back side by side (`utils/images.combine_front_back`,
  ported from `combine_images.py`), for every row with both scans. It goes
  after the scans, so the front stays picture 1, which is the search
  thumbnail and the image in the send-offers Slack prompts. It is built before
  the scans upload, because uploading deletes the originals, and named after
  the two scans so a re-run hits the upload cache. A failure here logs a
  warning and the listing goes ahead with two pictures.
  `create-variation-listings` does the same per card. New
  `add-combined-images` backfills live listings: preview by default, then
  `--apply [--limit N]`. For each single-card listing with exactly two
  pictures, it re-hosts the live pictures in GCS the way relist does, then
  sets front, back, and combined on the inventory item, round-tripping
  everything else. The pictures come from `GetItem` because eBay's inventory
  item often lists only the front. Pillow is now a declared dependency (it was
  already installed through Streamlit).
- **`variation-template`**: lists a whole subset as "You Pick" variation
  listings from the checklist. `variation-template build "2026 Topps Update"
  --subset Base` writes a workbook with every card (US1–US350) in card-number
  order, split into segments (default 2) of at most 250 cards, eBay's
  per-listing variation cap. You fill in Qty and Price per card, plus a default
  price, ad rate and volume-discount tiers on the Listing sheet.
  `variation-template upload` validates the workbook, reporting every problem
  by row before anything reaches eBay, and creates one variation listing per
  segment, titled with its card range. Every card is listed, with a blank Qty
  showing as sold out, so the dropdown always covers the full checklist. See
  `docs/cli.md`.

- **`watch-searches` on Cloud Run.** The saved-search watcher joins the daily
  syncs as a Cloud Run job on a 5-minute Cloud Scheduler cron (shipped
  paused until the cutover). New `paths.searches_state_uri: gs://bucket/prefix`
  keeps the seen-caches, run state, hits buffer and lock in GCS
  (`clients/search_state_gcs.py`). Each run takes a generation-matched lock
  object, downloads the state to a scratch directory, runs the unchanged
  watcher, and uploads what changed after each search. SIGTERM still saves and
  releases, and a lock from a killed run goes stale after 20 minutes. Every
  host naming the same URI shares one state and one lock, so the workstation
  and the Slack bot's `watch-searches --reseed` stay consistent with the
  cloud. `paths.searches_file` may now be a `gs://` object. It is deployed by
  a new Cloud Build trigger on the private searches repo
  (`deploy/searches-cloudbuild.yaml`), which validates with the new
  `scripts/validate_searches.py` before copying, so a broken edit fails the
  commit instead of the job. `scripts/migrate_search_state_to_gcs.py` moves
  the workstation's state across with no reseed. In Cloud Run the watcher
  refuses to start without durable state. See `docs/deployment.md`, "Saved
  searches".
- `sync-watch-list` is now a Cloud Run job too, at 04:30 daily like its
  Windows task.
- **Failure alerting** for the Cloud Run jobs. `deploy/alerts/job-failed.yaml`
  is a Cloud Monitoring policy on failed job executions, grouped per job, that
  fires on the first failure and carries the log-reading and re-run commands
  for that pipeline in its notification body. Because the jobs never retry, a
  failed run is final, and it would otherwise show up only as a "Completed"
  message that never arrived. `scripts/create_alert_channel.py` points the
  alerts at the Slack channel the pipelines already use, reusing the existing
  bot token rather than installing a second Slack app. See `docs/deployment.md`.
- **Cloud Run deployment** for the four daily syncs (`sync-orders`,
  `sync-active-listings`, `sync-active-listing-details`, `end-oos-listings`),
  so they no longer depend on a workstation being awake. `Dockerfile` builds
  one image whose entry point is the `shoebox` CLI; `deploy/jobs.yaml`
  declares each Cloud Run job and its Cloud Scheduler cron (the cloud
  counterpart of `scripts/tasks.yaml`); `scripts/deploy_cloud_run.py` renders
  it into `gcloud` commands (`--list`, `--dry-run`, `--only`, `--jobs-only`);
  `deploy/cloudbuild.yaml` builds, pushes and rolls the image out on every
  push to `main`. The gitignored config files are mounted from Secret Manager
  and the job's service account replaces `configs/gcp.json`, since the GCS
  and BigQuery clients already fall back to Application Default Credentials.
  A local macOS or Windows checkout is unaffected. See `docs/deployment.md`.
- `tcdb-wantlist`: exports your tcdb.com want list to CSV or JSONL
  (`shoebox tcdb-wantlist`, `--all-categories`, `--format jsonl`). The want
  list is the collection view filtered to want status
  (`ViewCollectionMode.cfm?...&Filter=W`), not `Wantlists.cfm`, which is other
  members' lists; the pager is followed 100 cards at a time. One row per card:
  title, ViewCard link, set/card/item ids, quantity, price, category. The
  signed-in username is read from the nav, so no account is hardcoded.
  Read-only. New `clients/tcdb/collection.py` parser.
- `tcdb-wantlist` also breaks each row's title into `set_year`, `set_name`
  (master set, so `2026 Topps Chrome` even for a subset), `subset_name`
  (`Base` when there is none), `card_number` and `player`, and reads the row's
  note codes (`RC`, `VAR`, `XRC`, `ERR`) with the `<figcaption>` explaining
  them. Card numbers keep the shapes TCDB really uses -- `NNO`, `PP 3`,
  `GAA-BB`, `131 / 292`, and `2 DS` where the code abbreviates the subset --
  while a description after the number (`#4 NL Batting Average Leaders`) stays
  with the player; checked against all 4,018 rows of the live list. `--limit N`
  caps the run, and `--with-team` adds each card's team at the cost of one page
  load per card.
- `tcdb-add`: adds cards to your tcdb.com collection from their TCDB titles
  (`shoebox tcdb-add "2009 Bowman Chrome - X-Fractors #171 Matt Cain"`, or
  `--file` with one per line) or ViewCard links. Each title is found with an
  advanced search on year + card number + name. Only an exact title match is
  added, so parallels are never confused, and zero or several matches are
  reported and skipped. The card page's own Quick Add button is clicked and
  the resulting `CollectionAdd*_ajax.cfm` request is checked. Cards already in
  the collection are skipped unless `--allow-duplicates`. `--dry-run` finds
  cards without adding them. `tcdb-search` sessions gain `add N`.
- `sync-watch-list`: snapshots the account's eBay watch list into
  `ebay.watch_list` (JSONL → GCS → BigQuery, `WRITE_APPEND`), each listing
  enriched with `GetItem` item specifics, category, condition, and pictures.
  Listings whose details can't be fetched are kept with `detail_error` set.
- `tcdb-search`: interactive advanced search on tcdb.com. Drives a real Chrome
  (undetected-chromedriver, persistent profile so Cloudflare clearance and the
  TCDB login survive between runs), prompts you to log in by hand once per
  session, then takes card numbers one at a time and searches them with saved
  defaults (`--name Bonds`, `tcdb.search_defaults`, or `set year=1993` inside
  the session). Matches print as a table and the results page stays open in
  Chrome so the card can be added to the collection. New `tcdb` config section,
  `shoebox/clients/tcdb/` package and `models/tcdb.py`, laid out so further
  TCDB actions are one parser plus one browser method each (see
  `docs/tcdb.md`).

### Changed

- `build_indexes` (the dropdown order) moved from `ui/app.py` to
  `transforms/queue_indexes.py`, shared by the UI and the API.
- `GCSClient` gained `upload_bytes` and `read_text`; `clients/gcs.py` gained
  `download_gs_file`. `normalize_nulls` treats `pd.NA` as null.
- `fastapi`, `uvicorn[standard]` and `python-multipart` are base dependencies;
  `httpx` is a dev dependency (FastAPI's test client).
- `setup_logging()` honours `SHOEBOX_LOG_DIR`; `-` or empty logs to the console
  only, for hosts that collect stdout and discard the filesystem. Unset
  behaves as before (`logs/<timestamp>.log`).

### Fixed

- **`enhance-listing-titles` deleted back pictures.** An inventory retitle
  replaces the whole item, and it sent back the item's own `imageUrls`.
  eBay's `getInventoryItem` often returns only the front, so every retitled
  listing lost its back: all 302 listings created with two pictures that now
  show one had been retitled. Retitles now send the live listing's pictures
  (`GetItem`, re-hosted in GCS like relist, in the new
  `services/listing_pictures.py`). An inventory retitle without an item ID
  now refuses instead of writing. `inventory_item_body_with_title` is
  removed. `add-combined-images --restore-backs` puts the backs back from
  GCS (1,175 of the 1,181 affected listings have a stored back).
- `relist-listings` names re-hosted pictures by position (`front`, `back`,
  `combined`). It used to call everything after picture 1 `back`, so a third
  picture overwrote the back in GCS and the relisted listing showed the
  composite twice and lost the back.

- **Chrome on Apple Silicon.** TCDB commands, the 130point price scraper and
  the Topps release scraper now start Chrome on an arm64 Mac that has no
  Rosetta. undetected-chromedriver 3.5.5 downloaded the Intel chromedriver
  (`Bad CPU type in executable`). Its in-place patch also broke the arm64
  driver's code signature, so macOS killed it (`Status code was: -9`).
  `clients/chromedriver.py` now downloads `mac-arm64` and ad-hoc re-signs the
  driver after uc patches it. It does nothing on other platforms. See
  `docs/tcdb.md`, "Apple Silicon Macs".
- Variation listings: per-card scans are matched by card number without
  matching longer numbers, so `US1` no longer picks up `US10`–`US199`'s
  images. `create-variation-listings --dry-run` no longer needs eBay
  credentials.
- `watch-searches` no longer rejects a listing whose aspect value arrives
  comma-joined. `getItem` returns a multi-valued aspect as one string —
  `Features: "Insert, Serial Numbered, Parallel/Variety"` — rather than one
  entry per value, so the new aspect check compared that whole string against a
  configured `Serial Numbered` and dropped a card that plainly is one. eBay's
  own `aspect_filter` indexes those values separately, which is why the search
  returned the listing at all; only the re-check read them as one opaque value.
  Each value is now matched against its comma-separated parts, which cannot
  lose a comma-bearing value because a *configured* value can never contain a
  comma either (`SavedSearch` rejects that at load time, as eBay documents no
  escape for `,` inside `aspect_filter`). Parts are still compared whole, so an
  abbreviation like `SP` does not match `Short Print`. Rejected listings are
  recorded as seen, so listings already dropped by this bug stay suppressed
  until their search is `--reseed`ed.
- `watch-searches` no longer alerts on listings that violate a search's
  `aspects`. eBay's relevance backfill returns listings that ignore
  `aspect_filter`, and because those listings still carry the query words in
  their titles they passed `require_query_in_title` untouched — an
  "autographed or serial-numbered" watch would post a burst of plain base
  cards. New listings are now confirmed against their own item specifics
  (`verify_aspects`, on by default) before anything is posted. The check costs
  one Browse `getItem` per new listing, since a search result carries no item
  specifics at all, so it is capped per run by `verify_aspects_budget`
  (default 40): listings over the budget, and listings whose lookup fails, are
  left for the next run rather than alerted unchecked or dropped. A rejected
  listing is recorded as seen so its lookup is paid for once. A seed that posts
  (`notify_on_seed`) verifies the listings it is about to show, but not the
  rest of the seed window: those are recorded and never alerted, so leaving
  them unverified costs nothing, and checking a full 2000-item window would
  not.
- `watch-searches` wrote its end-of-run `last_seen_at`/`last_price` refresh to
  a scope named after the *search* instead of the channel scope every other
  write in the run uses, so on any run that posted an alert the refresh missed
  the real cache. Since pruning keys off `last_seen_at`, a long-lived listing
  in a busy channel could age out of the seen-cache while still being returned,
  and then re-alert as new.
- `sync-active-listing-details` no longer throws away a whole sweep because one
  listing failed, and no longer writes a snapshot that quietly omits listings:
  failures are collected per item, and a run that loses more than 10% of the
  store aborts before writing. Running out of eBay's daily call allowance
  (error 518) now raises `TradingQuotaExceeded` and stops immediately instead
  of firing hundreds more calls into the same wall.
- `enhance-listing-titles` handles listings that have no SKU. They were created
  outside the Sell Inventory API, which cannot see them, so they now go through
  Trading `ReviseFixedPriceItem` by item ID (new
  `TradingClient.revise_listing_title`); a failure on the inventory route
  falls back to the same path. Previously a missing SKU arrived as pandas' NaN,
  passed a `str(value or "")` guard as the truthy string `"nan"`, and reached
  eBay as a float.
- `enhance-listing-titles --apply` wrote one row per listing again. Its results
  were merged back on `sku`, which is blank for Trading-API listings, so those
  rows joined to each other — 132 SKU-less listings turned into ~18,000 report
  rows. (The sweep itself was unaffected; only the report multiplied.)
- A title update no longer sends back packaging with a missing or zero weight,
  which eBay rejects with errorId 25020 and which killed the update for
  listings whose inventory record carries incomplete package details.
- `StoresClient` calls bypass the generated `sell_stores_*` wrappers: in
  `ebay_rest` 1.1.4 (latest) those pass `user_access_token=False`, sending an
  application token to an API that only accepts authorization-code-grant user
  tokens — every call failed with HTTP 403 / errorId 1100 regardless of scopes.
  `StoresClient._invoke` re-issues the same call with the user token; drop it
  once upstream fixes the flag.

### Added

- `TradingClient.get_watch_list` / `get_watch_list_items` — the account's
  buyer-side watch list via Trading `GetMyeBayBuying` (`WatchList`
  container). Pages like the selling lists; the flattened rows carry item_id,
  title, seller, current price, Buy It Now price, bid count, time left, end
  time, and the listing URL.
- **`enhance-listing-titles`** — improve the titles of listings already live.
  Reads a listings dataframe — by default the BigQuery view
  `<ebay_dataset>.v_active_listing_details`, or any CSV / JSONL with `item_id`,
  `sku`, `title`, and a team via `--input` — and, inside eBay's
  80-character limit: repairs mis-encoded characters, strips dead phrases,
  expands `(RC)` → `Rookie` and `AU` → `Auto`, drops `MEM`, and adds the team
  short name — none of it repeated when the title already says it. Writes a
  full per-listing report to `exports/csv/`; `--apply` pushes the changes via
  `createOrReplaceInventoryItem`, so listings keep their ID, watchers, and
  search standing. New `pipelines/enhance_listing_titles.py`,
  `transforms/title_enhancer.py`, and `EbayClient.update_listing_title`.
- Character repair for titles that went through a bad encoding hop and are live
  on eBay as mojibake ("Vidal BrujÃ¡n" for "Vidal Bruján") — 19 of them in the
  current active set. `title_enhancer.normalize_characters` undoes the damage
  and folds the result to plain ASCII; `has_illegal_characters` reports which
  titles carry anything outside printable ASCII.
- `configs/title_crosswalk.yaml` — the team short-name crosswalk (MLB, NFL, NBA,
  plus historical, minor-league, and college names), the title shorthand
  expansions, the tokens and phrases to drop, and the synonyms that suppress a
  redundant expansion. Loaded by `utils/title_crosswalk.py`; editing the YAML is
  now all it takes to teach the store a new team or a new title rule.
- `scripts/refresh_ebay_token.py` — reset / re-consent / persist the eBay REST
  user token. Needed after any scope change: scopes are fixed at consent time,
  and `ebay_rest` neither re-consents while a refresh token is present nor
  writes new tokens back to `ebay_rest.json`. Includes `--check`, `--reset`,
  `--diagnose` (per-scope endpoint probe), and `--verify`.
- The `sell.stores` OAuth scope in `configs/ebay_rest.example.json` and the
  scope/re-mint documentation in `docs/setup.md`.
- `send-offers` is now a Cloud Run job. It runs interactively every 15
  minutes from 08:00 to 21:45 Pacific, with a 10-minute reply window and a
  14-minute timeout, so runs never overlap. It needs only the eBay APIs and
  Slack. See `docs/deployment.md`, "send-offers".

### Changed

- **eBay client package reorganized.** `shoebox/clients/ebay_rest/` is now
  `shoebox/clients/ebay/` (the old name collided with the `ebay_rest` PyPI
  package), and the Trading API client moved in beside the REST sub-clients as
  `clients/ebay/trading.py` / `TradingClient` (was `clients/ebay_legacy.py` /
  `TradingClient`). Import paths change; behavior does not.
- **`EbayClient` is a composition root.** The five listing workflows
  (`create_listing_from_inventory_flow`, `refresh_listing_flow`,
  `create_variation_listing_flow`, `update_listing_title`,
  `update_listing_store_categories`) moved to `ListingService` in
  `shoebox/services/listings.py` as `create_listing`, `relist_listing`,
  `create_variation_listing`, `update_title`, `update_store_categories`. Each
  spans inventory + marketing or inventory + trading, so none belonged on a
  single client. Pipelines construct `ListingService(EbayClient())`.
- **`InventoryClient` is real.** It was a stub with broken methods while the
  facade made raw `sell_inventory_*` calls; it now owns
  `get_offers` / `find_offer` (typed `Offer`, empty on the 25713 "no offer
  yet" 404), `create/update/delete/withdraw/publish_offer`,
  `publish_offer_by_group`, `upsert_inventory_item(_group)`, and the 25001
  transient retry, applied consistently to the idempotent calls. Pipelines no
  longer reach through `ebay_api.api` for inventory calls.
- **Typed errors.** REST failures raise `EbayApiError` (`.error_id` is eBay's
  `errorId`) instead of `ebay_rest.Error`; the ids the clients react to are
  named constants in `clients/ebay/errors.py`.
- **The Trading client is built lazily** on first `ebay.trading` access, from
  `ebay.trading_token_path` in `app.yaml` (default `configs/ebay_legacy.json`).
  REST-only pipelines no longer need the Trading token file. `legacy_api` is
  gone; use `trading`.
- `TradingClient`'s three GetMyeBaySelling methods share one pager,
  `get_my_ebay_selling_items(list_name, sort=...)`, which also returns the raw
  item nodes for callers that need fields the flattened shape drops. `dig` and
  `ensure_list` are public.
- **`sync-active-listing-details` runs its `GetItem` calls concurrently** —
  ~1,650 listings in about two minutes instead of twenty-plus. New
  `TradingClient.get_item_details_bulk` spreads the calls over a
  `ThreadPoolExecutor` (default 12 workers, `--workers` to change), feeding the
  pool in a sliding window so an early stop can cancel the rest. There is no
  batch alternative: `GetSellerList` returns no item specifics at any detail
  level (verified: 0 of 200 on a full page) and Browse `getItems` needs
  partner-level access this account lacks.
- Every Trading API call now goes through a pooled `requests.Session` instead
  of a bare `requests.post`, so connections are reused across calls. Worth ~4%
  on its own; it also keeps 12 concurrent workers from churning connections.
  No automatic retries — this session carries mutating calls, and a silent
  replay of `ReviseFixedPriceItem` is worse than a visible error.
- `utils/shorten_team.py` is now `utils/title_crosswalk.py`, reading its team
  map from `configs/title_crosswalk.yaml` instead of a hardcoded dict.
  `shorten_team_name` keeps its signature (unmapped teams still return `None`);
  the map gained NFL, NBA, minor-league, and college coverage, so
  non-baseball listings now get a team in their titles.
- **Store category management moved from the Trading API to the REST Sell
  Stores API.** New `StoresClient` (`clients/ebay_rest/stores.py`), reachable as
  `EbayClient.stores`. It uses the same OAuth token as the other REST clients,
  so managing categories no longer requires the Auth'n'Auth token in
  `configs/ebay_legacy.json`.
- The Trading-API store category methods added to `TradingClient` in 0.2.0
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
