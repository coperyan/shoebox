# Listing-queue API (`shoebox api`)

The HTTP backend for the iOS app in [`ios/`](../ios/ShoeboxQueue/README.md). It
does for the phone what the Streamlit UI does on the workstation: serves the
Set → Subset → Parallel → Card # dropdowns from the BigQuery metadata views,
enriches queue rows with the same `transforms/queue_enrichment.build_enriched_json`,
and writes the result where `create-listings` can read it. It runs as a Cloud
Run **service** from the same image as the jobs.

```
iPhone app ──X-API-Key──▶ shoebox api (Cloud Run)
                             │ GET /metadata         ◀── cards.v_checklist / v_parallels (BigQuery, cached 1h)
                             │ PUT /queue            ──▶ gs://…/queue/listing_queue_enriched.jsonl  (+ listing_queue_draft.json)
                             │ POST /images          ──▶ gs://<image_bucket>/uploads/<yyyy-mm>/<id>.jpg
                             ▼
workstation: shoebox create-listings   (reads paths.queue_uri; downloads gs:// photos; everything else unchanged)
```

## Authentication

Every route except `/health` requires the header `X-API-Key: <key>`. The key
is the `SHOEBOX_API_KEY` environment variable (in Cloud Run, mounted from the
`shoebox-api-key` secret; see [deployment.md](deployment.md#services)). A
missing or wrong key is a `401`. The server refuses to start without the
variable, and without `paths.queue_uri` in `app.yaml`.

The service itself is reachable by anyone (`allUsers` invoker binding) because
the phone has no Google identity; the API key is the only gate, so treat it
like a password: `openssl rand -hex 32`, rotate by adding a secret version.

## Endpoints

| Method, path | Body | Returns |
|---|---|---|
| `GET /health` | – | `{"status":"ok","version":"0.2.0"}` (no auth). `/healthz` is an alias that only works locally: on a `run.app` URL Google's front end answers `/healthz` itself with a 404 |
| `GET /metadata` | optional `If-None-Match` | the dropdown data (below); `ETag: W/"<version>"`; `304` when unchanged |
| `POST /metadata/refresh` | – | reloads from BigQuery now; `{"version","generated_at","counts"}` |
| `POST /queue/preview` | `{"rows":[QueueRowIn]}` | enrichment per row, nothing written |
| `PUT /queue` | `{"rows":[QueueRowIn]}` | writes the enriched JSONL + draft; `422` and **nothing written** if any row fails |
| `PUT /queue/draft` | `{"rows":[DraftRowIn]}` | saves raw (possibly half-filled) rows only |
| `GET /queue` | – | the last saved rows (queue or draft), the 8 user fields only |
| `POST /images` | multipart `file`, `side`, optional `client_id` | `201 {"uri":"gs://…","side","client_id","bytes","content_type"}` |
| `POST /queue/listings` | `{"publish": true, "schedule": true}` | `202 ListingRun` (state `requested`); starts the Cloud Run `create-listings` job. `409` when the latest save is a draft or a run is active; `502` when the job could not be started |
| `GET /queue/listings` | – | `{"runs": [ListingRun without results…]}`, newest first (20) |
| `GET /queue/listings/{run_id}` | – | the full `ListingRun`; `404` if unknown |

### Row shapes

`QueueRowIn` is exactly what the Streamlit form collects:

```json
{"set_name": "2024 Topps Chrome", "subset_name": "Base", "parallel_variety": "Gold Refractor",
 "card_number": "150", "quantity": 1, "price": 4.99, "image_front": "123", "image_back": "124"}
```

- `parallel_variety` may be `null`. `quantity` is 1..999, `price` ≥ 0.
- `image_front` / `image_back` are a **bare scan number** (`"123"`, stored
  verbatim; the workstation resolves it to `scans/SCAN_0123.jpg` by the
  configured convention), a workstation path, or a **`gs://` URI** returned by
  `POST /images`. Blank is allowed but reported as a warning.
- Unknown keys are rejected (`422`). A request holds 1–500 rows.

`DraftRowIn` is the same with `set_name`, `subset_name` and `card_number`
allowed to be empty, so a draft can hold rows you have not finished.

### Preview / submit result

```json
{"rows": [
   {"index": 0, "ok": true,  "enriched": { "...the 21 ListingQueueRow fields..." }, "error": null,
    "warnings": ["image_back missing"]},
   {"index": 1, "ok": false, "enriched": null, "error": "No checklist match", "warnings": []}
 ],
 "counts": {"rows": 2, "ok": 1, "errors": 1},
 "metadata_version": "3f9a1c0b7e2d4a65"}
```

Errors are the same as the UI's: `No checklist match` (set/subset/card #
not in `v_checklist`) or `set, subset and card # are required`. Warnings
mirror what a careful user would notice in the Streamlit preview: a missing
image, a parallel name that is not in `v_parallels` (the row is listed as
base, exactly as the UI does), and a duplicate of an earlier row.

`PUT /queue` is strict on purpose: `create-listings` refuses a queue with
error rows, so a submission with one bad row returns
`422 {"detail": {"message": "1 rows failed enrichment", "rows": [RowResult…]}}`
and leaves the previous queue untouched. Fix the row (or refresh metadata if
the card was just added) and submit again. A successful `PUT` answers

```json
{"kind": "queue", "submitted_at": "2026-10-15T19:02:11.123456+00:00",
 "counts": {"rows": 2, "ok": 2, "errors": 0},
 "queue_uri": "gs://bucket/queue/listing_queue_enriched.jsonl",
 "draft_uri": "gs://bucket/queue/listing_queue_draft.json",
 "metadata_version": "3f9a1c0b7e2d4a65"}
```

Submitting twice overwrites; there is one queue, as on the workstation.

### Queue vs draft

Two objects live next to each other at `paths.queue_uri`:

- `listing_queue_enriched.jsonl` — what `create-listings` reads. Written only
  by a fully successful `PUT /queue`.
- `listing_queue_draft.json` — the raw rows of the **last save of either
  kind**, with `kind`, `submitted_at`, `counts` and `metadata_version`. `GET
  /queue` returns it; the app's "Load last submitted queue" restores the 8 user
  fields from it, like the Streamlit "Load last saved JSONL". `PUT /queue/draft`
  updates only this object, so saving a half-finished draft never replaces a
  good queue.

### Metadata

```json
{"version": "3f9a1c0b7e2d4a65", "generated_at": "2026-10-15T17:02:11+00:00", "ttl_s": 3600,
 "counts": {"checklist_rows": 41230, "parallel_rows": 1880, "sets": 212},
 "sets": [{"set_name": "2024 Topps Chrome", "set_year": "2024", "subsets": [
    {"subset_name": "Base",
     "cards": [{"card_number": "1", "derived_card_number": "1", "card_id": "…", "subset_id": "…",
                "set_year": "2024", "subset_type": "Base", "player": "…", "team": "…",
                "note": "RC", "note_check": null}],
     "parallels": [{"parallel_variety": "Gold Refractor", "parallel_id": "…", "print_run": 50, "other_note": null}]}
 ]}]}
```

Sets and parallels are sorted; subsets list **`Base` first**, then sorted;
**cards are in the UI's order** (numeric `derived_card_number` first, then
text, blanks last — `build_indexes` in `transforms/queue_indexes.py`). Clients
keep that order. Each set carries a `set_year` (the checklist's `set_year`,
else the four digits the name starts with, else `null`) so the app can group
the Set picker by year. Per-card detail is the first checklist row for the
number, the same row the UI autofills from.

