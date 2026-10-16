# Shoebox Queue (iOS)

A small SwiftUI app that builds the eBay listing queue from an iPhone. It talks to the
`shoebox api` FastAPI service (see `docs/api.md` at the repo root) which serves the
set / subset / parallel / card dropdown metadata from BigQuery, enriches rows with the same
`build_enriched_json` the Streamlit UI uses, and writes `listing_queue_enriched.jsonl` to GCS.
`shoebox create-listings --queue gs://...` on the workstation consumes it unchanged, or the app
starts the same command as a Cloud Run job (see *Listing from the app* below).

The app mirrors the single-form Streamlit flow (`shoebox/ui/app.py`): one draft form, a queue of
saved rows, Edit / Delete / reorder, Preview (enriched JSON per row) and Submit.

## Prerequisites

- macOS with **Xcode 15 or newer** (iOS 17 SDK). The project uses the Swift 5 language mode.
- [XcodeGen](https://github.com/yonaskolb/XcodeGen): `brew install xcodegen`
- An iPhone 15 (or any iOS 17) simulator, or a device with a development team set up.
- The API running somewhere reachable: either locally (`SHOEBOX_API_KEY=dev shoebox api --port 8080`)
  or the deployed Cloud Run service `shoebox-api` (see `docs/deployment.md`).

There are no third-party packages; nothing to `pod install` or resolve.

## Generate and open the project

```bash
cd ios/ShoeboxQueue
xcodegen generate
open ShoeboxQueue.xcodeproj
```

`project.yml` is the source of truth. The generated `ShoeboxQueue.xcodeproj` and
`ShoeboxQueue/Info.plist` are git-ignored; re-run `xcodegen generate` after adding files or
changing the spec. Select the `ShoeboxQueue` scheme, pick a simulator and press Cmd-R.

### App icon

`Resources/Assets.xcassets/AppIcon.appiconset/AppIcon-1024.png` is the single 1024 px icon iOS 17
needs; the SVG sources (icon and logo) are in `design/`. To change it, edit `design/app-icon.svg`
and re-render it opaque at 1024×1024 (for example `cairosvg design/app-icon.svg -W 1024 -H 1024
-o .../AppIcon-1024.png`; icons may not have transparency).

### Signing

The bundle id placeholder is `com.example.ShoeboxQueue`. For a real device set
`PRODUCT_BUNDLE_IDENTIFIER` and `DEVELOPMENT_TEAM` in `project.yml` (or change them under
*Signing & Capabilities* in Xcode; those edits are lost on the next `xcodegen generate`, so prefer
the spec).

## Pointing the app at the API

Open the **Settings** tab:

- **Base URL**: `http://localhost:8080` for a local `shoebox api` run from the simulator, or the
  Cloud Run URL (`https://shoebox-api-xxxxx-uc.a.run.app`). Only plain `http` to localhost / `.local`
  hosts is allowed by the app's ATS exception; everything else must be `https`.
- **API key**: the value of `SHOEBOX_API_KEY` (the `shoebox-api-key` secret in Secret Manager). It is
  stored in the Keychain, not in UserDefaults.
- **Save**, then **Test connection** (calls `GET /health`, which needs no key), then
  **Refresh metadata** (calls `GET /metadata` with the key; a 401 here means the key is wrong).

Metadata is cached on disk (`metadata.json` + `metadata.meta.json`) with its ETag, so the app opens
and lets you build rows offline; a failed refresh shows a "using cached metadata" banner.

## Using the app

- **Queue tab** – *Clear* and *Save* sit in the top bar, above the Add / Edit form (set -> subset
  -> parallel -> card #, with the same cascade and autofill as the Streamlit UI), quantity, price
  and the two images. *Save* (*Update* while editing) appends or updates the row and keeps set /
  subset / parallel for the next card (card # resets, quantity -> 1, price -> 0.99, images
  cleared). *Clear* resets the whole form. Tap a row to edit it, swipe to delete, use *Edit* next
  to the queue heading to reorder. The toolbar menu holds *Save as draft…*, *Drafts…*, *Save draft to server*,
  *Load last submitted queue…* and *Clear queue*.
- **Pickers** – the Set picker is sectioned by year (newest first; the year comes from the
  metadata's `set_year`, else the set name's leading digits). Subsets list *Base* first, then
  A–Z. Typing in the search field and pressing **Search** selects the best match (an exact match,
  else the first option starting with the text, else the first containing it), so `12` + Search
  picks card 12, not 112. The Card # list opens with the search field already focused.
- **Card batches** – in add mode, each card # has a checkbox. Check several (once one is checked,
  tapping a row or pressing **Search** checks instead of picking) and tap *Add N cards*: the form
  loads the first card (list order) and shows "Card 1 of N" with *Skip* / *End*. Each *Save* (or
  *Save & Next* on the keyboard bar) queues the card and loads the next one with the front scan
  field focused. Editing a queued row mid-batch returns to the batch card after *Update*;
  *Clear* or picking another set / subset ends the batch.
- **Images** – either a bare **scan number** typed on a number pad (*Done* above the keyboard, or
  a scroll, closes it; stored verbatim; the workstation resolves it to
  `<scans_dir>/<scan_prefix><number><scan_extension>` exactly as today), or a **photo** from the
  library. Photos are downscaled to 2000 px / JPEG 0.85, saved locally and uploaded to
  `POST /images` immediately when the API is configured; a row whose photo is still pending shows a
  warning badge and *Submit* uploads it first.
- **Preview tab** – `POST /queue/preview` shows each row's enriched JSON (all 21 fields in the
  `build_enriched_json` order), warnings (missing images, unknown parallel, duplicates) and the
  red `No checklist match` error. *Submit* is `PUT /queue`: it writes the JSONL only if every row
  enriches; otherwise the per-row errors are listed and nothing is written.
