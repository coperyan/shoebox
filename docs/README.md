# Documentation

Reference documentation for **shoebox** — an automation toolkit for running an
eBay sports-card store: metadata management in BigQuery, listing creation via the
eBay Sell APIs, image handling in GCS, order/traffic monitoring, and a Slack layer
for notifications, price approvals, and remote command execution.

| Document | Contents |
|---|---|
| [overview.md](overview.md) | What the project does, architecture, directory layout, end-to-end data flow |
| [setup.md](setup.md) | Requirements, installation, all credential/config files, Slack app setup, GCP + eBay onboarding |
| [configuration.md](configuration.md) | Full `configs/app.yaml` settings reference and environment variables |
| [cli.md](cli.md) | Every `shoebox` CLI command, its flags, and what it runs |
| [pipelines.md](pipelines.md) | Detailed walkthrough of each pipeline and service |
| [search.md](search.md) | The saved eBay search feature end to end: setup, YAML, filters, tuning, scheduling, state, troubleshooting |
| [metadata.md](metadata.md) | Field-by-field definitions of the checklist & parallel metadata |
| [slack.md](slack.md) | The Slack notification system: messaging, approval buttons, threading, the command bot, and design decisions |
| [data-storage.md](data-storage.md) | GCS buckets, BigQuery datasets/tables/views, local JSONL files, and how data moves between them |

## Quick orientation

- **Install**: `pip install -e .` (Python ≥ 3.11), then create the four config files
  from the templates in `configs/*.example` — see [setup.md](setup.md).
- **Run something**: the console script is `shoebox` (equivalently
  `python -m shoebox.cli`). `shoebox --help` lists all commands —
  see [cli.md](cli.md).
- **Day-to-day flow**: build a listing queue in the Streamlit UI (`shoebox ui`)
  or from Excel (`create-queue-excel`), then publish with `create-listings`.
  Monitoring pipelines (`sync-orders`, `sync-active-listings`, …) snapshot eBay
  state into BigQuery. Slack receives status notifications and hosts the
  interactive price-approval flow.