The views are loaded once and cached for an hour (`ttl_s`), like the
Streamlit `st.cache_data`. `version` is a hash of the content; send it back as
`If-None-Match: W/"<version>"` to get a `304` instead of the few MB (gzipped
under 1 MB). After `sync-metadata`, `POST /metadata/refresh` (or the app's
"Refresh metadata") picks the new cards up immediately. If BigQuery is down
on the very first load the route answers `503`; once loaded, a failed reload
keeps serving the previous copy.

### Images

`POST /images` is multipart/form-data: `file` (the bytes), `side` = `front`
or `back`, and optionally `client_id` (`^[A-Za-z0-9_-]{8,64}$`). The bytes are
sniffed: JPEG or PNG only (`415` otherwise), at most 10 MiB (`413`). The
object is `uploads/<YYYY-MM>/<client_id or uuid>.<jpg|png>` in
`gcs.image_bucket`, with `side`/`client_id` as object metadata, and the reply
carries its `gs://` URI to put in `image_front` / `image_back`.

Phone guidance: downscale to a ~2000 px long edge and ~0.85 JPEG quality
before uploading (300–800 KB), and send the local file's UUID as `client_id`
so a retried upload overwrites the same object instead of adding one.

These uploads are a staging area, not the listing images. On the workstation
`create-listings` downloads each `gs://` reference to
`scans_dir/app_uploads/<name>` and hands it to `ImageLogClient.upload_image`
exactly like a scan, so the dedup cache, the `images/<set>/…` naming, the local
mirror and the `images.image_log` table all behave as before (and the download
is deleted afterwards like any scan). Because the download happens before the
combined front+back picture is built, a phone photo feeds that third picture too. Add a lifecycle rule that deletes
`uploads/` objects after 30 days (see [deployment.md](deployment.md#services)).

## Listing from the app

`POST /queue/listings` turns the submitted queue into eBay listings without
the PC: the API writes a **run record** and starts the Cloud Run job
`create-listings` (`shoebox create-listings --run-request`). The job claims the
record, lists the queue with the two choices the app offers, and updates the
record after every row; the app polls it. Prices are **never scraped** on an
app-triggered run (the `--scrape-prices` path needs Chrome and Slack replies).

```json
{"run_id": "20261015T190211Z-3f9a1c0b", "state": "running",
 "requested_at": "2026-10-15T19:02:11+00:00", "started_at": "2026-10-15T19:02:40+00:00", "finished_at": null,
 "publish": true, "schedule": true, "queue_uri": "gs://bucket/queue/listing_queue_enriched.jsonl",
 "metadata_version": "3f9a1c0b7e2d4a65", "rows_total": 12, "rows_done": 3, "listed": 3, "failed": 0,
 "results": [{"sku": "…", "title": "2024 Topps Chrome Base Gold Refractor Player 150",
              "listing_id": "1264…", "offer_id": "8812…", "success": true, "error": null}],
 "error": null, "execution": "projects/…/jobs/create-listings/executions/create-listings-x7k2q", "host": "…"}
```

States: `requested` (record written, job starting) → `running` (job claimed it)
→ `done` or `failed` (`error` says why; the job-failed alert also fires). A
record left `requested`/`running` for over 70 minutes is treated as dead and
no longer blocks a new run. Records live at `<queue dir>/runs/<run_id>.json`.

Rules enforced by the API: the latest save must be a **queue** (`PUT /queue`),
not a draft; only one run at a time (`409` with the active `run_id`); a job
that fails to start leaves a `failed` record and answers `502`.

**Scans in the cloud.** The job has no OneDrive sync folder, so a bare scan
number is fetched from OneDrive through Microsoft Graph
(`clients/onedrive.py`) into the same path the workstation would use
(`scans/SCAN_0234.jpg`), which keeps the image-log cache keys and GCS object
names identical on both hosts. Phone photos come from `uploads/` as before.
Setup (Azure app registration, device-code sign-in, token cache in GCS) is in
[deployment.md](deployment.md#listing-from-the-app).

## The workstation side

Two settings/flags tie the API to `create-listings`:

- `paths.queue_uri: gs://bucket/queue/listing_queue_enriched.jsonl` in
  `configs/app.yaml` makes `create-listings` read the phone's queue instead of
  `exports/jsonl/listing_queue_enriched.jsonl`. Set the **same** URI in the
  cloud `app.yaml` (the API writes there).
- `shoebox create-listings --queue PATH_OR_GS_URI` overrides it for one run,
  e.g. to list a local Excel-built queue while the setting points at GCS.

Everything else in `create-listings` is unchanged; image references of all
three kinds (path, scan number, `gs://`) go through
`utils/image_refs.materialize_image`.

## Errors

| Status | Meaning |
|---|---|
| `401` | missing or wrong `X-API-Key` |
| `413` | image over 10 MiB |
| `415` | upload is not a JPEG or PNG |
| `422` | schema violation (unknown key, blank set, quantity 0, more than 500 rows, bad `client_id`/`side`), or `PUT /queue` with rows that fail enrichment (`detail.rows` lists them) |
| `409` | `POST /queue/listings`: the latest save is a draft, or a run is already active (`detail.run_id`) |
| `502` | `POST /queue/listings`: the Cloud Run job could not be started (IAM, wrong job name); the run record is marked `failed` |
| `503` | BigQuery metadata could not be loaded (first load only) |

## Running it locally

```bash
# needs ADC (gcloud auth application-default login) and an app.yaml with paths.queue_uri
SHOEBOX_API_KEY=dev shoebox api --port 8080

curl -s localhost:8080/health
curl -s -H 'X-API-Key: dev' --compressed localhost:8080/metadata | head -c 400
curl -s -H 'X-API-Key: dev' -H 'content-type: application/json' \
  -X POST localhost:8080/queue/preview \
  -d '{"rows":[{"set_name":"2024 Topps Chrome","subset_name":"Base","card_number":"1","image_front":"123","image_back":"124"}]}'
curl -s -H 'X-API-Key: dev' -F file=@front.jpg -F side=front localhost:8080/images
```

Point the app's Settings at `http://localhost:8080` (the simulator) or your
Mac's LAN address (a device) with the same key. The deployed service's URL is
printed by `gcloud run deploy`; it is HTTPS, so no transport-security
exceptions are needed on the phone.

`create_app()` in `shoebox/api/app.py` takes every collaborator as an
argument (settings, key, metadata loader, GCS client, clock), which is how
`tests/test_api.py` runs the whole surface without BigQuery or GCS.
