# Deploy

Terraform for running the scheduled shoebox pipelines on Cloud Run, plus the
Cloud Build config that produces the image.

This is an **additional** host, not a replacement. A Mac or Windows checkout
runs every command exactly as before; nothing here changes local behaviour.
See [../docs/deployment.md](../docs/deployment.md) for what runs where and why.

```
deploy/
  cloudbuild.yaml   image build + test, pushed to Artifact Registry
  main.tf           jobs, schedules, identities, the Slack bot service
  variables.tf      project, region, image, secrets, schedules
  outputs.tf        service-account emails and in-container config paths
```

## What gets created

| Resource | Purpose |
|---|---|
| `shoebox-runner` service account | Runs the jobs. **Replaces `configs/gcp.json`** — the GCS and BigQuery clients fall back to ADC when that file is missing |
| `shoebox-scheduler` service account | Triggers jobs; deliberately has no access to the secrets |
| 5 scheduled Cloud Run jobs | `sync-active-listings`, `sync-active-listing-details`, `sync-orders`, `end-oos-listings`, `watch-searches` |
| 2 manual Cloud Run jobs | `orders-awaiting-shipment-pull` / `-buyer` — run on demand, as on Windows |
| 1 Cloud Run service (optional) | The Socket Mode Slack bot, `enable_bot = true` |

## One-time setup

Steps are ordered by dependency: secrets must exist before `terraform apply`
(the IAM bindings reference them), and the image must exist before that too
(Cloud Run validates it at deploy time).

**0. Prerequisites and APIs.**

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT

gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com
```

Storage and BigQuery are already enabled if shoebox has been running. You also
need `terraform` >= 1.5 locally.

> **Check the eBay refresh token first.** The mounted `ebay_rest.json` is
> read-only, which is fine because `ebay_rest` never writes a refreshed token
> back — but it means the file you upload must *already* contain a valid
> `refresh_token`. If it is empty, `ebay_rest` tries to open a consent browser,
> which in a container just fails. Confirm before uploading:
>
> ```bash
> python scripts/refresh_ebay_token.py --check
> ```
>
> It should print `refresh_token: present`. If it says `EMPTY (consent will
> run)`, mint one locally first — see [../docs/setup.md](../docs/setup.md).

**1. Create the secrets.** Four of the five gitignored config files become
Secret Manager secrets. `gcp.json` is not one of them — the runner service
account replaces it.

```bash
gcloud secrets create shoebox-app-yaml     --data-file=configs/app.yaml
gcloud secrets create shoebox-ebay-rest    --data-file=configs/ebay_rest.json
gcloud secrets create shoebox-ebay-legacy  --data-file=configs/ebay_legacy.json
gcloud secrets create shoebox-searches     --data-file=configs/searches.yaml
```

**2. Adjust the copy of `app.yaml` you upload.** A secret volume mounts a
*directory*, so each file lands at `/secrets/<key>/<filename>`. Two paths are
passed to the container as environment variables (`SHOEBOX_CONFIG_PATH`,
`EBAY_REST_CONFIG_PATH`); the other two are resolved by `app.yaml` itself and
must be absolute:

```yaml
paths:
  exports_dir: /app/exports              # writable; staging + mirrored state
  query_dir: configs/bigquery/queries    # baked into the image
  searches_file: /secrets/searches/searches.yaml
  searches_git_pull: false               # Secret Manager versioning replaces this

ebay:
  trading_token_path: /secrets/ebay-legacy/ebay_legacy.json

gcp:
  # Intentionally a path that does not exist, so the clients use the
  # job's service account via Application Default Credentials.
  service_account_json: configs/gcp.json

state_sync:
  enabled: true                          # REQUIRED on a stateless host
  bucket: ""                             # blank = gcs.ebay_bucket
  prefix: state/searches
```

`terraform output config_paths` prints the exact in-container path of every
config file if you change the secret keys.

> **`state_sync.enabled: true` is not optional for `watch-searches`.** Without
> it every execution starts with an empty seen-cache, and the watcher re-seeds
> *silently* rather than alerting — it reports success while notifying you of
> nothing. See [`shoebox/clients/state_mirror.py`](../shoebox/clients/state_mirror.py).

**3. Build and push the image.**

```bash
gcloud artifacts repositories create shoebox --repository-format=docker --location=us-central1
gcloud builds submit --config deploy/cloudbuild.yaml
```

**4. Apply.**

```bash
cd deploy
terraform init
terraform apply \
  -var project_id=YOUR_PROJECT \
  -var image=us-central1-docker.pkg.dev/YOUR_PROJECT/shoebox/shoebox@sha256:... \
  -var timezone=America/Los_Angeles
