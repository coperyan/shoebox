# `tools/` — authoring workbooks

The metadata sync (`shoebox sync-metadata`) reads a master Excel
workbook, `checklist_parallel_metadata.xlsm`, with two sheets:

| Sheet | Columns | Loaded into |
|---|---|---|
| `Checklist` | Record ID, Set Year, Subset ID, Set Name, Subset Name, Subset Type, Derived Card Number, Card Number, Player, Team, Note | BigQuery `checklist` table |
| `Parallels` | Parallel ID, Subset ID, Set Name, Subset Name, Parallel/Variety, Print Run, Other Note | BigQuery `parallels` table |

Only the columns present in `configs/bigquery/schemas/{checklist,parallels}.json`
are kept during the load; the remaining workbook columns (IDs, Set Year,
Derived Card Number) are surfaced downstream by your BigQuery views. For a
field-by-field definition of every column, see
[docs/metadata.md](../docs/metadata.md).

## Getting started

The real workbook is **not** tracked in git (it can be large and is
store-specific). A small, generic template is provided instead:

```bash
cp tools/checklist_parallel_metadata.sample.xlsx tools/checklist_parallel_metadata.xlsm
```

Then edit it with your own sets/checklists and run `shoebox sync-metadata`.
`.xlsm` (macro-enabled) and `.xlsx` are both read via `openpyxl`; the real
workbook is macro-enabled because it also feeds the listing-input workbook's
dropdowns through Power Query, but plain `.xlsx` works for the sync itself.

> `tools/checklist_parallel_metadata.xlsm` and `tools/inputs.xlsm` are
> gitignored — keep your real data local. Only the `*.sample.xlsx` template is
> tracked.
