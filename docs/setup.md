# Setup

## Requirements

- **Python ≥ 3.11** (the code uses 3.11+ features; `TimeoutError` handling
  relies on the 3.11 `asyncio.TimeoutError` alias).
- **Google Chrome** on the host — the price scraper (130point.com) and the
  Topps release scraper drive a real browser via `undetected-chromedriver`
  (both sites block plain HTTP).
- Accounts/credentials for: an **eBay developer application** (production
  keys), a **GCP project** (GCS + BigQuery, and the Calendar API if using the
  Topps sync), and a **Slack workspace** where you can create an app.
- Excel is only needed if you use the Excel-based queue input
  (`data/Inputs (Param).xlsm`) or update the master metadata workbook.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
pip install -U pip
pip install -e .
```

This installs the `shoebox` console script. All dependencies (including
`ebay_rest`, `slack-bolt`, `aiohttp`, Google clients, Selenium, Streamlit) come
from `pyproject.toml`; there is no `requirements.txt`.

## Configuration files

Five files live in `configs/` and are **gitignored** — create each from its
checked-in `.example` template:

| File | Template | Purpose |
|---|---|---|
| `configs/app.yaml` | `app.example.yml` | Central application settings (validated by `settings.py`; see [configuration.md](configuration.md)) |
| `configs/ebay_rest.json` | `ebay_rest.example.json` | eBay REST credentials for the `ebay_rest` library (applications / users / headers / key_pairs) |
| `configs/ebay_legacy.json` | `ebay_legacy.example.json` | eBay Trading API auth token (`{"token": "..."}`) |
| `configs/gcp.json` | `gcp.example.json` | GCP service-account key (standard downloadable JSON) |
| `configs/searches.yaml` | `searches.example.yml` | Saved eBay searches for `watch-searches` (optional; only needed for that command) |

```bash
cp configs/app.example.yml configs/app.yaml
cp configs/ebay_rest.example.json configs/ebay_rest.json
cp configs/ebay_legacy.example.json configs/ebay_legacy.json
cp configs/gcp.example.json configs/gcp.json
cp configs/searches.example.yml configs/searches.yaml   # optional
# then edit each with real values
```

`app.example.yml` validates as-is against the settings schema, so a straight
copy is a working starting point. The config path can be overridden with the
`SHOEBOX_CONFIG_PATH` environment variable.

## eBay setup

1. Create an eBay developer account and a production keyset:
   <https://developer.ebay.com/api-docs/static/creating-edp-account.html>
2. Fill the `applications` / `users` sections of `configs/ebay_rest.json`
   (see the instructions block inside the template and the
   [`ebay_rest` docs](https://github.com/matecsaj/ebay_rest)). The entry names
   you use (e.g. `production_1`, header `US`) must match `app.yaml`'s
   `ebay.application` / `ebay.user` / `ebay.header`.
3. `ebay.path` in `app.yaml` is the **directory** containing `ebay_rest.json`
   (default `configs`); the full file path can be overridden with the
   `EBAY_REST_CONFIG_PATH` environment variable.
4. Obtain a Trading API auth token for the seller account and place it in
   `configs/ebay_legacy.json`. The Trading client reads
   `configs/ebay_legacy.json` relative to the working directory, so run
   commands from the repo root.
5. Scopes needed by the REST user token (pre-filled in the template):
   `sell.inventory`, `sell.marketing`, `sell.account`, `sell.fulfillment`,
   `sell.analytics.readonly`, plus the base `api_scope`.

## Google Cloud setup

1. Create (or reuse) a GCP project; enable **Cloud Storage** and **BigQuery**.
2. Create a service account, grant it object write on the four buckets and
   BigQuery load/append on the datasets, download its JSON key to
   `configs/gcp.json`, and set `gcp.service_account_json` in `app.yaml`.
   - If the file referenced by `gcp.service_account_json` does not exist, the
     GCS/BigQuery clients fall back to **Application Default Credentials**
     (`gcloud auth application-default login` for local development).
3. Create the four GCS buckets referenced in `app.yaml` (`image_bucket`,
   `metadata_bucket`, `ebay_bucket`, `image_log_bucket`) and the BigQuery
   datasets (`checklist_dataset`, `ebay_dataset`, `images_dataset`).
4. For the Topps calendar sync only: enable the **Google Calendar API**, share
   the target calendar with the service-account email ("Make changes to
   events" — service accounts cannot self-grant), and put the calendar ID in
   `google_calendar.calendar_id`.

## Slack app setup

The Slack integration uses **Socket Mode** (no public URL required) with the
`slack-bolt` library. One-time setup at <https://api.slack.com/apps>:

1. **Create the app** (from scratch) in your workspace.
2. **Socket Mode** → toggle on; create an **app-level token** with the
   `connections:write` scope. This is `slack.app_token` (`xapp-…`).
3. **OAuth & Permissions → Bot Token Scopes** — add:
   - `chat:write` — send messages (all `notify*` functions)
   - `channels:history` — receive messages in public channels (thread replies,
     command parsing); use `groups:history` instead/additionally for private
     channels
   - `reactions:read` — receive reaction events (legacy reaction helper)
4. **Event Subscriptions** → enable, then under *Subscribe to bot events* add
   `message.channels` (and `message.groups` for private channels) and
   `reaction_added`. With Socket Mode there is no Request URL to verify.
5. **Interactivity & Shortcuts** → toggle **Interactivity on**. This is
   required for the Approve button in the price-approval flow; with Socket
   Mode no Request URL is needed — just the toggle.
6. **Install to Workspace** (reinstall after any scope change). The **Bot User
   OAuth Token** (`xoxb-…`) is `slack.bot_token`.
7. **Invite the bot** to each channel it uses: `/invite @YourBot` in the
   notify, pricing, and command channels.
8. Collect the three **channel IDs** (channel details → ID at the bottom) into
   `app.yaml`: `notify_channel`, `pricing_channel`, `command_channel`.
9. Optional but recommended: restrict the command bot with
   `slack.allowed_user_ids` (your member ID: profile → "…" → *Copy member ID*).
   Empty list = anyone in the command channel can run commands.

See [slack.md](slack.md) for how each piece is used at runtime.

## First run

```bash
# 1. Verify settings load and create runtime dirs
shoebox --help

