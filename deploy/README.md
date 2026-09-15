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

**5. Grant bucket and dataset access.** `terraform output runner_service_account`
gives the email; it needs object write on the four buckets in `app.yaml` and
BigQuery append on the datasets. The project-level roles in `main.tf` cover the
common case; tighten to per-bucket bindings if you prefer.

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
