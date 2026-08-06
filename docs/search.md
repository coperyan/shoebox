# Saved eBay searches

Everything about the search feature: what it does, how to set it up from
nothing, how to write and tune a search, how to schedule it, and what it leaves
behind on disk and in BigQuery.

Related reference: [cli.md](cli.md#saved-searches) (flag tables),
[configuration.md](configuration.md#saved-searches-configssearchesyaml) (YAML
schema), [pipelines.md](pipelines.md#saved-search-watcher) (internals),
[data-storage.md](data-storage.md) (where the bytes live).

---

## What it is

A **saved search** is a YAML-declared eBay Browse query. A watcher runs the due
ones, diffs the results against what it has already seen, and posts only the
**net-new** listings to Slack — one parent message per search, each listing as a
thread reply with its photo.

Three commands, one cron entry:

| Command | Purpose | Side effects |
|---|---|---|
| `watch-searches` | Run due searches, alert Slack, record state | Slack, local state, GCS + BigQuery |
| `preview-search` | Show every listing a search returns and which filter rejected each | **None** — read-only |
| `search-aspects` | List eBay's structured attributes (aspects) you could filter on | **None** — read-only |

The design goal is a channel that is silent unless something genuinely new
appeared. Consequences of that goal show up throughout: no "0 new" posts, a
silent first run, an explicit overflow line instead of dropped items, and a
duplicate-over-miss bias when something fails mid-run.

---

## Quick start

Assuming shoebox is already installed and `configs/app.yaml`,
`configs/ebay_rest.json` and `configs/gcp.json` exist (see [setup.md](setup.md)):

```bash
cp configs/searches.example.yml configs/searches.yaml
```

1. Set `slack.search_channel` in `configs/app.yaml` (a channel **ID**, and
   `/invite` the bot into it).
2. Edit `configs/searches.yaml` — delete the sample searches, write one of your
   own.
3. Validate, without touching eBay or Slack:

```bash
shoebox watch-searches --list
```

4. See what it would actually match:

```bash
shoebox preview-search my_search
```

5. Go live, then add the cron entry from [Scheduling](#scheduling):

```bash
shoebox watch-searches
```

The first real run **seeds silently** — it records every current match and posts
one confirmation line. Only listings that appear *after* that alert.

---

## Setup

### Prerequisites

| Need | Why | Where |
|---|---|---|
| eBay REST credentials | Browse API calls | `configs/ebay_rest.json` — see [setup.md](setup.md#ebay-setup) |
| A Slack bot token + a channel | The alerts | `slack.bot_token`, `slack.search_channel` in `app.yaml` |
| GCP service account (optional-ish) | Durable `search_hits` log in BigQuery | `configs/gcp.json`, `gcs.ebay_bucket`, `bigquery.ebay_dataset` |

Browse runs on the eBay **application** token, so no extra `sell.*` scope is
needed beyond the base `api_scope` already in the `ebay_rest.json` template. The
Trading API token (`configs/ebay_legacy.json`) is **not** used by search.

GCP is only needed for the durable log. A failed GCS/BigQuery flush is logged as
a warning and retried next run — dedup and alerting never depend on it — but if
you have no GCP at all, run with `--no-flush` (or accept one warning per run).
The `ebay.search_hits` table is created automatically on the first successful
flush from `configs/bigquery/schemas/search_hits.json`.

### `configs/app.yaml` keys the watcher reads

| Key | Meaning |
|---|---|
| `paths.searches_file` | Path to the searches YAML (default `configs/searches.yaml`) |
| `paths.exports_dir` | Parent of the state directory (`<exports_dir>/jsonl/searches/`) |
| `slack.bot_token` | Bot token used to post |
| `slack.search_channel` | Default destination for hits; falls back to `notify_channel` when unset |
| `gcs.ebay_bucket` | Staging bucket for the hits JSONL |
| `bigquery.ebay_dataset` | Dataset holding `search_hits` |

`SHOEBOX_CONFIG_PATH` overrides the location of `app.yaml`; `--config` on each
search command overrides `paths.searches_file` for that invocation.

### `configs/searches.yaml`

**Gitignored** — the repo is public and your buy criteria, price ceilings and
blocked sellers are not. The committed template is `configs/searches.example.yml`
and it is heavily commented; start there.

The document has three top-level blocks:

```yaml
version: 1                    # must be 1

channels:                     # optional alias -> Slack channel ID map
  card_alerts: C0123456789

defaults:                     # inherited by every search below
  interval: 30m
  channel: card_alerts

searches:                     # the list of searches
  - name: jordan_psa10_bin
    query: "michael jordan psa 10"
    category_ids: ["261328"]
    interval: 15m
    price: { min: 25, max: 200 }
    title_exclude: [reprint, lot, custom]
```

Validation is strict on purpose (`shoebox/models/saved_search.py`): unknown keys
are **rejected** with their exact location (`searches.0.intervl`), because a
search that silently searches for the wrong thing is worse than one that refuses
to load. `shoebox watch-searches --list` validates the file and prints each
search's resolved interval and destination without running anything.

**Inheritance rule:** omitting a key inherits `defaults`; setting it to `[]`
explicitly overrides a non-empty default with "no filter". There is no nested
`filters:` block — the schema is deliberately flat, because deep-merge semantics
are ambiguous.

### Choosing a channel

Resolution order, first match wins:

1. `channel:` on the search
2. `channel:` under `defaults:`
3. `slack.search_channel` in `app.yaml`
4. `slack.notify_channel` in `app.yaml`

A `defaults.channel` in `searches.yaml` therefore outranks `search_channel` in
`app.yaml`. Values are Slack **IDs** (`C…`/`G…`/`D…`, from channel details → copy
ID), not `#names`; an alias defined in `channels:` may be used anywhere an ID
can. A typo'd alias fails at load with the list of aliases that do exist, rather
than as a `channel_not_found` on the 3am cron run. Invite the bot to every
channel you route to.

---

## Writing a search

### Identity — what is searched

| Key | Notes |
|---|---|
| `name` | **Required.** `^[a-z0-9][a-z0-9_-]{0,63}$`. Used as the seen-cache filename and as part of the dedup key, so renaming a search re-seeds it |
| `query` | Max 100 chars (eBay truncates `q` beyond that); `*` wildcards rejected. Space-separated terms are AND; `(a, b)` is OR |
| `category_ids` | A list, but eBay Browse accepts **exactly one** per request. Split anything wider into separate searches. **Inheritable** — set it once under `defaults:` when a whole file searches one category; a search overrides it, and `[]` there means "no category filter" rather than "inherit" |
| `price` | `{min, max}` — at least one bound, non-negative, `min ≤ max`. Emits `priceCurrency` automatically |
| `aspects` | `{Aspect: [values]}` — see [Aspects](#aspects). Requires exactly one `category_ids` |

At least one of `query` or `category_ids` must be present — counted after the
merge, so a query-only search is fine when `defaults:` supplies the category.
The same goes for `aspects`, which needs exactly one category: an inherited one
counts.

`name`, `query`, `price` and `aspects` are the fields that cannot be inherited;
everything else in this document can live under `defaults:`.

### Scheduling and volume

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Disabled searches are listed by `--list` but never run |
| `interval` | `30m` | `<number><unit>`, unit `s`/`m`/`h`/`d`, minimum 60s. **Bare numbers are rejected** — `15` is ambiguous between seconds and minutes. This is a *floor*: actual cadence is also bounded by how often cron fires |
| `sort` | `newlyListed` | `newlyListed`, `endingSoonest`, `price`, `-price`. Best Match is intentionally unavailable — it returns an arbitrary slice of the result set, so a genuinely new listing could stay invisible for days |
| `max_results` | `200` | Items fetched per poll. One Browse call returns up to 200, so raising this within that bound costs nothing extra |
| `seed_max_results` | `2000` | Items fetched on the silent first run. Must be ≥ `max_results` — anything that matches but is not seeded resurfaces later as a false "new listing", so seed wide |
| `max_notify` | `10` | Cap on Slack thread replies per run. Slack allows ~1 message/sec/channel, so this is a time budget as much as a noise budget. Overflow is summarized in one line and recorded, never silently dropped |
| `notify_on_seed` | `false` | Post the first `max_notify` matches when the search seeds, instead of only the confirmation line — see [Seeing the initial results](#seeing-the-initial-results) |
| `prune_seen_after_days` | `90` | Seen-cache entries older than this are dropped at the end of a run |

A search's effective cadence is `max(interval, cron period)`, with a 10% grace
on the interval check so a search on a 15m interval driven by a 5m cron doesn't
drift to 20m.

### Filters: server-side vs post-filtered

This split is the single most important thing to understand when tuning. eBay
caps the result set at `max_results` **before** you see it, so every Python-side
filter consumes result slots: fetch 200 items with a post-filter that rejects
90% and you get 20 usable listings. Push everything eBay supports server-side.

**Server-side** — sent to eBay in the `filter` string, free:

| Key | Emitted clause |
|---|---|
| `buying_options` | `buyingOptions:{FIXED_PRICE\|AUCTION}` |
| `price` + `currency` | `price:[25..200]`, `priceCurrency:USD` |
| `conditions` | `conditions:{NEW\|USED}` |
| `item_location_countries` (exactly one) | `itemLocationCountry:US` |
| `delivery_country` | `deliveryCountry:US` |
| `sellers` / `exclude_sellers` | `sellers:{…}` / `excludeSellers:{…}` — mutually exclusive; caps 250 / 100 |
| `free_shipping_only` | `maxDeliveryCost:0` |
| `aspects` | separate `aspect_filter` parameter |

**Post-filtered in Python** (`transforms/search_filters.py`) — costs result
slots:

| Key | Why it can't be server-side |
|---|---|
| `title_exclude` | **Browse has no negative-keyword support at all.** `q` is positive-match only. This is the unavoidable one |
| `title_must_include_all` / `title_must_include_any` | `q` matches the whole listing with Best-Match fuzz, not exact title substrings |
| `seller_min_feedback_score` | No seller-quality filter exists. A seller with unknown feedback fails a threshold you explicitly asked for |
| `item_location_countries` (2+) | `itemLocationCountry` takes a single value |
| `max_total_price` | eBay filters item price and delivery cost independently; their *sum* needs Python |

Title matching is case-insensitive substring matching, not word matching:
`title_exclude: [lot]` also drops "Camelot".

> ⚠️ **eBay returns only `FIXED_PRICE` listings when `buyingOptions` is absent.**
> The clause is therefore always emitted, and an empty `buying_options` list is
> rejected at load — otherwise an auction watcher would silently never fire.

### Aspects

Aspects are eBay's structured item attributes — Player/Athlete, Season,
Parallel/Variety, Grade, Professional Grader, Set. They are filtered on eBay's
side, so unlike `title_exclude` they cost no result slots, which makes them the
best available narrowing tool.

They are category-specific *and* result-set-specific, so discover them against
the search itself:

```bash
shoebox search-aspects graded_chrome_autos --top 5
```

Copy a name/value pair straight into the search:

```yaml
aspects:
  Grade: ["10"]
  Professional Grader: ["Professional Sports Authenticator (PSA)"]
```

Constraints: exactly one `category_ids` is required (eBay's `aspect_filter` must
repeat the category ID inside the filter string), values may not contain `,`
(eBay documents no escape), and every aspect must have at least one value.

---

## Tuning a search

The loop is: write it → preview it → narrow it → dry-run it → let it go live.

**1. See exactly what eBay is asked and what comes back.**

```bash
shoebox preview-search jordan_psa10_bin
```

Prints the literal request (`query`, `category_ids`, `sort`, `filter`,
`aspect_filter`), a pass/reject count, a breakdown of which config key rejected
how many listings, and the result table with rejected rows dimmed. It writes
nothing — no Slack, no seen-cache, no BigQuery — so run it as often as you like.

Useful flags: `--rows 0` (print all), `--passed-only`, `--max-results N`,
`--csv exports/preview.csv`.

**2. Analyse it properly in pandas when the terminal isn't enough.**

```python
from shoebox.pipelines.preview_search import preview_search

df = preview_search("jordan_psa10_bin")
df[df.passed].sort_values("total_price")  # what would alert, cheapest first
df[~df.passed].dropped_by.value_counts()  # what each filter is costing you
```

Columns: `passed`, `dropped_by`, `title`, `price`, `shipping`, `total_price`,
`buying`, `condition`, `seller`, `feedback`, `country`, `listed`, `item_id`,
`url`.

**3. Move rejections server-side.** If `dropped_by` shows a filter throwing away
most of the fetch, ask whether an aspect, a tighter category, or a price bound
can do the same job on eBay's side. A post-filter rejecting 90% means you are
paying for 200 results and reading 20.

**4. Rehearse the actual Slack output.**

```bash
shoebox watch-searches --dry-run --only jordan_psa10_bin
```

Logs the parent message, up to `max_notify` item messages, the overflow line,
and a warning for any listing with no photo (those fall back to a link unfurl).
No Slack, no state writes, no flush. On a search that hasn't seeded yet this
prints a 15-row sample of what the seed would record.

**5. Go live.** Enable it and let the next scheduled run seed it.

If you change a search's criteria *after* it has been running and want a clean
baseline, re-seed it:

```bash
shoebox watch-searches --reseed jordan_psa10_bin
```

That discards the seen-cache and silently re-records everything currently
matching. Without it, a loosened filter dumps its entire newly-matching backlog
into Slack as "new".

---

## Scheduling

One scheduled entry drives every search; each search's own `interval` plus its
stored `last_run_at` decides which actually fire, so adding a search means
editing YAML and nothing else. Run the command every few minutes and let the
intervals do the rest.

Whatever the scheduler, two things matter: the working directory must be the
repo root (config paths resolve relative to it), and the `shoebox` entry point
must be the one inside your virtual environment.

**Linux / macOS — cron:**

```bash
*/5 * * * * cd /Users/you/shoebox && ./.venv/bin/shoebox watch-searches >> logs/cron_watch_searches.log 2>&1
```

On macOS, `cron` needs Full Disk Access granted to `/usr/sbin/cron`. The
alternative is a `launchd` agent with `StartInterval 300`, a `WorkingDirectory`
of the repo root, and `StandardOutPath` under `logs/`.

**Windows — Task Scheduler:**

The scheduled tasks on the Windows host are generated from
`scripts/tasks.yaml` — see [scheduling.md](scheduling.md). `watch-searches` is
already defined there as a logon trigger repeating every 5 minutes:

```bash
python scripts/generate_tasks.py --only watch-searches --register
```

For a one-off task outside that setup:

```powershell
schtasks /create /tn "shoebox watch-searches" /sc minute /mo 5 /ru "%USERNAME%" ^
  /tr "cmd /c cd /d C:\shoebox && .venv\Scripts\shoebox.exe watch-searches >> logs\watch_searches.log 2>&1"
```

`cd /d` is what sets the working directory, and `cmd /c` is what makes the
redirect work — a bare `/tr` command line does not go through a shell. In the
Task Scheduler GUI the equivalent is *Start in* = the repo root; omitting it is
a silent, every-run failure. Tick **Run whether user is logged on or not** for
an unattended host, and leave *Stop the task if it runs longer than* well above
your longest expected run, since a run posting a full `max_notify` thread takes
tens of seconds.

Overlapping runs are prevented by a non-blocking advisory lock on
`exports/jsonl/searches/.lock` (`flock` on Unix, `msvcrt.locking` on Windows): a
run posting at ~1 msg/sec can outlast the scheduler's period, so if the previous
tick is still going the new one logs and exits 0. Task Scheduler's own "do not
start a new instance" rule is a reasonable belt-and-braces addition, but the
lock does not depend on it.

**API budget.** One due search costs one Browse call per run (up to 200 items).
Ten searches at 15m ≈ 960 calls/day against a default Browse ceiling of ~5,000.
The same ten at 5m ≈ 2,900/day — still under, but tighten `max_results` before
adding many more at that cadence. When nothing is due, the run returns before
even constructing the eBay client.

### Flags

| Flag | Effect |
|---|---|
| `--list` | Validate the config and list searches; run nothing |
| `--dry-run` | Log what *would* be posted. No Slack, no state, no GCS/BigQuery |
| `--force` | Ignore intervals; run every enabled search now |
| `--only NAME` | Restrict to one search (repeatable). Still interval-gated unless combined with `--force` |
| `--reseed NAME` | Discard that search's seen-cache and silently re-seed (repeatable) |
| `--config PATH` | Override `paths.searches_file` |
| `--no-flush` | Skip the GCS/BigQuery flush this run |

---

## What lands in Slack

**Parent message**, only for a search that actually has hits — a "0 new" post
every interval would drown the channel:

```
🔎 jordan_psa10_bin — 3 new listings
$25.00–$200.00 · Buy It Now · every 15m
```

**One thread reply per listing**, capped at `max_notify` — a Block Kit
`section`, an `image` block with the listing photo at 500px, and a `divider`
closing the card:

```
2026 Topps Tribute Crest Calligraphy Barry Zito Auto Blue /150
$59.99 (bid) · Auction · 8d 13h left
+$5.98 shipping
seller `cmcardshop` (330)
[photo]
────────────────────────────
```

One fact per line. Shipping sits below the price rather than beside it, because
two amounts dot-separated on one line invite reading the second as the first;
the line is omitted entirely when eBay quotes no shipping. The qualifier next to
the price is italic — Slack mrkdwn has no font-size control, so italics is the
only way to mark it as secondary — and reads `8d 13h left` on an auction (from
`item_end_date`, omitted when eBay doesn't send one) or `or Best Offer` on a
fixed-price listing that accepts offers.

Carrying the photo ourselves means the picture doesn't depend on eBay's OG tags
and Slack's crawler. Listings with **no** photo fall back to a bare URL with
unfurling on, and get no divider: blocks would put the URL inside one, and the
unfurl that path exists for keys off the URL being in the message text.

**Overflow line** when `max_notify` truncates, so nothing disappears quietly:

```
+7 more new listings not shown (max_notify=10). All 17 are recorded, so they won't alert again.
```

**Seed confirmation**, one line on the first run — a fully silent seed is
indistinguishable from a broken config:

```
🌱 jordan_psa10_bin — seeded with 412 existing listing(s). Future runs alert on new ones only (every 15m).
```

With `notify_on_seed: true` that line becomes the parent of a thread carrying
the first `max_notify` matches — see
[Seeing the initial results](#seeing-the-initial-results).

**Failure summary**, posted only when something went wrong, plus a separate
"bad config" message if the YAML fails to load (under cron nobody reads the log).

---

## State, dedup and seeding

Everything lives under `<exports_dir>/jsonl/searches/`:

| File | Lifetime | Contents |
|---|---|---|
| `search_state.json` | Persistent | Per-search `last_run_at`, `seeded_at`, `seed_count`, last status/error/new-count |
| `<name>_seen.jsonl` | Persistent, append-only | The dedup cache: one `SeenEntry` per observation, later lines win. Compacted and pruned at end of run |
| `search_hits_append.jsonl` | Cleared after a successful flush | Buffered BigQuery rows; survives a failed flush and retries next run |
| `.lock` | Per run | Advisory lock guarding against overlapping scheduled invocations |

**Dedup** is item-id based and per search (`search_name` + `item_id`), and runs
entirely off the local cache — never off BigQuery. That is why a GCS or BigQuery
outage can delay the durable log but can never cause a duplicate alert or a
missed one.

**Seeding** records every current match with `notified: false` and alerts on
none of them. It happens when:

- the search has never been seeded, or
- `--reseed` was passed, or
- the seen-cache was lost while the state still claims "seeded" (the two are
  separate files, so a stray `rm exports/jsonl/*.jsonl` would otherwise make
  every listing look brand new — a search that legitimately seeded *zero*
  matches is excluded from this guard, so a narrow search still alerts on its
  first genuine hit), or
- the search has been idle for more than **6×** its interval (asleep laptop,
  long-disabled search) — those matches are hours old and alerting on them
  would be noise.

Items past the `max_notify` cap are recorded as `notified: false` too. They are
saved precisely so they never alert; without that they would re-alert forever.

### Seeing the initial results

By default a seed is silent because its job is to establish a baseline, not to
report one. Set `notify_on_seed: true` (per search, or in `defaults:`) when you
want a new search to show you what is *already* out there:

```yaml
searches:
  - name: barry_zito_autos
    query: "barry zito auto"
    notify_on_seed: true
```

The seed then posts its normal parent line plus the first `max_notify` matches
as thread replies, identical in shape to a real alert:

```
🌱 barry_zito_autos — seeded with 412 existing listing(s), showing 10 below. Future runs alert on new ones only (every 15m).
```

Three things to know:

- **The cap still applies.** A seed fetches `seed_max_results` (2000 by default)
  to build a complete cache; only the first `max_notify` are posted, in the
  search's `sort` order — with `newlyListed` that's the most recent. All of them
  are recorded, so the unshown ones never alert later.
- **It fires on a first-ever seed and on `--reseed`.** Both are seeds a person
  asked for and is waiting on output from.
- **The recovery seeds stay silent regardless.** A lost seen-cache or a search
  idle for more than 6× its interval re-seeds without posting listings, because
  those exist to *prevent* an alert storm over listings that are already old.
  The confirmation line is still posted, so the recovery is visible.

For a one-off look at what a search matches — with the rejected listings and the
filter that rejected each — use [`preview-search`](#tuning-a-search) instead. It
writes nothing and doesn't need the search to be seeding.

Already-seen items are refreshed every run so `last_seen_at` and `last_price`
stay current — that's the seam a future price-drop alert builds on.

---

## The durable log (`ebay.search_hits`)

Every observation — seeds, alerts and overflow — is buffered locally and flushed
once per run (only when something new was found, keeping load jobs to a handful
a day) via GCS `logs/search_hits/…` into BigQuery.

One flat row per observation. Notable columns: `run_id`, `search_name`,
`item_id`, `cache_key`, `hit_type` (`NEW_LISTING` in v1 — the extension point
for `PRICE_DROP`/`ENDING_SOON`), `hit_at`, `first_seen_at`, `is_seed`,
`notified`, `notified_at`, `slack_channel`, `slack_parent_ts`, plus the listing
snapshot (`title`, `item_web_url`, `thumbnail_url`, `item_origin_date`,
`last_price`, `current_bid`, `shipping_cost`, `free_shipping`, `buying_options`,
`condition`, `seller_username`, `seller_feedback_score`,
`item_location_country`, `leaf_category_id`, `item_end_date`). Full schema:
`configs/bigquery/schemas/search_hits.json`.

`is_seed` and `notified` are what separate real alerts from silent bookkeeping:

```sql
-- Alert volume per search, last 30 days
SELECT search_name, COUNT(*) AS alerts
FROM `PROJECT.ebay.search_hits`
WHERE notified AND NOT is_seed
  AND hit_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY search_name
ORDER BY alerts DESC;
```

---

## Guarantees and failure behaviour

- **Slack first, then state, per item.** If state were committed first, a Slack
  failure would mark an item seen and it would never be alerted — silently and
  undetectably. This way a crash re-alerts an item: visible and self-limiting.
  Committing per item rather than per batch bounds a mid-thread crash to exactly
  one duplicate. For an alerting system a duplicate is a shrug; a miss is the
  whole feature failing.
- **One bad search doesn't stop the run.** Each search runs in its own
  `try/except`; failures are collected and posted as one summary.
- **A failed search still advances `last_run_at`** — otherwise a permanently
  broken search would retry every tick and burn the Browse quota.
- **A config error aborts everything.** It's global, not per-search, so the run
  fails rather than half-running a broken file — and the error goes to Slack.
- **A failed flush is a warning, not a failure.** The buffer survives and
  retries next run.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Missing searches file: configs/searches.yaml` | Copy `configs/searches.example.yml`, or point `paths.searches_file` elsewhere |
| `extra_forbidden` at `searches.0.<key>` | A typo'd or unsupported key — the path in the error is the location in the YAML |
| `interval must be a string like '15m'` | Bare numbers are rejected; quote the unit |
| `no Slack channel` for a search | Set `slack.search_channel` in `app.yaml`, or a `channel:` on the search/defaults |
| `channel_not_found` / `not_in_channel` | Wrong ID, or the bot was never `/invite`d into that channel |
| `unknown channel alias` | The alias isn't in the `channels:` block — the error lists the ones that are |
| Nothing posts, log says "No searches due" | Intervals haven't elapsed. `--force` to override, `--list` to see the resolved intervals |
| Nothing posts, and nothing is due either | Another run holds `.lock` ("Another watch-searches run holds the lock") — a previous tick is still posting |
| Everything alerted at once | A seen-cache was deleted while `search_state.json` survived, or the search was renamed (the name is part of the dedup key) |
| A whole backlog alerted after a config change | Expected — loosening a filter makes old listings newly matching. Use `--reseed <name>` after widening a search |
| `preview-search` shows results but the watcher posts nothing | Those items are already in the seen-cache; preview ignores dedup entirely |
| No listings returned at all | The eBay-side filter is too narrow — `preview-search` prints the exact filter string it sent |
| Most results rejected by post-filters | Check the "Rejected by" breakdown; move what you can server-side or raise `max_results` |
| `search-aspects` returns nothing | eBay reports aspects per category — the search needs `category_ids` |
| Auctions never appear | `buying_options` is missing `AUCTION`; eBay defaults to `FIXED_PRICE`-only |
| Warning about a failed flush every run | GCS/BigQuery credentials or permissions; alerts are unaffected. `--no-flush` to silence |

---

## Where the code lives

| File | Role |
|---|---|
| [shoebox/models/saved_search.py](../shoebox/models/saved_search.py) | YAML schema, defaults inheritance, validation, channel aliases |
| [shoebox/transforms/search_filters.py](../shoebox/transforms/search_filters.py) | Browse `filter`/`aspect_filter` builders, post-filters, rejection reasons |
| [shoebox/clients/ebay_rest/browse.py](../shoebox/clients/ebay_rest/browse.py) | Browse search + aspect refinements |
| [shoebox/pipelines/watch_searches.py](../shoebox/pipelines/watch_searches.py) | The run loop: fetch → filter → diff → alert → commit |
| [shoebox/clients/search_state.py](../shoebox/clients/search_state.py) | Seen-cache, run state, lock, hits append log → GCS → BigQuery |
| [shoebox/models/search_hit.py](../shoebox/models/search_hit.py) | `SeenEntry` (local dedup) and `SearchHit` (BigQuery row) |
| [shoebox/utils/search_formatting.py](../shoebox/utils/search_formatting.py) | Slack message builders |
| [shoebox/pipelines/preview_search.py](../shoebox/pipelines/preview_search.py) | `preview-search` and `search-aspects` |

Tests: `tests/test_saved_search_config.py`, `test_search_filters.py`,
`test_search_state.py`, `test_watch_searches_flow.py`,
`test_search_formatting.py`, `test_preview_search.py`.
