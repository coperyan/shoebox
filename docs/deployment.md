# Deployment (Cloud Run)

How to run the daily sync pipelines and the saved-search watcher as **Cloud
Run jobs** on a schedule, so they no longer depend on a workstation being
awake, and which pipelines deliberately stay put.

The container is an **additional host**, not a replacement. A Mac or Windows
checkout runs every command exactly as before, and the two schedulers coexist:
[`scripts/tasks.yaml`](../scripts/tasks.yaml) stays the source of truth for
Windows Task Scheduler ([scheduling.md](scheduling.md)), and
[`deploy/jobs.yaml`](../deploy/jobs.yaml) is its cloud counterpart.

```
Dockerfile                    one image; ENTRYPOINT is the `shoebox` CLI
deploy/jobs.yaml              the jobs: command, cron, resources   <- edit this
scripts/deploy_cloud_run.py   renders jobs.yaml into gcloud commands and runs them
deploy/cloudbuild.yaml        Cloud Build: test -> build -> push -> roll the image out
deploy/searches-cloudbuild.yaml  Cloud Build: validate searches.yaml -> copy it to GCS
```

---

## What runs where

| Workload | Host | Why |
|---|---|---|
| `sync-orders`, `sync-active-listings`, `sync-active-listing-details`, `end-oos-listings` | **Cloud Run job** | API → JSONL → GCS → BigQuery. No browser, no local input, no state between runs |
| `watch-searches` | **Cloud Run job** | Seen-caches, run state and lock live in GCS (`paths.searches_state_uri`); `searches.yaml` is deployed to GCS on every push to the searches repo. See [Saved searches](#saved-searches-watch-searches) |
| `slack-bot` | Workstation (for now) | Long-running Socket Mode listener; would be a Cloud Run *service*, not a job |
| `create-listings`, `create-variation-listings`, `relist-listings`, `send-offers` | Workstation | Read card scans and Excel workbooks from local paths; scrape prices via Chrome; wait on Slack replies |
| `sync-topps-calendar`, `tcdb-search` | Workstation | Drive a real Chrome via `undetected-chromedriver` |
| `ui` (Streamlit), `sync-metadata`, `orders-awaiting-shipment --display` | Workstation | Interactive, or need the master workbook |

The line is simple: pipelines that talk only to APIs move; pipelines that need
a browser, a local file, or a human in the loop stay.

## Why no pipeline code changed

Three seams already existed:

- `SHOEBOX_CONFIG_PATH` relocates `app.yaml`; `EBAY_REST_CONFIG_PATH`
  relocates `ebay_rest.json`; `ebay.trading_token_path` inside `app.yaml`
  relocates `ebay_legacy.json`.
- The GCS and BigQuery clients fall back to **Application Default Credentials**
  when the file named by `gcp.service_account_json` does not exist. In Cloud
  Run, ADC is the job's service account, so `configs/gcp.json` is never uploaded.
- `SHOEBOX_LOG_DIR=-` (set in the Dockerfile) turns off the per-run log file.
  Cloud Run collects stdout and discards the filesystem after each run.

The image's working directory is the repo root, exactly like the Windows tasks,
so `configs/bigquery/...` schemas and SQL resolve unchanged.

`watch-searches` is the exception. It keeps state between runs, so it gained a
GCS state backend and can read `searches.yaml` from a `gs://` URI. Both are
opt-in settings; see [Saved searches](#saved-searches-watch-searches).

---

## `deploy/jobs.yaml` and the deploy script

```bash
python scripts/deploy_cloud_run.py --list                  # what is configured
python scripts/deploy_cloud_run.py --dry-run               # print every gcloud command, run nothing
python scripts/deploy_cloud_run.py                         # jobs + IAM bindings + schedules
python scripts/deploy_cloud_run.py --only sync-orders
python scripts/deploy_cloud_run.py --scheduler-only        # after editing a cron or `enabled:`
python scripts/deploy_cloud_run.py --jobs-only --image REGION-docker.pkg.dev/PROJECT/shoebox/shoebox:TAG
```

Everything is create-or-update, so re-running is safe. For each job the script
runs, in order: `gcloud run jobs deploy` (image, args, env, secret volumes,
resources, **no retries**), `gcloud run jobs add-iam-policy-binding` (lets the
scheduler identity start it), `gcloud scheduler jobs create|update http` (the
cron, calling the job's `:run` endpoint), and `pause`/`resume` only when
`enabled:` disagrees with the schedule's current state. `--dry-run` prints one
shell line per command; it is what Cloud Build pipes to `bash`.

Nothing is ever deleted. Removing or renaming an entry leaves the old Cloud
Run job and Scheduler job in GCP until you `gcloud run jobs delete` /
`gcloud scheduler jobs delete` them.

`defaults:` supplies every setting and each job overrides what it needs:

```yaml
- name: sync-orders                 # Cloud Run + Scheduler job name (lowercase, digits, hyphens)
  run: sync-orders                  # the shoebox subcommand; a string is shell-split, a list is verbatim
  description: Sync orders into GCS/BigQuery.
  schedule: "0 4 * * *"             # 5-field cron in defaults.timezone, or `manual`
  timeout: 1800s                    # any defaults key may be overridden here
  memory: 1Gi
```

| Default | Meaning |
|---|---|
| `region` | Cloud Run and Scheduler region |
| `image` | Image every job runs; `{project}` / `{region}` are filled in. Cloud Build overrides it per rollout |
| `runner_service_account` | The job's identity. Needs GCS object write, BigQuery load/query, and `secretmanager.secretAccessor` on the three secrets |
| `scheduler_service_account` | The identity Cloud Scheduler uses. Only ever gets `roles/run.invoker`, never a secret |
| `timezone` | IANA zone for every cron, so 03:00 stays 03:00 across DST |
| `env` | Container environment (`SHOEBOX_LOG_DIR`, the two `*_CONFIG_PATH` variables) |
| `secrets` | `<file path in container>: <secret>:<version>`. **One secret per directory** — Cloud Run mounts a secret as a directory and refuses two at one path |
| `cpu`, `memory`, `timeout`, `max_retries` | Cloud Run task resources. `max_retries: 0` on purpose — see below |
| `attempt_deadline`, `scheduler_retries` | How long Scheduler waits for the `:run` call (it returns as soon as the execution starts) and how often it retries. Zero, same reason |
| `enabled` | `false` pauses the schedule (a new one is created and paused in the same run); the job stays and can still be run by hand |

**Why no retries.** The syncs `WRITE_APPEND` snapshot rows keyed by
`file_date`. A retry after a partial BigQuery load appends a second snapshot
instead of repairing the first. Re-run a failed day deliberately once you know
why it failed: `gcloud run jobs execute sync-orders --region us-central1 --wait`.

### Current jobs

| Job | Schedule (America/Los_Angeles) | Runs | Windows task |
|---|---|---|---|
| `sync-active-listings` | 03:00 daily | `shoebox sync-active-listings` | `sync-active-listings` |
| `sync-active-listing-details` | 03:30 daily | `shoebox sync-active-listing-details` (1h, 2Gi) | `sync-active-listing-details` |
| `sync-orders` | 04:00 daily | `shoebox sync-orders` | `sync-orders` |
| `end-oos-listings` | 05:00 daily | `shoebox end-oos-listings` | `end_oos_listings` |
| `watch-searches` | every 5 minutes (**paused** until [cutover](#cutover)) | `shoebox watch-searches` (15m timeout) | `watch-searches` |

---

## The cloud copy of `app.yaml`

A Secret Manager secret holds the **whole** `app.yaml`: every section your
workstation file has (`gcp`, `bigquery`, `gcs`, `paths`, `ebay`, `slack`,
`google_calendar`, `store`, ...), because the container validates it with the
same schema and fails on a missing section. Keep a separate copy,
`configs/app.cloud.yaml` (gitignored), made by copying your real file and
changing **only** the lines shown below. Do not upload just this excerpt.

```bash
cp configs/app.yaml configs/app.cloud.yaml
# edit the lines below, then check it still loads:
SHOEBOX_CONFIG_PATH=configs/app.cloud.yaml python -c "from shoebox.settings import get_settings as g; print(g().paths.scans_dir)"
```

The two `*_CONFIG_PATH` environment variables handle `app.yaml` and
`ebay_rest.json`; everything else below is resolved by `app.yaml` itself.

```yaml
gcp:
  service_account_json: configs/gcp.json   # deliberately absent in the image -> ADC -> the runner SA

paths:
  data_dir: data                           # all relative to /app and writable by the container user
  query_dir: configs/bigquery/queries      # baked into the image; must stay exactly this
  exports_dir: exports
  scans_dir: scans                         # NOT your OneDrive path (see below)
  set_images_dir: set_images               # same
  tools_dir: tools
  searches_file: gs://BUCKET/config/searches.yaml   # watch-searches; see "Saved searches"
  searches_state_uri: gs://BUCKET/state              # watch-searches refuses to run in Cloud Run without it
  searches_git_pull: false                           # the image has no git; the file is pushed, not pulled

ebay:
  path: /secrets/ebay-rest                 # redundant with EBAY_REST_CONFIG_PATH, harmless
  trading_token_path: /secrets/ebay-legacy/ebay_legacy.json
```

> **Every `paths.*_dir` must be relative.** The CLI calls
> `ensure_runtime_dirs()` before any command and `mkdir -p`s each of them. A
> workstation value such as `/users/you/OneDrive/Pictures/...` makes every job
> die at startup with `PermissionError` before doing any work.

---

## One-time setup

Steps are ordered by dependency. `PROJECT` is your GCP project ID; the region
is `us-central1` unless you change `deploy/jobs.yaml`.

**0. Tools and auth.** The Google Cloud SDK is not part of the repo's
requirements:

```bash
brew install --cask google-cloud-sdk      # macOS; see https://cloud.google.com/sdk/docs/install
gcloud auth login
gcloud config set project PROJECT
```

**1. Check the eBay refresh token.** The mounted `ebay_rest.json` is read-only,
which is fine because `ebay_rest` never writes a refreshed token back — but the
file you upload must *already* contain a valid `refresh_token`. If it is
empty, `ebay_rest` tries to open a consent browser, which in a container just
fails.

```bash
python scripts/refresh_ebay_token.py --check      # must say: refresh_token: present
```

**2. Enable the APIs.**

```bash
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com
```

**3. Write `configs/app.cloud.yaml`** as described above: a full copy of
`app.yaml` with the path lines changed, not just the excerpt.

**4. Create the secrets.** Three of the gitignored config files become
secrets. `gcp.json` is not one of them.

```bash
gcloud secrets create shoebox-app-yaml    --replication-policy=automatic --data-file=configs/app.cloud.yaml
gcloud secrets create shoebox-ebay-rest   --replication-policy=automatic --data-file=configs/ebay_rest.json
gcloud secrets create shoebox-ebay-legacy --replication-policy=automatic --data-file=configs/ebay_legacy.json
```

To change one later, add a version; jobs read `latest` on their next run:

```bash
gcloud secrets versions add shoebox-app-yaml --data-file=configs/app.cloud.yaml
```

**5. Runner service account.** The simplest choice is to **reuse the account
you already have** — the `client_email` in `configs/gcp.json` — since it
already owns the buckets and datasets. Put its email in
`deploy/jobs.yaml` → `runner_service_account`, then let it read the secrets:

```bash
RUNNER=your-existing-sa@PROJECT.iam.gserviceaccount.com
for s in shoebox-app-yaml shoebox-ebay-rest shoebox-ebay-legacy; do
  gcloud secrets add-iam-policy-binding $s --member serviceAccount:$RUNNER --role roles/secretmanager.secretAccessor
done
```

If you would rather start clean: `gcloud iam service-accounts create
shoebox-runner`, then grant it `roles/bigquery.jobUser`,
`roles/bigquery.dataEditor` and `roles/storage.objectAdmin` (project-wide, or
per dataset and bucket) plus the three bindings above. Once the jobs run under
this identity you can delete the downloaded key from your workstation.

**6. Scheduler service account.** Gets no project roles at all; the deploy
script grants it `roles/run.invoker` on each job.

```bash
gcloud iam service-accounts create shoebox-scheduler --display-name "shoebox scheduler"
```

**7. Artifact Registry.**

```bash
gcloud artifacts repositories create shoebox --repository-format=docker --location=us-central1
```

**8. Build identity.** Projects that enabled Cloud Build after mid-2024 run
builds as the Compute Engine default service account. Either grant that
account the roles below or, better, create `shoebox-builder` and select it on
the trigger in step 12. Two of the roles belong on a specific resource, not
the project, so grant them from the terminal rather than the IAM page:

| Role | Where | Why |
|---|---|---|
| `roles/run.developer` | project | `gcloud run jobs deploy` |
| `roles/logging.logWriter` | project | build logs (required for any user-specified build SA) |
| `roles/artifactregistry.writer` | the `shoebox` repository | push the image |
| `roles/iam.serviceAccountUser` | **on the runner SA only** | deploy a job that runs as it (`actAs`). Project-wide would let the builder act as any account |

```bash
BUILDER=shoebox-builder@PROJECT.iam.gserviceaccount.com
RUNNER=your-runner-sa@PROJECT.iam.gserviceaccount.com

gcloud iam service-accounts create shoebox-builder --display-name "shoebox Cloud Build"
gcloud projects add-iam-policy-binding PROJECT --member serviceAccount:$BUILDER --role roles/run.developer
gcloud projects add-iam-policy-binding PROJECT --member serviceAccount:$BUILDER --role roles/logging.logWriter
gcloud artifacts repositories add-iam-policy-binding shoebox --location us-central1 \
  --member serviceAccount:$BUILDER --role roles/artifactregistry.writer
gcloud iam service-accounts add-iam-policy-binding $RUNNER \
  --member serviceAccount:$BUILDER --role roles/iam.serviceAccountUser
```

The builder needs nothing else: no `run.admin`, no secret access, nothing on
Cloud Scheduler. It only ever runs `--jobs-only`; IAM bindings and schedules
are applied from your laptop in step 10.

A build only runs as the builder when it names it: the trigger in step 12
does, and `gcloud builds submit` does when you pass `--service-account`.
Submitting by hand also uploads the source to a bucket named
`PROJECT_cloudbuild`, which the builder must be able to read (without this the
submit fails with `could not resolve source ... storage.objects.get`):

```bash
gcloud storage buckets add-iam-policy-binding gs://PROJECT_cloudbuild \
  --member serviceAccount:$BUILDER --role roles/storage.objectViewer
```

Your own account needs `roles/iam.serviceAccountUser` on both the runner SA
(step 10 deploys jobs as it) and the builder (to submit builds and create the
trigger as it). Project Owner has both; a narrower role may not.

**9. First image.** Skip the rollout, since the jobs do not exist yet — Cloud
Run validates the image at deploy time, so it must be pushed first.

```bash
gcloud builds submit --config deploy/cloudbuild.yaml \
  --service-account projects/PROJECT/serviceAccounts/shoebox-builder@PROJECT.iam.gserviceaccount.com \
  --substitutions=_TAG=$(git rev-parse --short HEAD),_DEPLOY_JOBS=false
```

Passing `--service-account` makes the first build exercise the same identity
the trigger will use, so a missing role shows up now rather than on the first
push to `main`. This also runs the test suite inside the image's own
interpreter.

**10. First deploy, from your laptop.** Do this once with your own credentials
so that any IAM or API problem surfaces where you can fix it. Cloud Build only
ever needs `--jobs-only` afterwards.

```bash
python scripts/deploy_cloud_run.py --dry-run     # read it
python scripts/deploy_cloud_run.py               # jobs, invoker bindings, schedules
```

To keep the schedules paused while you verify by hand, set `enabled: false`
under `defaults:` first, then flip it and run `--scheduler-only`.

**11. Verify.**

```bash
gcloud run jobs execute sync-orders --region us-central1 --wait
gcloud run jobs executions list --region us-central1
gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=sync-orders' \
  --limit 50 --format='value(textPayload)'
bq query --use_legacy_sql=false 'SELECT MAX(file_date) FROM `PROJECT.ebay.orders`'
```

The Slack `notify_channel` should show "Starting sync_orders.." and
"Completed sync_orders..". A manual run appends one snapshot for today, the
same as running the command by hand on Windows.

**12. Connect GitHub and create the trigger.** Connecting the repository is a
browser step: Cloud Build → Triggers → *Connect repository* → GitHub →
authenticate → *Install Google Cloud Build* on the GitHub account, granting it
**only** this repository → select it → *Connect*. The trigger itself can then
be created from the terminal:

```bash
gcloud builds triggers create github --name=shoebox-main \
  --repo-owner=coperyan --repo-name=shoebox --branch-pattern='^main$' \
  --build-config=deploy/cloudbuild.yaml \
  --substitutions='_TAG=$SHORT_SHA' \
  --service-account=projects/PROJECT/serviceAccounts/shoebox-builder@PROJECT.iam.gserviceaccount.com
```

From then on every push to `main` tests, builds, pushes `:<sha>` and
`:latest`, and updates all four jobs to the new image. The rendered `gcloud`
commands appear in the build log. Commit the deployment files before the first
push: the trigger builds whatever is on `main`.

**13. Cut over.** Each Windows run *and* each cloud run appends a snapshot, so
turn the Windows tasks off the same day the cloud schedules go live. In
`scripts/tasks.yaml` set `enabled: false` on the four daily tasks and
re-register (`python scripts/generate_tasks.py --register`), or:

```powershell
Get-ScheduledTask -TaskPath \shoebox\ |
  Where-Object Name -in sync-active-listings,sync-active-listing-details,sync-orders,end_oos_listings |
  Disable-ScheduledTask
```

Leave `slack-bot` alone; it stays on Windows. `watch-searches` has its own
[cutover](#cutover), because its state has to move with it. Nothing is
deleted, so falling back is one `Enable-ScheduledTask`.

---

## Saved searches (`watch-searches`)

The watcher is harder to move than the daily syncs for two reasons. It keeps
state between runs (the seen-caches that stop a listing alerting twice), and its
config changes often, from a separate **private** repo, because this one is
public. Each gets its own piece:

```
private searches repo ──push to main──▶ Cloud Build: validate ──▶ copy
                                          (searches-cloudbuild.yaml)   │
                                                                       ▼
Cloud Scheduler */5 ──▶ Cloud Run job watch-searches ◀── reads ── gs://BUCKET/config/searches.yaml
                              │  lock → download → run → upload
                              ▼
                        gs://BUCKET/state/   search_state.json, <channel>_seen.jsonl,
                                             search_hits_append.jsonl, .lock
```

**The config is pushed, not pulled.** When a push to `main` of the searches
repo touches `searches.yaml`, it runs
[`deploy/searches-cloudbuild.yaml`](../deploy/searches-cloudbuild.yaml). That
build validates the file with the watcher's own loader
(`scripts/validate_searches.py`) and then copies it to GCS. Compared with a
`git pull` on every tick:

- A broken edit fails that commit's build, visibly, and the job keeps reading
  the last good copy.
- The job needs no GitHub credentials and does not depend on GitHub being up
  288 times a day.
- Bucket versioning keeps the history, so rolling back is one `cp`.

`paths.searches_file` in the cloud `app.yaml` is the `gs://` object, and the
loader re-reads it every run. The workstation keeps its clone (with
`searches_git_pull` if you use it) for `preview-search` and `search-aspects`,
which are read-only and stay local.

**The state lives in GCS.** With `paths.searches_state_uri` set, each run:

1. Takes a lock object in GCS, created with `ifGenerationMatch=0`, so only one
   run can win.
2. Downloads the state files to a scratch directory.
3. Runs the unchanged watcher against those files.
4. Uploads whatever changed after **each search** and again at the end.

What a crash costs changes by one step. On disk, state is committed per item,
so a crash costs one duplicate. Here it is committed per search, so a run
killed mid-search re-posts that search's batch (at most `max_notify`) on the
next tick. That is still a duplicate, never a miss. Cloud Run's timeout
SIGTERM is caught, so the final upload and the lock release still happen. A
lock left behind by a run that was SIGKILLed goes stale after 20 minutes and
is broken. That window must stay above the job's 900s timeout.

**Every host that names the same URI shares one state and one lock.** Set
`searches_state_uri` in the workstation's `app.yaml` as well. Then a
`watch-searches` run from the terminal, or from the Slack bot
(`/watch-searches --reseed NAME`), works on the cloud's state under the cloud's
lock. It cannot double-alert or diverge. Without the setting, a local run
reads the pre-cutover files and re-alerts everything the cloud has posted
since.

**A guard.** In Cloud Run (`CLOUD_RUN_JOB` is set), `watch-searches` refuses to
run without `searches_state_uri`. Otherwise every execution would start empty
and reseed, and every `notify_on_seed` search would repost its matches every
five minutes.

### Saved-search setup

This assumes the [one-time setup](#one-time-setup) above is done. Names used
below:

```bash
PROJECT=your-project
BUCKET=$PROJECT-shoebox-searches
RUNNER=runner-sa@$PROJECT.iam.gserviceaccount.com       # runner_service_account in jobs.yaml
BUILDER=shoebox-builder@$PROJECT.iam.gserviceaccount.com
SEARCHES_REPO=your-private-searches-repo
```

**S1. A dedicated bucket**: private, versioned, with old versions expiring
after a week. The state is rewritten every tick, so versioning gives you a
week of restore points for both state and config.

```bash
gcloud storage buckets create gs://$BUCKET --location=us-central1 \
  --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update gs://$BUCKET --versioning
echo '{"rule": [{"action": {"type": "Delete"}, "condition": {"daysSinceNoncurrentTime": 7}}]}' > lifecycle.json
gcloud storage buckets update gs://$BUCKET --lifecycle-file=lifecycle.json && rm lifecycle.json
```

A separate bucket rather than a prefix in `gcs.ebay_bucket` keeps your buy
criteria away from anything that is ever shared. It also means the builder's
write access (S2) reaches nothing else.

**S2. Access.** `roles/storage.objectUser` covers everything the lock and sync
do: read, list, create, overwrite and delete. The runner needs it for the job.
The builder needs it to deploy the config. If the workstation's
`configs/gcp.json` is a different account from the runner, grant it too: it
runs the migration script and any shared-state local run.

```bash
for SA in $RUNNER $BUILDER; do
  gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
    --member serviceAccount:$SA --role roles/storage.objectUser
done
```

**S3. Upload the config once by hand**, so the job has something to read
before the trigger exists:

```bash
python scripts/validate_searches.py configs/searches.yaml
gcloud storage cp configs/searches.yaml gs://$BUCKET/config/searches.yaml
```

**S4. Connect the searches repo and create the trigger.** On GitHub, open
*Settings → Applications → Google Cloud Build → Configure* and add
`$SEARCHES_REPO` to the selected repositories. Then in Cloud Build →
Triggers → *Connect repository*, select it. From **this** checkout, since
`--inline-config` reads the local file and embeds it in the trigger:

```bash
gcloud builds triggers create github --name=shoebox-searches \
  --repo-owner=coperyan --repo-name=$SEARCHES_REPO --branch-pattern='^main$' \
  --included-files=searches.yaml \
  --inline-config=deploy/searches-cloudbuild.yaml \
  --substitutions=_DEST_URI=gs://$BUCKET/config/searches.yaml,_SEARCHES_PATH=searches.yaml \
  --service-account=projects/$PROJECT/serviceAccounts/$BUILDER

gcloud builds triggers run shoebox-searches --branch=main    # try it
```

If the file is not at the repo root, set `_SEARCHES_PATH` and
`--included-files` to its path. The validate step downloads shoebox at
`_SHOEBOX_REF` (default `main`), so `scripts/validate_searches.py` must be on
`main` before the trigger's first run. To change `searches-cloudbuild.yaml`
later, `gcloud builds triggers delete shoebox-searches` and create it again.

**S5. The cloud `app.yaml`.** In `configs/app.cloud.yaml`, set the three
`searches` lines shown in [The cloud copy of `app.yaml`](#the-cloud-copy-of-appyaml),
check that `slack.search_channel` matches the workstation's, and upload:

```bash
gcloud secrets versions add shoebox-app-yaml --data-file=configs/app.cloud.yaml
```

**S6. Deploy the job** (paused) and rehearse it in the cloud. A dry run reads
the config from GCS, takes and releases the lock, calls eBay, and writes
nothing:

```bash
python scripts/deploy_cloud_run.py --only watch-searches
gcloud run jobs execute watch-searches --region us-central1 --wait --args=watch-searches,--list
gcloud run jobs execute watch-searches --region us-central1 --wait --args=watch-searches,--dry-run,--force
```

### Cutover

In this order. The Windows task must not tick between steps 1 and 3.

1. **Stop the workstation.** Set `enabled: false` on `watch-searches` in
   `scripts/tasks.yaml` and re-register, or
   `Disable-ScheduledTask -TaskPath \shoebox\ -TaskName watch-searches`.
2. **Move the state.** Report first, then apply. The script takes both the
   local and the GCS lock while it copies, and refuses to overwrite existing
   state without `--force`:

   ```bash
   python scripts/migrate_search_state_to_gcs.py --uri gs://$BUCKET/state
   python scripts/migrate_search_state_to_gcs.py --uri gs://$BUCKET/state --apply
   ```

3. **Share it.** Add `searches_state_uri: gs://BUCKET/state` under `paths:` in
   the workstation's `configs/app.yaml`.
4. **Start the cloud.** Set `enabled: true` on `watch-searches` in
   `deploy/jobs.yaml`, commit, and apply it:

   ```bash
   python scripts/deploy_cloud_run.py --scheduler-only --only watch-searches
   ```

5. **Verify** over the next ten minutes:

   ```bash
   gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=watch-searches' \
     --limit 50 --format='value(textPayload)'
   gcloud storage cat gs://$BUCKET/state/search_state.json | head -20   # last_run_at advancing
   ```

   There should be no seed messages in Slack. A `🌱 seeded` line means that
   search's state did not come across.

**Falling back** needs no copy back. Pause the schedule
(`gcloud scheduler jobs pause watch-searches --location us-central1`) and
re-enable the Windows task. With step 3 done, it carries on from the GCS state.

### Day to day

| Task | How |
|---|---|
| Add or change a search | Push to `main` of the searches repo. Live on the next tick after the build (about a minute). A red check on the commit means it was rejected and the old file is still live |
| `--reseed`, `--force`, `--only` | From the Slack bot as before (shared state, shared lock), or `gcloud run jobs execute watch-searches --region us-central1 --args=watch-searches,--reseed,NAME` |
| Tune a search | `preview-search` / `search-aspects` on the workstation, unchanged |
| Look at state | `gcloud storage cat gs://$BUCKET/state/search_state.json` |
| Undo a bad config | `gcloud storage ls -a gs://$BUCKET/config/searches.yaml`, then `gcloud storage cp 'gs://$BUCKET/config/searches.yaml#GENERATION' gs://$BUCKET/config/searches.yaml`, and revert the commit so the next push doesn't redeploy it |
| Who holds the lock | `gcloud storage cat gs://$BUCKET/state/.lock` (host, pid, Cloud Run execution, time). It breaks itself after 20 minutes. Delete it by hand only if you are sure nothing is running |

---

## Running the image locally

Useful for reproducing a cloud failure with the same code and interpreter.
Bind-mount the config files where the jobs expect them:

```bash
docker build -t shoebox .
docker run --rm shoebox --help
docker run --rm \
  -e SHOEBOX_CONFIG_PATH=/secrets/app/app.yaml \
  -e EBAY_REST_CONFIG_PATH=/secrets/ebay-rest/ebay_rest.json \
  -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcp.json \
  -v "$PWD/configs/app.cloud.yaml:/secrets/app/app.yaml:ro" \
  -v "$PWD/configs/ebay_rest.json:/secrets/ebay-rest/ebay_rest.json:ro" \
  -v "$PWD/configs/ebay_legacy.json:/secrets/ebay-legacy/ebay_legacy.json:ro" \
  -v "$PWD/configs/gcp.json:/secrets/gcp.json:ro" \
  shoebox sync-orders
```

This is a real run against eBay, GCS and BigQuery.

---

## Alerting

The pipelines post "Starting" and "Completed" to Slack, so a crash shows up
only as a message that never arrives. Nobody notices a silence at 04:00. Worse,
the jobs run with `--max-retries 0` by design, so a failed run is final rather
than something that quietly recovers.

[`deploy/alerts/job-failed.yaml`](../deploy/alerts/job-failed.yaml) closes that
gap: a Cloud Monitoring policy on `run.googleapis.com/job/completed_execution_count`
filtered to `result = "failed"`, grouped per job so the alert names the pipeline
that broke. It fires on the first failure rather than waiting for a pattern,
and auto-closes after 30 minutes so the next failure opens a fresh incident
instead of being suppressed by an old one. The notification body carries the
log-reading and re-run commands for that specific job.

This covers strictly more than a `try/except` in the CLI would: a container
that dies before Python starts, from a bad secret mount, an image that will not
pull, or an out-of-memory kill, never gets the chance to post to Slack itself.

### Sending it to Slack

Cloud Monitoring's Slack channel type needs a token with `chat:write`. There
are two ways to supply one, and this project already has what the second needs.

**Reuse the existing bot (no new Slack app).** `slack.bot_token` already has
`chat:write` and the bot is already in `slack.notify_channel`, so alerts arrive
from the same bot and in the same channel as the pipelines' own messages:

```bash
python scripts/create_alert_channel.py --dry-run          # show what it will do
python scripts/create_alert_channel.py --attach-to-policies
```

The script reads the token from the app config and sends it only to the
Monitoring API in your own project, which stores it obfuscated. It never
prints it. Re-running reuses an existing channel rather than duplicating it.

**Or use the console OAuth flow.** Monitoring → Alerting → *Edit notification
channels* → Slack → *Add new*. This installs Google's own "Google Cloud
Monitoring" Slack app, which then has to be invited to the channel
(`/invite @Google Cloud Monitoring`). Use this if you would rather not have the
bot token in a second place.

Either way, attach the channel to the policy:

```bash
gcloud monitoring policies list --format='value(name,displayName)'
gcloud monitoring policies update POLICY_ID --set-notification-channels=CHANNEL_NAME
```

### Applying the policy

```bash
gcloud monitoring policies create --policy-from-file=deploy/alerts/job-failed.yaml \
  --notification-channels=projects/PROJECT/notificationChannels/CHANNEL_ID
```

The channel is passed on the command line rather than stored in the YAML: its
id is project-specific and the file is committed to a public repo.

### Testing it

Deliberately fail a throwaway job rather than breaking a real one. `shoebox`
rejects an unknown subcommand with a non-zero exit before it loads any config
or calls any API, so nothing is touched:

```bash
gcloud run jobs deploy alert-test --region us-central1 \
  --image us-central1-docker.pkg.dev/PROJECT/shoebox/shoebox:latest \
  --args not-a-real-command --max-retries 0 --task-timeout 120s
gcloud run jobs execute alert-test --region us-central1 --wait   # exits non-zero
```

The alert lands in Slack within a few minutes; metric ingestion is not instant.
Then remove it:

```bash
gcloud run jobs delete alert-test --region us-central1 --quiet
```

---

## When a step fails

| Symptom | Cause |
|---|---|
| `error: \`gcloud\` is not on PATH` | Step 0 |
| `Permission ... iam.serviceAccounts.actAs denied` on deploy | The deploying identity needs `roles/iam.serviceAccountUser` on the runner SA (step 8) |
| Deploy fails with `Permission denied on secret` | The runner SA lacks `secretmanager.secretAccessor` on that secret (step 5) |
| Job fails instantly with a pydantic `validation error` listing missing fields | The uploaded `app.yaml` is incomplete (only the excerpt was uploaded). Upload a full copy as a new secret version |
| Job fails instantly: `Missing config file` | `SHOEBOX_CONFIG_PATH` points where no secret is mounted; compare `env` and `secrets` in `jobs.yaml` |
| Job fails instantly: `PermissionError: ... /users/...` | A `paths.*_dir` in the uploaded `app.yaml` is a workstation path. Make them relative and add a secret version |
| `FileNotFoundError` on a `.sql` file | `paths.query_dir` in the uploaded `app.yaml` is not `configs/bigquery/queries` |
| eBay calls fail with a consent/browser error | The uploaded `ebay_rest.json` has no `refresh_token` — step 1 |
| eBay 403, `errorId` 1100, `domain: ACCESS` | A missing scope, not a deployment problem; see [setup.md](setup.md) |
| `Missing Trading API token file` | `ebay.trading_token_path` in the uploaded `app.yaml` is not `/secrets/ebay-legacy/ebay_legacy.json` |
| `gcloud builds submit`: `could not resolve source ... storage.objects.get` | The builder cannot read the `PROJECT_cloudbuild` source bucket (step 8) |
| Cloud Build `test` step: `FileNotFoundError: ... 'git'` | The test step installs git before pytest; the saved-search tests need it. Check `deploy/cloudbuild.yaml` was not trimmed |
| Cloud Build: `denied: Permission "artifactregistry.repositories.uploadArtifacts"` | The build SA needs `roles/artifactregistry.writer`, or the repository (step 7) was never created |
| Cloud Build `rollout` step: `PERMISSION_DENIED` on `run.jobs.update` | The build SA needs `roles/run.developer` and `actAs` on the runner SA |
| Scheduler tick fails with a token or permission error | The scheduler SA lacks `roles/run.invoker` on that job; re-run the script with `--scheduler-only` |
| Cloud Scheduler complains about a missing App Engine app | Rare on current projects; `gcloud app create --region=us-central` once and re-run |
| Rows appear twice in BigQuery for one `file_date` day | Both hosts ran; step 13 |
| `watch-searches is running in Cloud Run with local state` | The uploaded `app.yaml` has no `paths.searches_state_uri` (S5) |
| `watch-searches`: `Missing object: gs://.../searches.yaml` | Nothing deployed yet (S3), or `paths.searches_file` and the trigger's `_DEST_URI` disagree |
| `watch-searches`: 403 on `storage.objects.create` or `.get` | The runner lacks `roles/storage.objectUser` on the searches bucket (S2) |
| Every tick logs "Another watch-searches run holds the lock" | A run is still going, or one died holding it. It expires after 20 minutes; `gcloud storage cat gs://BUCKET/state/.lock` shows the holder |
| Searches build fails at `validate` with `INVALID` | The edit is broken; the message has the YAML location. The job is still reading the previous copy |
| Searches build: `HTTP Error 404` or `No such file ... validate_searches.py` | `_SHOEBOX_REF` does not exist, or `main` predates the validator |
| Pushing to the searches repo starts no build | The repo is not granted to the Cloud Build GitHub App or not connected (S4), or the commit did not touch the `--included-files` path |
| Listings alert twice after the cutover | The Windows task ran with its old local state: disable it (cutover step 1) or give it the shared URI (step 3) |
| A `🌱 seeded` line for an existing search after the cutover | Its state did not migrate; rerun the migration report and compare |

Logs for any failed run:

```bash
gcloud logging read \
  'resource.type=cloud_run_job AND resource.labels.job_name=sync-orders AND severity>=WARNING' \
  --limit 50 --format='value(textPayload)'
```

---

## Things worth knowing

**The eBay refresh token expires (~18 months).** Renewal is a browser consent
flow, so it happens on a workstation via `scripts/refresh_ebay_token.py`, after
which you upload a new secret version:

```bash
gcloud secrets versions add shoebox-ebay-rest --data-file=configs/ebay_rest.json
```

Nothing warns you before it expires except the jobs failing. A calendar
reminder is cheap.

**Timezone.** Schedules use `defaults.timezone`, so they keep firing at the
same local hour across DST. The container's clock is UTC. `sync-active-listings`
builds its 90-day traffic window from a naive `datetime.now()`; at 03:00 Pacific
the UTC calendar date is the same, so nothing changes. To make the container
behave exactly like the workstation anyway, uncomment `TZ` under `env` in
`jobs.yaml`.

**Failure alerts** are covered by the Alerting section below.

**Cost.** Four short daily jobs sit inside the free tier or near it.
`watch-searches` is about 288 executions a day. Most are a few seconds of
"nothing due", so expect roughly $1–3/month beyond the free tier. Cloud
Scheduler is free for the first three jobs and $0.10/month for each after,
so $0.20 for five. The searches bucket, Secret Manager and Artifact Registry
are pennies. The eBay Browse call budget is unchanged: one call per due
search, the same as on Windows ([search.md](search.md#scheduling)).

**Job overlap.** Cloud Run has no equivalent of Task Scheduler's
`IgnoreNew`; a second `execute` while one is running starts a second
execution. Daily schedules with sub-hour timeouts cannot self-overlap, and the
four jobs are independent of each other. `watch-searches` can outlast its
five-minute tick. The next execution then finds the GCS lock held, logs it,
and exits 0.

**Moving more pipelines later.** `slack-bot` needs to become a Cloud Run
service with one always-on instance; the image already contains the code for
it. Its `watch-searches` commands already work against the cloud state once the
workstation's `app.yaml` names the shared `searches_state_uri`.

---

## See also

- [scheduling.md](scheduling.md) — the Windows Task Scheduler setup, unchanged
- [configuration.md](configuration.md) — `SHOEBOX_LOG_DIR` and the other environment variables
- [data-storage.md](data-storage.md) — what each pipeline writes to GCS and BigQuery
