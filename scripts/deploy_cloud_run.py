"""Deploy the shoebox Cloud Run jobs and their Cloud Scheduler schedules.

The cloud counterpart of ``generate_tasks.py``: ``deploy/jobs.yaml`` declares
one entry per pipeline (command, cron, resources) plus a ``defaults:`` block,
and this script turns each entry into ``gcloud`` commands and runs them.

    python scripts/deploy_cloud_run.py --list
    python scripts/deploy_cloud_run.py --dry-run          # print the commands, run nothing
    python scripts/deploy_cloud_run.py                    # jobs + IAM + schedules
    python scripts/deploy_cloud_run.py --only sync-orders
    python scripts/deploy_cloud_run.py --jobs-only --image REGION-docker.pkg.dev/PROJECT/shoebox/shoebox:TAG

Everything is create-or-update, so re-running is safe. Per job, in order:

1. ``gcloud run jobs deploy`` -- the job itself (image, args, env, secret
   volumes, resources, no retries).
2. ``gcloud run jobs add-iam-policy-binding`` -- lets the scheduler identity
   start it (``roles/run.invoker``).
3. ``gcloud scheduler jobs create|update http`` -- the schedule, calling the
   Cloud Run ``:run`` endpoint with an OAuth token for that identity.
4. ``gcloud scheduler jobs pause|resume`` -- only when ``enabled:`` disagrees
   with the schedule's current state.

``--jobs-only`` stops after step 1; it is what Cloud Build runs to roll a new
image out. ``--scheduler-only`` runs steps 2-4 without touching images.

``--dry-run`` prints one ``shlex``-quoted line per command, so the output is a
shell script you can read or pipe to ``bash``. Nothing is ever deleted: a job
removed or renamed here lingers in GCP until you delete it by hand.
"""

from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / "deploy" / "jobs.yaml"

# Cloud Run job names: lowercase letters, digits and hyphens, starting with a
# letter, at most 49 characters. Cloud Scheduler is more permissive, so the
# stricter rule is the one that matters.
JOB_NAME_RE = re.compile(r"^[a-z]([a-z0-9-]{0,47}[a-z0-9])?$")
CRON_FIELDS = 5
MANUAL = "manual"

# Cloud Run Admin API endpoint Cloud Scheduler calls to start one execution.
RUN_URI = "https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{name}:run"


class ConfigError(Exception):
    """Raised when jobs.yaml cannot be turned into a valid deployment."""


@dataclass(frozen=True)
class Command:
    """One gcloud invocation: what it does, and the argv to run it."""

    description: str
    argv: list[str]

    def shell(self) -> str:
        return shlex.join(self.argv)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults") or {}
    jobs = data.get("jobs") or []
    if not jobs:
        raise ConfigError(f"no jobs defined in {path}")

    names = [job.get("name") for job in jobs]
    if any(not name for name in names):
        raise ConfigError("every job needs a `name`")
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"duplicate job names: {', '.join(sorted(dupes))}")

    for job in jobs:
        validate_job(job)
    return defaults, jobs


def validate_job(job: dict[str, Any]) -> None:
    name = job["name"]
    if not JOB_NAME_RE.match(name):
        raise ConfigError(
            f"{name}: Cloud Run job names must be lowercase letters, digits and hyphens, "
            "start with a letter, and be at most 49 characters"
        )
    if not job.get("run"):
        raise ConfigError(f"{name}: needs a `run` (the shoebox subcommand with any flags)")
    schedule = job.get("schedule", MANUAL)
    if schedule != MANUAL and len(str(schedule).split()) != CRON_FIELDS:
        raise ConfigError(
            f"{name}: `schedule` must be a 5-field cron expression or {MANUAL!r}, got {schedule!r}"
        )


def setting(job: dict[str, Any], defaults: dict[str, Any], key: str, fallback: Any = None) -> Any:
    """A per-job value, falling back to `defaults:` and then to `fallback`."""
    return job.get(key, defaults.get(key, fallback))


def expand(value: Any, **variables: str) -> Any:
    """Fill ``{project}`` / ``{region}`` placeholders throughout a value.

    Works recursively over dicts and lists; non-strings pass through. An
    unknown placeholder is a config error rather than a silently literal brace.
    """
    if isinstance(value, str):
        try:
            return value.format_map(variables)
        except (KeyError, IndexError) as exc:
            raise ConfigError(f"unknown placeholder {exc} in {value!r}") from None
    if isinstance(value, dict):
        return {k: expand(v, **variables) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, **variables) for v in value]
    return value


def job_args(job: dict[str, Any]) -> list[str]:
    """The container args: a list is passed verbatim, a string is split like a shell would."""
    run = job["run"]
    if isinstance(run, str):
        return shlex.split(run)
    return [str(part) for part in run]


