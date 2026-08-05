# Slack Integration

The Slack layer (migrated from Telegram) provides three capabilities:

1. **Outbound notifications** — pipeline status, order summaries, release
   announcements.
2. **Interactive price approvals** — an Approve button plus threaded-reply
   overrides, used by the listing/relist pricing flows.
3. **A command bot** — run whitelisted CLI commands from chat.

Everything runs over **Socket Mode** (outbound websocket; no public endpoint),
using `slack-bolt`'s async API. App setup (scopes, events, interactivity) is
covered in [setup.md](setup.md); channel/token configuration in
[configuration.md](configuration.md).

## Module layout

| Module | Role |
|---|---|
| `utils/slack.py` | Core primitives: send, wait-for-reply, approval flow, command listener |
| `utils/slack_formatting.py` | mrkdwn helpers: titles, bullets, tables, message chunking |
| `services/slack_bot_service.py` | The command bot built on `listen_for_commands` |
| `utils/pricing.py` (`parse_price_reply`) | Tolerant parsing of human price replies |

## Core API (`utils/slack.py`)

Synchronous wrappers (each runs its own event loop; safe to call from plain
pipeline code):

```python
notify(channel, message, thread_ts=None, unfurl_links=False, blocks=None) -> str
```
Posts a message; returns its `ts` so follow-ups can thread under it. Used for
all fire-and-forget notifications and the threaded order summaries.

