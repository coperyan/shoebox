# Checklist & Parallel Metadata Reference

The catalog of cards you sell is described by two datasets — **checklist**
(every card in a set) and **parallels** (the colored/numbered variations of
those cards). This document defines every field.

## How the metadata flows

```
tools/checklist_parallel_metadata.xlsm   (you author this; gitignored)
        │  sync-metadata reads the Checklist + Parallels sheets
        ▼
data/checklist.csv, data/parallels.csv   (normalized extracts)
        │  load schemas keep only the base columns
        ▼
BigQuery load tables  (<checklist_dataset>.checklist / .parallels)
        │  your views enrich + join
        ▼
cards.v_checklist, cards.v_parallels      (BigQuery VIEWS — see note below)
        │  checklist_queue.sql / parallels_queue.sql / card_metadata.sql
        ▼
Streamlit UI dropdowns + queue enrichment (build_enriched_json)
```

> **Views are external to this repo.** `cards.v_checklist` and
> `cards.v_parallels` (and the `cards` dataset) are **not** committed — they are
> your own BigQuery views that compute the derived/helper fields
> (`card_id`, `subset_id`, `derived_card_number`, `note_check`, `parallel_id`)
> on top of the loaded base tables. The `sync-metadata` load schemas
> (`configs/bigquery/schemas/checklist.json` / `parallels.json`) deliberately
> keep only the base columns; everything else is produced downstream in the
> views. If you are standing this up fresh, create those two views to expose the
> columns that `configs/bigquery/queries/*.sql` select.

## Checklist fields

One row per card in a set. Authored in the `Checklist` sheet of the master
workbook.

| Field | In workbook | Loaded to BQ | Meaning |
|---|---|---|---|
| **Set Name** | ✅ | ✅ | Full set name, e.g. `2023 Topps Chrome`. Joins checklist ↔ parallels. |
| **Set Year** | ✅ | via view | Release year, e.g. `2023`. Feeds the `Season` / `Year Manufactured` eBay aspects. |
| **Subset Name** | ✅ | ✅ | The subset/insert this card belongs to, e.g. `Base`, `Future Stars`. |
| **Subset Type** | ✅ | ✅ | `Base` or `Insert`. Drives the `Insert Set` aspect, store category, and title formatting. |
| **Card Number** | ✅ | ✅ | Card number exactly as printed (string — supports short-print / prefixed numbers like `BCP-15`). |
| **Derived Card Number** | ✅ | via view | Card Number with any alphanumeric prefix stripped so the dropdown sorts numerically — e.g. `91AS-123` → `123`, `USC-12` → `12`. Sort/ordering only; never shown to buyers. |
| **Player** | ✅ | ✅ | Player name. Multiple players are separated by ` / ` (e.g. `Aaron Judge / Juan Soto`) and split into repeated `Player/Athlete` aspects. |
| **Team** | ✅ | ✅ (nullable) | Team name; also ` / `-splittable. Optional. |
| **Note** | ✅ | ✅ (nullable) | Free-text note **specific to this row**, e.g. `RC`, `SP`. Scanned for `RC` (rookie) and `SP` (short print) flags. |
| **note_check** | — | view-derived | Rookie/attribute inheritance from the **base** card: looks up the same player in the set's base checklist and picks up flags (e.g. `RC`) that inserts usually omit. Example: a 2024 Topps rookie appears in several insert sets that don't label him a rookie, so rookie status is inferred from his base card via player match. |
| **Record ID** | ✅ | via view | Helper ID for lookups/sorting; not buyer-facing. |
| **Subset ID** | ✅ | via view | Helper ID identifying a subset; also the grouping key for multi-variation ("You Pick") listings. |
| `card_id` | — | view-derived | Stable card key used to join enriched queue rows back to the checklist. |

## Parallels fields

One row per parallel/variety available for a subset. Authored in the
`Parallels` sheet.

| Field | In workbook | Loaded to BQ | Meaning |
|---|---|---|---|
| **Set Name** | ✅ | ✅ | Matches the checklist Set Name (join key). |
| **Subset Name** | ✅ | ✅ | Matches the checklist Subset Name (join key). |
| **Parallel/Variety** | ✅ | ✅ | The parallel name, e.g. `Gold Refractor`, `Orange /25`. Becomes the `Parallel/Variety` aspect and part of the title. |
| **Print Run** | ✅ | ✅ (nullable) | Serial-number print run as an integer, e.g. `99` for a `/99`. Drives the `Serial Numbered` feature and the `/NN` title suffix. |
| **Other Note** | ✅ | ✅ (nullable) | Parallel-level note, e.g. `MEM` (memorabilia/relic), `SP`. Scanned for those flags when building aspects/titles. |
| **Parallel ID** | ✅ | via view | Helper ID for lookups/sorting; not buyer-facing. |
| **Subset ID** | ✅ | via view | Links the parallel to its subset (helper/lookup). |

## Notes

- "In workbook" = you maintain it in `tools/checklist_parallel_metadata.xlsm`.
  "Loaded to BQ" = kept by the `sync-metadata` load schema. "via view" /
  "view-derived" = surfaced by your `cards.v_*` views, not the load tables.
- The parallels join is intentionally loose (Set + Subset + Parallel/Variety),
  so a single parallel row applies to every card in that subset.
- See [tools/README.md](../tools/README.md) for the exact sheet/column names the
  sync expects and how to start from the shipped sample workbook.