def env_for(job: dict[str, Any], defaults: dict[str, Any]) -> dict[str, str]:
    merged = {**(defaults.get("env") or {}), **(job.get("env") or {})}
    return {str(k): str(v) for k, v in merged.items()}


def secrets_for(job: dict[str, Any], defaults: dict[str, Any]) -> dict[str, str]:
    """Container file path -> ``secret:version``, one secret per directory.

    Cloud Run mounts a secret as a directory volume and refuses two volumes at
    one mount path, so two secrets under the same directory would fail at
    deploy time. Catch it here, where the message can say why.
    """
    merged = {**(defaults.get("secrets") or {}), **(job.get("secrets") or {})}
    secrets = {str(path): str(ref) for path, ref in merged.items()}

    by_dir: dict[str, str] = {}
    for path in secrets:
        if not path.startswith("/"):
            raise ConfigError(f"{job['name']}: secret path must be absolute: {path}")
        directory = str(PurePosixPath(path).parent)
        if directory in by_dir:
            raise ConfigError(
                f"{job['name']}: secrets {by_dir[directory]} and {path} share the directory "
                f"{directory}; Cloud Run mounts one secret per directory"
            )
        by_dir[directory] = path
    return secrets


def resolve_project(defaults: dict[str, Any], cli_project: str | None, gcloud: str) -> str:
    """jobs.yaml `project` > --project > the active gcloud config."""
    if defaults.get("project"):
        return str(defaults["project"])
    if cli_project:
        return cli_project
    result = subprocess.run(
        [gcloud, "config", "get-value", "project"],
        capture_output=True,
        text=True,
        check=False,
    )
    project = result.stdout.strip()
    if result.returncode != 0 or not project or project == "(unset)":
        raise ConfigError(
            "no GCP project: set `project:` in jobs.yaml, pass --project, "
            "or run `gcloud config set project PROJECT`"
        )
    return project


# ---------------------------------------------------------------------------
# Renderers (pure: argv in, argv out)
# ---------------------------------------------------------------------------


def gcloud_list(values: list[str]) -> str:
    """Render a gcloud list flag value.

    gcloud splits on commas by default; a value containing a comma switches to
    its custom-delimiter syntax (``^|^a|b``) so nothing is split by accident.
    """
    if any("," in v for v in values):
        if any("|" in v for v in values):
            raise ConfigError(f"cannot render a gcloud list containing both ',' and '|': {values}")
        return "^|^" + "|".join(values)
    return ",".join(values)


def gcloud_map(mapping: dict[str, str]) -> str:
    return gcloud_list([f"{k}={v}" for k, v in sorted(mapping.items())])


def _common(job: dict[str, Any], defaults: dict[str, Any], project: str) -> dict[str, str]:
    return {
        "project": project,
        "region": str(setting(job, defaults, "region", "us-central1")),
    }


def render_job_deploy(
    job: dict[str, Any],
    defaults: dict[str, Any],
    *,
    project: str,
    image: str | None,
    gcloud: str,
) -> Command:
    """``gcloud run jobs deploy``: creates the job or updates it in place."""
    where = _common(job, defaults, project)
    variables = {"project": project, "region": where["region"]}
    name = job["name"]

    resolved_image = image or expand(setting(job, defaults, "image"), **variables)
    if not resolved_image:
        raise ConfigError(f"{name}: no image; set `image:` in jobs.yaml or pass --image")
    runner = expand(setting(job, defaults, "runner_service_account"), **variables)
    if not runner:
        raise ConfigError(f"{name}: `runner_service_account` is required")

    argv = [
        gcloud,
        "run",
        "jobs",
        "deploy",
        name,
        "--project",
        project,
        "--region",
        where["region"],
        "--quiet",
        "--image",
        resolved_image,
        "--args",
        gcloud_list(job_args(job)),
        "--service-account",
        runner,
        "--cpu",
        str(setting(job, defaults, "cpu", "1")),
        "--memory",
        str(setting(job, defaults, "memory", "1Gi")),
        "--task-timeout",
        str(setting(job, defaults, "timeout", "1800s")),
        "--max-retries",
        str(setting(job, defaults, "max_retries", 0)),
        "--tasks",
        "1",
        "--parallelism",
        "1",
    ]

    env = expand(env_for(job, defaults), **variables)
    if env:
        argv += ["--set-env-vars", gcloud_map(env)]
    secrets = secrets_for(job, defaults)
    if secrets:
        argv += ["--set-secrets", gcloud_map(secrets)]
    labels = setting(job, defaults, "labels") or {}
    if labels:
        argv += ["--labels", gcloud_map({str(k): str(v) for k, v in labels.items()})]

    return Command(f"deploy Cloud Run job {name}", argv)


