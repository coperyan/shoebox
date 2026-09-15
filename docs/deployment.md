# Deployment

How to run the recurring pipelines on Cloud Run instead of a workstation, and
which pipelines deliberately stay put.

The container is an **additional host**, not a replacement. Everything in
[`shoebox/`](../shoebox) is cross-platform: the only platform branch in the
package is the file lock in
[`clients/search_state.py`](../shoebox/clients/search_state.py), which
dispatches `msvcrt` on Windows and `fcntl` elsewhere and is tested both ways.
A Mac or Windows checkout keeps working exactly as it does today, and the two
scheduling systems coexist — [`scripts/tasks.yaml`](../scripts/tasks.yaml)
stays the source of truth for Windows Task Scheduler, and
[`deploy/main.tf`](../deploy/main.tf) is its cloud counterpart.

Terraform and the step-by-step setup live in
[`deploy/README.md`](../deploy/README.md). This page is the reasoning.

---

## What runs where

| Workload | Host | Why |
|---|---|---|
| `sync-orders`, `sync-active-listings`, `sync-active-listing-details`, `end-oos-listings` | **Cloud Run job** | API → JSONL → GCS → BigQuery. No browser, no local input, no state between runs |
| `orders-awaiting-shipment --message` | **Cloud Run job** (manual) | The Slack path is non-interactive |
| `orders-awaiting-shipment --display` | Workstation | Calls `input("Press Enter to Continue..")` and `console.clear()` — it is a terminal UI |
| `watch-searches` | **Cloud Run job** | Needs `state_sync` (below); otherwise fine |
| `slack-bot` | **Cloud Run service** | Long-running Socket Mode listener, not a scheduled job |
| `create-listings`, `create-variation-listings`, `relist-listings` | Workstation | Read card scans and Excel workbooks from local paths; `--scrape-prices` drives Chrome |
| `sync-topps-calendar`, `tcdb-search` | Workstation | Drive a real Chrome via `undetected-chromedriver` |
| `ui` (Streamlit) | Workstation | Interactive; reads `scans_dir`, writes the queue file |
| `sync-metadata` | Either | Needs the master workbook, so usually local |

The split is not arbitrary: it is the line between pipelines that talk only to
APIs and pipelines that need a browser or a local file a container has no copy
of.

---

## The three things that had to change

### 1. `watch-searches` kept its state on disk

[`SearchStateStore`](../shoebox/clients/search_state.py) writes
`search_state.json`, `<scope>_seen.jsonl`, `search_hits_append.jsonl` and
`.lock` under `exports/jsonl/searches/`. Two assumptions hold on a long-lived
host and break on a container scheduler:

- **The seen-cache.** A fresh container has none, so every search looks
  unseeded. `cache_was_lost` detects exactly this and re-seeds *silently*
  rather than alerting — which turns the watcher into a no-op that reports
  success. For an alerting system that is the worst available failure mode:
  not noise, but silence.
- **The lock.** `flock`/`msvcrt` coordinate processes sharing a filesystem.
  Two container executions share nothing, so the file lock cannot see the
  other run at all.

[`clients/state_mirror.py`](../shoebox/clients/state_mirror.py) fixes both
without touching `SearchStateStore`. It pulls the state directory from GCS
before a run, pushes it back after, and holds a lock that lives in the same
bucket — an object created with `if_generation_match=0`, which GCS rejects if
it already exists, making creation an atomic test-and-set. The lock carries an
expiry so a crashed run cannot wedge the schedule forever.

Enable it in `app.yaml`:

```yaml
state_sync:
  enabled: true
  bucket: ""              # blank = gcs.ebay_bucket
  prefix: state/searches
  lock_ttl_seconds: 900
```

Left `false` (the default), none of this code runs and a local checkout behaves
exactly as before.

Two deliberate choices worth knowing:

- **State is pushed even when a run raises.** That matches what the watcher
  already does locally, where state is committed per item as it goes: a crash
  mid-run must not roll back the items it already alerted on, or they alert
  again on the next tick.