# 2. Push metadata to BigQuery (needs tools/checklist_parallel_metadata.xlsm)
shoebox sync-metadata

# 3. Smoke-test Slack
python -c "
from shoebox.settings import get_settings
from shoebox.utils.slack import notify
notify(get_settings().slack.notify_channel, 'shoebox is configured 🎉')"

# 4. Build a queue and dry-run a listing
shoebox ui                      # or: shoebox create-queue-excel
shoebox create-listings --dry-run
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Missing config file: configs/app.yaml` | Copy the template (see above) or set `SHOEBOX_CONFIG_PATH` |
| Google auth errors (`DefaultCredentialsError`) | Point `gcp.service_account_json` at a real key, or `gcloud auth application-default login` |
| `Missing ebay_rest config file` | Create `configs/ebay_rest.json`; check `ebay.path` / `EBAY_REST_CONFIG_PATH` |
| `Missing 'token' in configs/ebay_legacy.json` | Fill in the Trading API token |
| Slack messages send but replies/buttons never arrive | Event Subscriptions or Interactivity not enabled, bot not invited to the channel, or app not reinstalled after scope changes |
| Button clicks do nothing | Interactivity toggle is off (step 5 above) |
| `FileNotFoundError` for a `.sql` file | `paths.query_dir` must point at the directory holding the repo's SQL (`configs/bigquery/queries`) |
| Scraper opens no browser / 403s | Chrome not installed, or version mismatch — `undetected-chromedriver` pins to the installed Chrome version |
| eBay "offer already exists" | Handled automatically by `existing_offer_action`; for manual runs, delete or update the stale offer |