def render_invoker_binding(
    job: dict[str, Any], defaults: dict[str, Any], *, project: str, gcloud: str
) -> Command:
    """Let the scheduler identity start this job. Re-adding a binding is a no-op."""
    where = _common(job, defaults, project)
    scheduler = expand(setting(job, defaults, "scheduler_service_account"), **where)
    if not scheduler:
        raise ConfigError(f"{job['name']}: `scheduler_service_account` is required")
    argv = [
        gcloud,
        "run",
        "jobs",
        "add-iam-policy-binding",
        job["name"],
        "--project",
        project,
        "--region",
        where["region"],
        "--quiet",
        "--member",
        f"serviceAccount:{scheduler}",
        "--role",
        "roles/run.invoker",
    ]
    return Command(f"allow {scheduler} to run {job['name']}", argv)


def scheduler_uri(project: str, region: str, name: str) -> str:
    return RUN_URI.format(project=project, region=region, name=name)


def render_scheduler_upsert(
    job: dict[str, Any],
    defaults: dict[str, Any],
    *,
    project: str,
    exists: bool,
    gcloud: str,
) -> Command:
    """``gcloud scheduler jobs create|update http``; both take the same flags."""
    where = _common(job, defaults, project)
    name = job["name"]
    scheduler = expand(setting(job, defaults, "scheduler_service_account"), **where)
    if not scheduler:
        raise ConfigError(f"{name}: `scheduler_service_account` is required")

    verb = "update" if exists else "create"
    argv = [
        gcloud,
        "scheduler",
        "jobs",
        verb,
        "http",
        name,
        "--project",
        project,
        "--location",
        where["region"],
        "--quiet",
        "--schedule",
        str(job["schedule"]),
        "--time-zone",
        str(setting(job, defaults, "timezone", "Etc/UTC")),
        "--uri",
        scheduler_uri(project, where["region"], name),
        "--http-method",
        "POST",
        "--oauth-service-account-email",
        scheduler,
        "--attempt-deadline",
        str(setting(job, defaults, "attempt_deadline", "320s")),
        "--max-retry-attempts",
        str(setting(job, defaults, "scheduler_retries", 0)),
    ]
    if description := job.get("description"):
        argv += ["--description", str(description)]
    return Command(f"{verb} schedule for {name} ({job['schedule']})", argv)


def render_scheduler_state(
    job: dict[str, Any],
    defaults: dict[str, Any],
    *,
    project: str,
    current_state: str | None,
    gcloud: str,
) -> Command | None:
    """Pause or resume the schedule only when `enabled:` disagrees with GCP.

    ``current_state`` is what ``gcloud scheduler jobs list`` reported before
    this run (``ENABLED`` / ``PAUSED``). A schedule that did not exist yet is
    created enabled by the preceding ``create``, so callers pass ``ENABLED``
    for it and a job with ``enabled: false`` is paused in the same run.
    """
    enabled = bool(setting(job, defaults, "enabled", True))
    where = _common(job, defaults, project)
    common = [job["name"], "--project", project, "--location", where["region"], "--quiet"]
    if current_state == "ENABLED" and not enabled:
        return Command(f"pause {job['name']}", [gcloud, "scheduler", "jobs", "pause", *common])
    if current_state == "PAUSED" and enabled:
        return Command(f"resume {job['name']}", [gcloud, "scheduler", "jobs", "resume", *common])
    return None


# ---------------------------------------------------------------------------
# Planning and execution
# ---------------------------------------------------------------------------


def select_jobs(jobs: list[dict[str, Any]], only: list[str] | None) -> list[dict[str, Any]]:
    if not only:
        return jobs
    known = {job["name"] for job in jobs}
    if unknown := set(only) - known:
        raise ConfigError(f"unknown job(s): {', '.join(sorted(unknown))}")
    return [job for job in jobs if job["name"] in only]


def list_scheduler_jobs(
    project: str, region: str, gcloud: str, *, dry_run: bool
) -> dict[str, str] | None:
    """Existing Scheduler jobs in the region -> their state.

    One list call up front instead of a describe per job. Returns None in a dry
    run (the command is printed, not executed) so callers can tell "nothing
    exists" from "did not look".
    """
    argv = [
        gcloud,
        "scheduler",
        "jobs",
        "list",
        "--project",
        project,
        "--location",
        region,
        "--format",
        "value(name,state)",
    ]
    print(f"# list existing schedules in {region}")
    print(shlex.join(argv))
    if dry_run:
        return None
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    return parse_scheduler_list(result.stdout)