- The working queue autosaves to `current.json` and is restored on launch. Named drafts live on the
  phone; *Save draft to server* (`PUT /queue/draft`) and *Load last submitted queue…* (`GET /queue`)
  move the 8 raw fields through the API so you can continue on another device. Opening a named
  draft or the server copy first shows a preview (where it came from, how many rows, the first 5)
  with a *Replace queue* button; nothing changes until you confirm.

### Listing from the app

After *Submit*, the **List on eBay** section on the Preview tab has two toggles, **Publish offers**
and **Schedule start time** (remembered between launches), and a button that asks for confirmation
and then calls `POST /queue/listings`. The API starts the `create-listings` Cloud Run job for the
last submitted queue; the job pulls scan numbers from OneDrive and never scrapes prices, so the PC
does not need to be on. The app opens the run screen and polls `GET /queue/listings/<run_id>` every
3 seconds while the job is `requested` / `running`: state badge, progress bar, listed / failed counts
and a per-row list with the listing id or the error. The **Runs** toolbar button shows recent runs
(`GET /queue/listings`). A 409 means the last server save is a draft (submit first) or a run is
already in progress; a 502 means the job could not be started (check the deployment). Start with
**Publish off**: the offers are created unpublished so you can check them in Seller Hub, then run
again with Publish on.

## Tests

Cmd-U in Xcode (or `xcodebuild test -scheme ShoeboxQueue -destination 'platform=iOS Simulator,name=iPhone 15'`).
The `ShoeboxQueueTests` target is pure XCTest with no network: `APIClientTests` use a stub
`URLProtocol` (including the `Fixtures/listing_run_sample.json` and `listing_runs_sample.json` run
records), `QueueViewModelTests` use the `Fixtures/metadata_sample.json` fixture and a temporary
`DraftStore`, `ListingRunViewModelTests` drive the start / poll loop against `FakeAPI` with a short
poll interval, and `CardNumberSortTests` checks the Swift port of `_cardno_sort_key` against the
server order in the fixture.

## Where things live

| What | Where |
|---|---|
| API contract (the only place URLs / headers / multipart are built) | `ShoeboxQueue/Services/APIClient.swift` |
| Wire shapes | `ShoeboxQueue/Models/APIModels.swift`, `Metadata.swift`, `QueueRow.swift` |
| Session-state logic ported from `shoebox/ui/app.py` | `ShoeboxQueue/ViewModels/QueueViewModel.swift` |
| Listing runs (start, 3 s polling, history) | `ShoeboxQueue/ViewModels/ListingRunViewModel.swift`, `Views/ListingRunView.swift` |
| On-device data | `Application Support/ShoeboxQueue/` : `current.json`, `drafts/<uuid>.json`, `images/<uuid>.jpg`, `metadata.json`, `metadata.meta.json` |
| API key | Keychain generic password, service `com.example.ShoeboxQueue`, account `api-key` |
| Base URL | `UserDefaults` key `baseURL` |
| Listing toggles | `UserDefaults` keys `listingPublish`, `listingSchedule` (default on / on) |

The Python side of the contract is `shoebox/api/schemas.py`; the plan section "The API contract"
in the repo docs (`docs/api.md`) is the human-readable version. Keep the three in sync.

## Troubleshooting

- **"API key rejected (401)"** – the key in Settings does not match `SHOEBOX_API_KEY` on the server.
- **"Not found (404) — check the base URL"** – the base URL points at something that is not the API
  (a trailing path, the wrong port, or the Cloud Run URL of a job instead of the service).
- **Cannot connect from the simulator to localhost** – make sure `shoebox api` binds `0.0.0.0` or
  `127.0.0.1` on port 8080 and that the base URL is `http://localhost:8080` (no `https`).
- **Cannot connect from a physical phone to your Mac** – use your Mac's LAN IP over `http` only if
  the host ends in `.local` (ATS local-networking exception), otherwise deploy the service.
- **Photo upload fails with 413 / 415** – the API caps uploads at 10 MiB and accepts JPEG / PNG only;
  the app always sends a downscaled JPEG, so this usually means a proxy in between.
- **Metadata refresh fails with 503** – BigQuery is unreachable from the service; the app keeps the
  cached copy and shows the stale banner.
- **List on eBay fails with 409 "submit the queue first"** – the last thing saved on the server is a
  draft (or nothing); press *Submit queue* first. **409 "a run is in progress"** – wait for it (the
  *Runs* screen shows it) or for it to go stale. **502** – the service could not start the Cloud Run
  job; see the "Listing from the app" section of `docs/deployment.md`.
- **Build errors after pulling** – run `xcodegen generate` again; new files are not in the project
  until you do.
- **`plutil -p ShoeboxQueue/Info.plist`** should show `NSAllowsLocalNetworking` and *no*
  `NSAllowsArbitraryLoads`.
