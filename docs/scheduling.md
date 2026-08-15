# Scheduling

On the Windows host, the recurring pipelines run as Task Scheduler tasks in the
`\shoebox\` folder. Those tasks are **generated from
[`scripts/tasks.yaml`](../scripts/tasks.yaml)** rather than hand-built in the
GUI, so a task's schedule, working directory, and retry policy are reviewable in
the repo instead of living only in the scheduler database.

```
scripts/tasks.yaml          <- edit this
scripts/generate_tasks.py   <- turns it into XML, optionally registers
scripts/tasks/*.xml         <- generated; overwritten every run
```

Tasks invoke the `shoebox` entry point directly — there are no wrapper batch
files. Task Scheduler does not search `PATH`, so the generator resolves
`shoebox` to an absolute path and bakes it into the XML.

## Usage

```bash
python scripts/generate_tasks.py            # regenerate scripts/tasks/*.xml
python scripts/generate_tasks.py --list     # show configured tasks and triggers
python scripts/generate_tasks.py --register # regenerate, then register with Task Scheduler
python scripts/generate_tasks.py --only watch-searches --register
python scripts/generate_tasks.py --register --password-stdin   # password from stdin
```

Generating is safe and offline — it only writes XML. `--register` hands each
file to `Register-ScheduledTask`, replacing the existing task of the same name.

**Registering needs admin, but you do not have to arrange it.** Run `--register`
from an ordinary shell and it reopens itself in an elevated console via UAC,
forwarding whatever arguments you gave it. Accept the prompt and the rest of the
run — including the password prompt — happens in that new window, which stays
open so you can read the output. `--no-elevate` disables this and fails with an
explanation instead.

Two reasons admin is required:

- The tasks run at `RunLevel: HighestAvailable`. Registering that without admin
  fails with a bare `Access is denied` (`0x80070005`). A rejected password looks
  different (`0x8007052E`), so the two are easy to tell apart.
- They also run whether or not you are logged on (`LogonType: Password`), which
  stores a credential. `--register` asks for the account password once and
  reuses it for every task, prompting via `getpass`. Nothing is echoed — not
  even asterisks — so pasting looks like it did nothing, but it works.

To skip the prompt entirely, pipe the password in. This needs a shell that is
*already* elevated, because the elevated window gets a fresh console with no
stdin to pipe into. `Get-Credential` opens a dialog, so a password manager can
fill it:

```powershell
(Get-Credential -UserName $env:USERNAME -Message 'Task account').GetNetworkCredential().Password |
  python scripts/generate_tasks.py --register --password-stdin
```

Do not put the password in the command line itself (`'secret' | python ...`) —
PowerShell writes command history to `ConsoleHost_history.txt` in plain text.

The password is never written to disk and is passed to `Register-ScheduledTask`
through the child process's environment rather than as a command-line argument,
where it would be readable from any process listing.

## The working-directory rule

Every task must run with the repo root as its working directory. `shoebox`
resolves config paths and its log directory relative to the current directory,
so a task without `<WorkingDirectory>` inherits `C:\Windows\System32` and dies
before doing any work:

```
PermissionError: [Errno 13] Permission denied: 'C:\Windows\System32\logs\....log'
```

The generator always emits `<WorkingDirectory>`, which is the main reason to
prefer it over the GUI — this is easy to forget in *Start in* and produces a
task that fails on every run with a bare exit code 1.

## Task definitions

`defaults:` supplies every setting; each entry under `tasks:` overrides what it
needs. A task is a name, the command it runs, and a trigger:

```yaml
- name: sync-orders
  run: sync-orders                   # the shoebox subcommand, with any flags
  description: Sync orders into GCS/BigQuery.
  trigger:
    type: daily
    at: "04:00"
```

`run:` may be a list, which becomes one `<Exec>` action per entry, executed in
order:

```yaml
run:
  - orders-awaiting-shipment --pull-order --message
  - orders-awaiting-shipment --buyer-order --message
```

Task Scheduler does not stop at the first failing step, so use a list for
independent commands rather than a pipeline that depends on the previous step.

**Trigger types**

| `type` | Fires | Extra keys |
|---|---|---|
| `daily` | Every N days at a wall-clock time | `at` (required, `HH:MM`), `every_days`, `start_date` |
| `logon` | When the user logs on | `user` |
| `startup` | At boot | — |
| `manual` | Never automatically; run on demand | — |

`logon`, `startup`, and `daily` all accept `repeat_every` (an ISO 8601 duration
such as `PT5M`) to re-fire on an interval after the initial trigger, plus an
optional `repeat_for` window — omitted means repeat indefinitely.
`execution_time_limit` on the trigger caps a single run.

**Per-task keys beyond the defaults**

- `executable:` — the entry point `run:` commands are passed to. Defaults to
  whichever `shoebox` is on `PATH` when the generator runs; set it to
  `.venv\Scripts\shoebox.exe` to pin the tasks to the project virtualenv. A
  relative path is resolved against the repo root.
- `command:` / `arguments:` — an escape hatch for running something that is not
  a shoebox subcommand. `command:` is an absolute path and is mutually
  exclusive with `run:`.
- `logon_type:` — `password` (default, runs whether or not you are logged on),
  `interactive` (runs only while logged on, and registers without a password),
  or `s4u`.
- `restart_on_failure:` — `{count, interval}`, retried when the task exits
  nonzero. Used by `slack-bot`.
- `start_when_available: true` — run a missed schedule late rather than skipping
  it.
- Any `defaults:` key can be repeated on a task to override it.

The account is not stored in the YAML; the generator writes
`%USERDOMAIN%\%USERNAME%` into the XML, which Windows resolves to a SID on
registration.

## Verifying

```powershell
Get-ScheduledTaskInfo -TaskName watch-searches -TaskPath \shoebox\
```

`LastTaskResult` is the command's exit code — `0` is success, and `1` is usually
shoebox raising before it got anywhere. The task's own stderr is not captured
anywhere, so reproduce a failure by hand to see the traceback:

```powershell
shoebox watch-searches
```

Run it from `C:\Windows\System32` rather than the repo to reproduce a
working-directory problem specifically. Per-run logs land in `logs/` under the
repo root; see [data-storage.md](data-storage.md).

## Current tasks

| Task | Trigger | Runs |
|---|---|---|
| `sync-active-listings` | Daily 03:00 | `shoebox sync-active-listings` |
| `sync-active-listing-details` | Daily 03:30 | `shoebox sync-active-listing-details` |
| `sync-orders` | Daily 04:00 | `shoebox sync-orders` |
| `end_oos_listings` | Daily 05:00 | `shoebox end-oos-listings` |
| `watch-searches` | Logon, every 5m | `shoebox watch-searches` |
| `slack-bot` | Logon (restart ×3) | `shoebox slack-bot` |
| `ebay-orders-awaiting-shipment` | Manual | `shoebox orders-awaiting-shipment` ×2 |

`watch-searches` and `slack-bot` are the two that stay resident. Overlapping
`watch-searches` runs are already prevented by an advisory lock inside the
command — see [search.md](search.md#scheduling) — with the scheduler's
`MultipleInstancesPolicy: IgnoreNew` as a second line of defence.