```

Pin the image by **digest**, not `:latest`. A Cloud Build run then produces an
image without silently changing what tonight's scheduled job executes — rolling
forward stays an explicit `terraform apply`.

**5. Bucket and dataset access — usually nothing to do.** `main.tf` grants the
runner `roles/storage.objectAdmin` and `roles/bigquery.dataEditor` at the
project level, which already covers the four buckets and three datasets in
`app.yaml`. `terraform output runner_service_account` gives the email if you
would rather replace those with per-bucket and per-dataset bindings.

## Cutting over from the Windows host

Two components must not run in both places at once. Everything else is safe to
overlap — the daily syncs write snapshot rows keyed by `file_date`, so a
duplicate run is a duplicate snapshot, not corruption.

**`watch-searches` will double-alert.** The Windows host keeps its seen-cache on
disk and the cloud keeps its own in GCS, so neither sees what the other has
already posted and every new listing arrives in Slack twice. Disable the Windows
task before the first cloud run:

```powershell
Disable-ScheduledTask -TaskName watch-searches -TaskPath \shoebox\
```

Then seed the cloud with the state you already have, so the first run picks up
where Windows left off instead of re-seeding from scratch:

```bash
cd /path/to/shoebox
gsutil cp exports/jsonl/searches/search_state.json \
          exports/jsonl/searches/*_seen.jsonl \
          gs://YOUR_EBAY_BUCKET/state/searches/
```

Copy the state and seen files only — not `.lock`, which is host-local.

Skipping the copy is not harmful, just lossy: the first cloud run seeds
silently, so you get no flood, but you also get no alerts for anything listed
during the gap. The same applies if you leave a long pause between disabling
Windows and applying — a search whose `last_run_at` is more than six intervals
old re-seeds silently by design.

**`slack-bot` will answer every command twice.** Both instances are listening on
the same Socket Mode connection:

```powershell
Disable-ScheduledTask -TaskName slack-bot -TaskPath \shoebox\
```

Or set `enable_bot = false` and leave the bot on Windows.

**The rest can overlap** while you build confidence. Disable each Windows task
once its Cloud Run counterpart has run cleanly a few times:

```powershell
Get-ScheduledTask -TaskPath \shoebox\ | Disable-ScheduledTask
```

Nothing is deleted, so re-enabling is one command if you want to fall back.

## Verifying

```bash
gcloud run jobs execute sync-orders --region us-central1 --wait
gcloud run jobs executions list --region us-central1
gcloud logging read 'resource.type=cloud_run_job' --limit 50
```

For the watcher specifically, confirm the state round-trips — the second run
should report pulling files rather than seeding:

```bash
gcloud run jobs execute watch-searches --region us-central1 --wait
gsutil ls gs://YOUR_EBAY_BUCKET/state/searches/
```

## When a step fails

| Symptom | Cause |
|---|---|
| `terraform apply`: `Error 403 ... permission 'iam.serviceAccounts.actAs'` | The identity running Terraform needs `roles/iam.serviceAccountUser` on the runner service account. Project owner has it; a narrower role may not |
| `terraform apply`: secret `not found` | Step 1 was skipped or the secret IDs in `var.secrets` do not match what you created |
| Job execution fails instantly, `Missing config file` | `SHOEBOX_CONFIG_PATH` points somewhere the secret is not mounted. `terraform output config_paths` shows the real paths |
| Job fails with `FileNotFoundError` on a `.sql` file | `paths.query_dir` in the uploaded `app.yaml` is not `configs/bigquery/queries` |
| eBay calls fail with a consent/browser error | The uploaded `ebay_rest.json` has no `refresh_token` — see step 0 |
| eBay 403, `errorId` 1100, `domain: ACCESS` | A missing scope, not a deployment problem. The token carries the scopes it was granted; see [../docs/setup.md](../docs/setup.md) |
| `watch-searches` logs `seeding` on every run | `state_sync.enabled` is not `true` in the uploaded `app.yaml`, so state is not surviving between executions |
| `watch-searches` logs `Another run holds the state lock` forever | A previous execution died without releasing it. It self-heals after `lock_ttl_seconds` (default 15 min); to clear it now, delete `gs://BUCKET/state/searches/.lock.json` |
| Slack alerts arrive twice | The Windows `watch-searches` task is still enabled — see the cutover section |
| Cloud Build: `denied: Permission "artifactregistry.repositories.uploadArtifacts"` | The Cloud Build service account needs `roles/artifactregistry.writer`, or the repository in step 3 was never created |
| Cloud Scheduler complains about a missing App Engine app | Rare on current projects; create one in the same region (`gcloud app create --region=...`) and re-apply |

Logs for any failed run:

```bash
gcloud logging read \
  'resource.type=cloud_run_job AND resource.labels.job_name=sync-orders AND severity>=WARNING' \
  --limit 50 --format='value(textPayload)'
```

## Things worth knowing

**Timezone.** Schedules use `var.timezone`, not UTC, so they keep firing at the
same local hour across daylight saving — which a hand-converted UTC cron does
not. Note that `sync_active_listings` builds its traffic-report window from a
naive `datetime.now()`, so on a UTC container that window shifts by your offset.
It is a 90-day report, so this is cosmetic, but it is why the job's own log
timestamps may not match what you expect.

**The eBay refresh token expires (~18 months).** `ebay_rest` never writes a
refreshed token back to `ebay_rest.json`, which is why the mounted secret can be
read-only. But re-minting requires a browser consent flow, so it must be done on
a workstation with `scripts/refresh_ebay_token.py` and the result uploaded as a
new secret version:

```bash
gcloud secrets versions add shoebox-ebay-rest --data-file=configs/ebay_rest.json
```

Jobs pick up `latest` on their next execution; the bot service needs a new
revision (`terraform apply` after a rebuild, or `gcloud run services update`).

**The bot is the only always-on cost.** `min_instance_count = 1` with
`cpu_idle = false`, because Socket Mode does its work between requests and
Cloud Run would otherwise throttle the CPU and leave the bot unresponsive. Set
`enable_bot = false` to keep it on a workstation instead.

**The bot's command list is narrowed.** `var.bot_commands` withholds
`create-listings`, `create-variation-listings` and `create-queue-excel`: those
read card scans and Excel workbooks from local disk and drive Chrome, none of
which exist in the container. Offering a command that is certain to fail is
worse than not listing it.

**Jobs do not retry** (`max_retries = 0`). The monitoring pipelines append
snapshot rows keyed by `file_date`, so an automatic retry after a partial
BigQuery load would double-count rather than heal.