- **The format stays owned by `SearchStateStore`.** Mirroring a directory is a
  much smaller thing to get right than reimplementing last-line-wins seen
  entries, compaction ratios and seed-count loss detection against blobs — and
  the existing 53 tests keep covering the format unchanged.

### 2. Logging wrote a file nobody would read

`setup_logging()` created `logs/<timestamp>.log` unconditionally. A container's
stdout is already collected by the platform, and its filesystem is discarded
when the execution ends. `SHOEBOX_LOG_DIR=-` (set in the Dockerfile) turns the
file handler off; unset, or any other value, behaves as before.

### 3. The Slack bot needed a port and a narrower command list

Socket Mode dials **out** to Slack and never accepts a request, but a platform
that manages long-running services decides whether a revision started by
probing HTTP. Setting `PORT` makes the bot serve a health endpoint alongside
the listener. Unset locally, nothing listens.

`SHOEBOX_BOT_COMMANDS` narrows which commands a deployment offers. The cloud
bot withholds `create-listings`, `create-variation-listings` and
`create-queue-excel`, because the container has no card scans, no workbooks and
no Chrome. Offering a command that is certain to fail is worse than not listing
it: the failure arrives as a traceback in a Slack thread minutes later and
looks like a bug rather than a deployment boundary.

The bot itself needed no redesign — `run_command` shells out to
`sys.executable -m shoebox.cli`, and the image has the full package, so chat
commands run in the same container.

---

## Installing locally after this change

`undetected-chromedriver` moved from the base dependencies to a `scrapers`
extra. It pins itself to a locally installed Chrome, and it is the one
dependency that builds from a legacy `setup.py` — which fails outright on
newer setuptools, breaking `pip install -e .` on a clean machine.

If you use the Chrome-driven pipelines, install the extra:

```bash
pip install -e ".[scrapers]"     # price scraping, Topps calendar, TCDB
pip install -e ".[dev]"          # tests and lint
```

Everything else is unchanged. The affected commands are `create-listings
--scrape-prices`, `relist-listings --scrape-prices`, `sync-topps-calendar` and
`tcdb-search`; all of them already required a working Chrome install.

---

## Cost

Rough monthly figures at the schedules in `deploy/main.tf`:

| Item | Cost |
|---|---|
| 4 daily jobs, a few minutes each | Pennies; largely inside the free tier |
| `watch-searches` every 5 min (~8,600 executions) | A few dollars at 512 MiB |
| Cloud Scheduler | Free for 3 jobs, then $0.10/job/month |
| Secret Manager | ~$0.06/secret/month |
| Artifact Registry | ~$0.10/GB |
| **Slack bot service** | The only always-on component — one instance with CPU always allocated |

Set `enable_bot = false` to keep the bot on a workstation; the scheduled jobs
alone land in the low single digits per month.

---

## Operational notes

**Timezone.** Cloud Scheduler takes an IANA timezone, so the jobs keep firing
at the same local hour across daylight saving. Note that
`sync_active_listings` builds its traffic-report window from a naive
`datetime.now()` while the rest of the file uses `datetime.now(UTC)`; on a UTC
container that window shifts by your offset. It is a 90-day report, so the
effect is cosmetic — but it is worth knowing before it confuses you.

**The eBay refresh token expires (~18 months).** `ebay_rest` never writes a
refreshed token back to `ebay_rest.json`, which is why the mounted secret can
be read-only — but re-minting needs a browser consent flow, so it happens on a
workstation via `scripts/refresh_ebay_token.py`, then gets uploaded as a new
secret version. Worth a calendar reminder.

**`searches_git_pull` becomes unnecessary.** Secret Manager versioning gives
the same "edit from anywhere" property: update the `shoebox-searches` secret
and the next execution picks it up.

**Jobs do not retry.** The monitoring pipelines append snapshot rows keyed by
`file_date`, so an automatic retry after a partial BigQuery load would
double-count rather than heal.

---

## See also

- [`deploy/README.md`](../deploy/README.md) — setup, secrets, verification
- [scheduling.md](scheduling.md) — the Windows Task Scheduler setup, unchanged
- [search.md](search.md#scheduling) — how the watcher's intervals and lock work
- [data-storage.md](data-storage.md) — what each local file is for