def parse_scheduler_list(output: str) -> dict[str, str]:
    """``projects/P/locations/R/jobs/NAME<TAB>STATE`` lines -> {NAME: STATE}."""
    states: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        name, _, state = line.partition("\t")
        states[name.rsplit("/", 1)[-1]] = state.strip()
    return states


def plan(
    jobs: list[dict[str, Any]],
    defaults: dict[str, Any],
    *,
    project: str,
    image: str | None,
    mode: str,
    existing: dict[str, str] | None,
    gcloud: str,
) -> list[Command]:
    """Every command to run, in order. ``mode`` is ``all``, ``jobs`` or ``scheduler``."""
    commands: list[Command] = []
    for job in jobs:
        if mode in ("all", "jobs"):
            commands.append(
                render_job_deploy(job, defaults, project=project, image=image, gcloud=gcloud)
            )
        if mode in ("all", "scheduler") and job.get("schedule", MANUAL) != MANUAL:
            commands.append(render_invoker_binding(job, defaults, project=project, gcloud=gcloud))
            known = existing or {}
            exists = job["name"] in known
            commands.append(
                render_scheduler_upsert(
                    job, defaults, project=project, exists=exists, gcloud=gcloud
                )
            )
            # `create` leaves a schedule enabled, so a new one starts from ENABLED.
            state = render_scheduler_state(
                job,
                defaults,
                project=project,
                current_state=known[job["name"]] if exists else "ENABLED",
                gcloud=gcloud,
            )
            if state is not None:
                commands.append(state)
    return commands


def execute(commands: list[Command], *, dry_run: bool) -> None:
    """Print each command as a shell line, then run it unless dry-running.

    The printed form is POSIX shell quoting (``shlex.join``); on Windows it is
    for reading, not pasting into PowerShell.
    """
    for command in commands:
        print(f"# {command.description}")
        print(command.shell())
        if not dry_run:
            subprocess.run(command.argv, check=True)


def list_jobs(jobs: list[dict[str, Any]], defaults: dict[str, Any]) -> None:
    for job in jobs:
        schedule = str(job.get("schedule", MANUAL))
        enabled = "" if setting(job, defaults, "enabled", True) else "(paused)"
        print(
            f"{job['name']:<32} {schedule:<14} {enabled:<8} "
            f"{setting(job, defaults, 'memory', '1Gi'):<6} "
            f"{setting(job, defaults, 'timeout', '1800s'):<7} {shlex.join(job_args(job))}"
        )


def find_gcloud(explicit: str | None) -> str:
    if explicit:
        return explicit
    for candidate in ("gcloud", "gcloud.cmd"):
        if found := shutil.which(candidate):
            return found
    raise ConfigError(
        "`gcloud` is not on PATH; install the Google Cloud SDK "
        "(https://cloud.google.com/sdk/docs/install) or pass --gcloud PATH"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", type=Path, default=CONFIG, help="path to jobs.yaml")
    parser.add_argument("--project", help="GCP project (default: jobs.yaml, else gcloud config)")
    parser.add_argument(
        "--image", help="image for every selected job, overriding jobs.yaml (Cloud Build sets this)"
    )
    parser.add_argument(
        "--only", action="append", metavar="NAME", help="limit to this job (repeatable)"
    )
    parser.add_argument("--list", action="store_true", help="list the configured jobs; do nothing")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the gcloud commands; run nothing"
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--jobs-only",
        action="store_true",
        help="deploy the Cloud Run jobs only (no IAM, no schedules)",
    )
    scope.add_argument(
        "--scheduler-only",
        action="store_true",
        help="IAM bindings and schedules only (leave the jobs' images alone)",
    )
    parser.add_argument("--gcloud", help="path to the gcloud executable (default: search PATH)")
    args = parser.parse_args()

    try:
        defaults, jobs = load_config(args.config)
        jobs = select_jobs(jobs, args.only)
        if args.list:
            list_jobs(jobs, defaults)
            return 0

        # In a dry run gcloud need not be installed; use a placeholder name.
        gcloud = "gcloud" if args.dry_run and not args.gcloud else find_gcloud(args.gcloud)
        if args.dry_run and not args.project and not defaults.get("project"):
            project = "PROJECT"
        else:
            project = resolve_project(defaults, args.project, gcloud)

        mode = "jobs" if args.jobs_only else "scheduler" if args.scheduler_only else "all"
        existing: dict[str, str] | None = None
        if mode != "jobs":
            region = str(defaults.get("region", "us-central1"))
            existing = list_scheduler_jobs(project, region, gcloud, dry_run=args.dry_run)

        commands = plan(
            jobs,
            defaults,
            project=project,
            image=args.image,
            mode=mode,
            existing=existing,
            gcloud=gcloud,
        )
        execute(commands, dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"error: command failed with exit code {exc.returncode}", file=sys.stderr)
        return exc.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