`unfurl_links` must be opted into: **bot tokens do not expand text links by
default**, so a message whose value *is* the link preview gets no preview at
all unless it's set. Treat previews as best-effort — they depend on eBay's OG
tags and Slack fetching them asynchronously, neither of which is under our
control. When an image matters, send a Block Kit `image` block instead (see
[Saved-search alerts](#saved-search-alerts)).

`blocks`, when given, becomes the rendered message and `message` is demoted to
the push-notification and fallback string — so it must still read as a complete
summary on its own.

```python
notify_and_wait(channel, message, timeout_s=600) -> str
```
Posts a message, then waits for a **threaded reply** to it and returns the
reply text. Ignores the bot's own messages. Raises `TimeoutError` after
`timeout_s`.

```python
notify_and_wait_approval(channel, message, timeout_s=600,
                         approve_label="Approve") -> ApprovalResult
```
Posts the message with a primary **Approve** button (Block Kit,
`action_id="shoebox_approve"`) and a hint ("…or reply in this thread
with a new value"). Resolves on whichever comes first:

- **Button click** → `ApprovalResult(approved=True, approved_by=<user id>)`
- **Threaded reply** → `ApprovalResult(approved=False, reply_text=<text>)`
- **Timeout** → raises `TimeoutError`

In all three cases the original message is edited to show the outcome
(`✅ Approved by @user` / `✏️ Override received: …` / `⏰ Timed out`) and the
button is removed, so it can't be double-clicked or clicked after the waiting
pipeline has moved on. Clicks on the wrong message are ignored (matched by
`ts`).

```python
notify_and_wait_reaction(channel, message, timeout_s=600,
                         accepted_reactions=None) -> bool
```
Legacy helper (Telegram heart-tap idiom): waits for a `:heart:` reaction on
the message. Kept for compatibility; no current call sites — the approval flow
uses the button instead.

```python
listen_for_commands(dispatch)  # async, long-running
```
Socket Mode listener behind the command bot: for each top-level (non-thread)
message in `slack.command_channel`, parses `command --flags` (leading `/`
optional) and awaits `dispatch(event, client, command, args)`. When
`slack.allowed_user_ids` is non-empty, messages from any other user are
ignored and logged at WARNING.

### Reliability

- Every client the module creates (including the Bolt app's client) carries
  slack_sdk's `AsyncRateLimitErrorRetryHandler` (3 retries honoring
  `Retry-After`). Slack allows ~1 `chat.postMessage`/sec/channel; the threaded
  order-summary burst can exceed that, and without the handler a 429 raised
  and dropped the message.
- Each `notify_and_wait*` call opens a fresh Socket Mode connection for the
  duration of the wait (~1–2 s connect overhead). This is simple and robust
  for the current usage pattern (sequential prompts); a persistent connection
  would be the optimization if prompt volume grows.

## Formatting (`utils/slack_formatting.py`)

Slack mrkdwn has no table markup, so tables are rendered inside fenced code
blocks (monospace preserves column alignment, mirroring the terminal's rich
tables):

- `title(text)` → `*text*`
- `bullets(items)` → `• …` lines
- `table(headers, rows, col_spacing=4)` → aligned grid in a code block.
  **Mobile fallback:** Slack mobile doesn't horizontally scroll code blocks, so
  any table wider than `MOBILE_SAFE_WIDTH` (34 chars) automatically switches to
  a stacked `Header: value` block per record, which wraps gracefully at any
  width.
- `table_messages(heading, headers, rows, max_chars=2900)` → bold heading, a
  blank line, then the table — split across multiple messages
  (`(part i/N)` suffixes) when it would exceed `max_chars`, which is kept well
  under Slack's real limit so messages stay readable without "see more".

## Price approval flows

Three pipelines prompt the pricing channel. Replies are parsed by
`utils/pricing.parse_price_reply`, which tolerates `$4.99`, ` 4.99 `,
`1,299.99` and returns `None` for anything that isn't a positive price
(`no`, `Y`, typos) — callers then apply an explicit policy:

| Pipeline | Prompt | Accept | Override | On timeout / unparseable reply |
|---|---|---|---|---|
| `relist-listings` | `*Price approval:* <title>` with `Approve $X` button | Button click | Threaded reply with a number | **Skip the listing** (stays live untouched; next run retries). Rationale: approval is the point — never change a price without sign-off |
| `create-listings --scrape-prices` | `Please confirm price: X - <card>` | Reply `Y`/`YES` | Reply with a number | **Proceed with the proposed price** (scraped price, or the queue price if scraping failed). Rationale: the card is queued to be listed; the best-known price beats aborting the whole run |

Default timeout is 600 s per prompt (`timeout_s` parameter).

## The command bot (`services/slack_bot_service.py`)

Started with `shoebox slack-bot`; runs until interrupted (use a process
supervisor for always-on).

**Usage in the command channel:**

```
sync-orders
/create-listings --dry-run
orders-awaiting-shipment --message --pull-order
help
```

**Behavior:** the command is validated against a whitelist
(`ALLOWED_COMMANDS`); unknown commands get the help text, and unrecognized
flags are stripped with a warning. Valid commands run as a subprocess
(`python -m shoebox.cli <command> …`, 600 s cap). The bot replies in a
thread on the triggering message: `Running: …`, then `Done` with
stdout+stderr in a code block (tail-truncated near Slack's message limit).

**Whitelisted commands:** `sync-metadata`, `end-oos-listings`,
`create-queue-excel`, `create-listings` (`--dry-run --publish --schedule
--scrape-prices`), `sync-active-listings`, `sync-active-listing-details`,
`sync-orders`, `orders-awaiting-shipment` (`--pull-order --buyer-order
--display --message`), `watch-searches` (`--force --dry-run --list`). `ui` is
deliberately excluded (needs a display), as are the pipelines that would
themselves block on Slack approval prompts.

> Note: the validator keeps only exact flag matches, so value-taking flags
> (`--only NAME`, `--config PATH`) can't be whitelisted — the value would be
> silently dropped and the flag would then error. Run those from a shell.

**Security:**
- Channel-scoped: only messages in `slack.command_channel` are considered.
- Optional user allowlist: set `slack.allowed_user_ids` to restrict execution
  to specific member IDs; everyone else is silently ignored (logged at
  WARNING, no channel feedback).
- The whitelist means arbitrary shell/CLI input is never executed — commands
  and flags must match exactly.

## Threaded order summaries

`orders-awaiting-shipment --message` posts a single parent line
(`📦 Orders Awaiting Shipment — 3 buyer(s), 12 item(s)`) and sends every
section table as a reply in its thread. The channel shows one line per run;
details live in the thread. Long tables chunk into `(part i/N)` messages
within the same thread.

## Saved-search alerts

`watch-searches` posts to `slack.search_channel` (falling back to
`notify_channel`, or a per-search `channel:` override in `searches.yaml`). Same
parent-plus-thread shape as the order summaries: one parent per search that has
hits, each new listing as a reply.

Three deliberate differences from the table-based summaries:

- **No code fences.** `slack_formatting.table()` renders inside a fenced block,
  and Slack neither linkifies nor unfurls URLs there. Item replies are mrkdwn —
  `*<url|Title>*`, then price/condition/seller lines.
- **The photo is an image block, not an unfurl.** `build_item_message()` returns
  a `section` + `image` pair carrying the listing photo, and turns
  `unfurl_links` off. Leaving the picture to Slack's crawler would make the most
  useful part of a card alert depend on eBay's OG tags — so the image is
  requested explicitly instead. eBay serves every size off one CDN path, so
  `ItemSummary.thumbnail(500)` rewrites the `s-l<n>` segment to get a 500px
  image at no extra API call; Slack downscales anything larger anyway.
  A listing with no photo at all falls back to the old behavior — bare URL on
  its own line plus `unfurl_links=True`, which is its only shot at an image.
  `--dry-run` flags those listings so you know before the run goes live.
- **Explicit pacing.** Slack permits roughly one `chat.postMessage` per second
  per channel, and each `notify()` spins up its own event loop. The SDK's
  rate-limit handler only reacts *after* a 429 and gives up after three
  retries — which would abort a run mid-thread — so the watcher sleeps ~1s
  between replies and caps them at `max_notify` with a summarized overflow line.

A search with no new listings posts nothing at all: at ten searches on a
15-minute interval, "0 new" messages would otherwise be thousands per day.

## Design notes & history

- **Telegram → Slack mapping:** `notify` ↔ `send_message_only`;
  Telegram reply-to ↔ Slack threaded reply; Telegram ❤-reaction approval ↔
  Block Kit Approve button (reactions survive as the unused
  `notify_and_wait_reaction`); the Telegram bot service ↔ `slack-bot` over
  Socket Mode. Chat IDs (ints) became channel IDs (strings).
- **Why Socket Mode:** no inbound endpoint to host or secure; works from any
  machine that can reach Slack.
- **Why buttons over reactions:** one tap on mobile, unambiguous target
  message, attribution (`approved_by`), and the message self-updates to a
  terminal state so stale prompts can't be acted on.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `notify` works but waits always time out | Event Subscriptions missing `message.channels`/`message.groups`, or bot not in the channel |
| Button does nothing | Interactivity toggle off in the app config |
| Replies ignored | Reply wasn't threaded (must reply *in the thread*, not the channel), or it came from the bot itself |
| Command bot silent for a user | `allowed_user_ids` is set and doesn't include them (check service logs for the WARNING) |
| `invalid_auth` / `not_in_channel` errors | Wrong token in `app.yaml`, or bot not invited |
| Messages dropped during bursts | Shouldn't happen (rate-limit retries); if it does, check for `SlackApiError` after 3 retries in logs |
