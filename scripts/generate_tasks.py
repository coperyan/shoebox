"""Generate (and optionally register) Windows scheduled tasks from tasks.yaml.

The Task Scheduler XML in ``scripts/tasks/`` is generated output -- edit
``scripts/tasks.yaml`` instead and re-run this script.

    python scripts/generate_tasks.py                 # write XML only
    python scripts/generate_tasks.py --register      # write XML, then register
    python scripts/generate_tasks.py --only slack-bot --register

Registering needs admin, because the tasks run at ``HighestAvailable``. Run
``--register`` from anywhere and it reopens itself in an elevated console via
UAC; ``--no-elevate`` turns that off.

The tasks store a credential ("run whether user is logged on or not"), so
``--register`` asks for the account password once and reuses it for every task.
It is passed to Task Scheduler through the environment rather than a command
line, and is never written to disk. ``--password-stdin`` reads it from stdin
instead of prompting, and requires an already-elevated shell.
"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / "scripts" / "tasks.yaml"
OUTPUT_DIR = REPO_ROOT / "scripts" / "tasks"

NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"

# Set on the elevated rerun so it never tries to elevate a second time.
ELEVATION_MARKER = "SHOEBOX_TASKS_ELEVATED"

RUN_LEVELS = {"highest": "HighestAvailable", "limited": "LeastPrivilege"}
LOGON_TYPES = {
    "password": "Password",
    "interactive": "InteractiveToken",
    "s4u": "S4U",
    "service": "Password",
}

# Element order matters less to Task Scheduler than the docs suggest, but this
# mirrors what the Task Scheduler UI itself exports, which is known to register.
SETTINGS_ORDER = [
    "MultipleInstancesPolicy",
    "DisallowStartIfOnBatteries",
    "StopIfGoingOnBatteries",
    "AllowHardTerminate",
    "StartWhenAvailable",
    "RunOnlyIfNetworkAvailable",
    "IdleSettings",
    "AllowStartOnDemand",
    "Enabled",
    "Hidden",
    "RunOnlyIfIdle",
    "DisallowStartOnRemoteAppSession",
    "UseUnifiedSchedulingEngine",
    "WakeToRun",
    "ExecutionTimeLimit",
    "Priority",
]


class ConfigError(Exception):
    """Raised when tasks.yaml cannot be turned into a valid task."""


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _indent(lines: list[str], level: int) -> list[str]:
    pad = "  " * level
    return [pad + line for line in lines]


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME", "")
    user = os.environ.get("USERNAME", "")
    if not user:
        raise ConfigError("USERNAME is not set; pass `user:` in tasks.yaml instead")
    return f"{domain}\\{user}" if domain else user


def load_config(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults") or {}
    tasks = data.get("tasks") or []
    if not tasks:
        raise ConfigError(f"no tasks defined in {path}")

    names = [t.get("name") for t in tasks]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"duplicate task names: {', '.join(sorted(dupes))}")
    return defaults, tasks


def resolve_executable(task: dict[str, Any], defaults: dict[str, Any]) -> str:
    """Return the absolute path of the entry point `run:` commands are passed to.

    Task Scheduler does not search PATH, so the executable has to be baked into
    the XML as an absolute path.
    """
    configured = task.get("executable", defaults.get("executable"))
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = (REPO_ROOT / path).resolve()
        if not path.exists():
            raise ConfigError(f"executable not found: {path}")
        return str(path)

    found = shutil.which("shoebox")
    if not found:
        raise ConfigError(
            "`shoebox` is not on PATH; set `executable:` in tasks.yaml "
            r"(e.g. .venv\Scripts\shoebox.exe)"
        )
    # which() reports the extension in PATHEXT's casing (".EXE"); normalise it
    # so the generated XML does not depend on the shell that ran the generator.
    path = Path(found)
    return str(path.with_name(path.stem + path.suffix.lower()))


def build_actions(task: dict[str, Any], defaults: dict[str, Any], working_dir: Path) -> list[str]:
    """Build one <Exec> per command; Task Scheduler runs them in order."""
    if task.get("run") and task.get("command"):
        raise ConfigError(f"{task['name']}: set either `run` or `command`, not both")

    steps: list[tuple[str, str]] = []

    if command := task.get("command"):
        # Escape hatch for anything that is not a shoebox subcommand.
        steps.append((command, task.get("arguments", "")))
    else:
        run = task.get("run")
        if not run:
            raise ConfigError(f"{task['name']}: needs a `run` or `command`")
        if isinstance(run, str):
            run = [run]
        executable = resolve_executable(task, defaults)
        steps = [(executable, str(step)) for step in run]

    lines = ['<Actions Context="Author">']
    for command, arguments in steps:
        lines.append("  <Exec>")
        lines.append(f"    <Command>{escape(command)}</Command>")
        if arguments:
            lines.append(f"    <Arguments>{escape(arguments)}</Arguments>")
        lines.append(f"    <WorkingDirectory>{escape(str(working_dir))}</WorkingDirectory>")
        lines.append("  </Exec>")
    lines.append("</Actions>")
    return lines


def build_triggers(task: dict[str, Any], defaults: dict[str, Any], user: str) -> list[str]:
    trigger = task.get("trigger") or {"type": "manual"}
    kind = trigger.get("type", "manual")

    if kind == "manual":
        return ["<Triggers />"]

    body: list[str] = []

    if kind == "daily":
        at = trigger.get("at")
        if not at:
            raise ConfigError(f"{task['name']}: a daily trigger needs `at` (HH:MM)")
        start_date = trigger.get("start_date", defaults.get("start_date"))
        if not start_date:
            raise ConfigError(f"{task['name']}: no `start_date` for the daily trigger")
        inner = [f"<StartBoundary>{start_date}T{at}:00</StartBoundary>", "<Enabled>true</Enabled>"]
        inner += _repetition(trigger)
        if limit := trigger.get("execution_time_limit"):
            inner.append(f"<ExecutionTimeLimit>{limit}</ExecutionTimeLimit>")
        inner += [
            "<ScheduleByDay>",
            f"  <DaysInterval>{trigger.get('every_days', 1)}</DaysInterval>",
            "</ScheduleByDay>",
        ]
        body = ["<CalendarTrigger>", *_indent(inner, 1), "</CalendarTrigger>"]

    elif kind == "logon":
        inner = _repetition(trigger)
        if limit := trigger.get("execution_time_limit"):
            inner.append(f"<ExecutionTimeLimit>{limit}</ExecutionTimeLimit>")
        inner.append("<Enabled>true</Enabled>")
        inner.append(f"<UserId>{escape(trigger.get('user', user))}</UserId>")
        body = ["<LogonTrigger>", *_indent(inner, 1), "</LogonTrigger>"]

    elif kind == "startup":
        inner = _repetition(trigger)
        if limit := trigger.get("execution_time_limit"):
            inner.append(f"<ExecutionTimeLimit>{limit}</ExecutionTimeLimit>")
        inner.append("<Enabled>true</Enabled>")
        body = ["<BootTrigger>", *_indent(inner, 1), "</BootTrigger>"]

    else:
        raise ConfigError(
            f"{task['name']}: unknown trigger type {kind!r} "
            "(expected daily, logon, startup or manual)"
        )

    return ["<Triggers>", *_indent(body, 1), "</Triggers>"]


def _repetition(trigger: dict[str, Any]) -> list[str]:
    interval = trigger.get("repeat_every")
    if not interval:
        return []
    inner = [f"<Interval>{interval}</Interval>"]
    # An omitted Duration means "repeat indefinitely", which is what the
    # every-5-minutes watchers want.
    if duration := trigger.get("repeat_for"):
        inner.append(f"<Duration>{duration}</Duration>")
    inner.append(
        f"<StopAtDurationEnd>{_bool(trigger.get('stop_at_duration_end', False))}</StopAtDurationEnd>"
    )
    return ["<Repetition>", *_indent(inner, 1), "</Repetition>"]


def build_settings(task: dict[str, Any], defaults: dict[str, Any]) -> list[str]:
    def setting(key: str, fallback: Any = None) -> Any:
        return task.get(key, defaults.get(key, fallback))

    values: dict[str, str] = {
        "MultipleInstancesPolicy": str(setting("multiple_instances", "IgnoreNew")),
        "DisallowStartIfOnBatteries": _bool(setting("disallow_start_if_on_batteries", True)),
        "StopIfGoingOnBatteries": _bool(setting("stop_if_going_on_batteries", True)),
        "AllowHardTerminate": _bool(setting("allow_hard_terminate", True)),
        "StartWhenAvailable": _bool(setting("start_when_available", False)),
        "RunOnlyIfNetworkAvailable": _bool(setting("run_only_if_network_available", False)),
        "AllowStartOnDemand": _bool(setting("allow_start_on_demand", True)),
        "Enabled": _bool(setting("enabled", True)),
        "Hidden": _bool(setting("hidden", False)),
        "RunOnlyIfIdle": _bool(setting("run_only_if_idle", False)),
        "DisallowStartOnRemoteAppSession": _bool(
            setting("disallow_start_on_remote_app_session", False)
        ),
        "UseUnifiedSchedulingEngine": _bool(setting("use_unified_scheduling_engine", True)),
        "WakeToRun": _bool(setting("wake_to_run", True)),
        "ExecutionTimeLimit": str(setting("execution_time_limit", "PT72H")),
        "Priority": str(setting("priority", 7)),
    }

    lines: list[str] = ["<Settings>"]
    for name in SETTINGS_ORDER:
        if name == "IdleSettings":
            lines += _indent(
                [
                    "<IdleSettings>",
                    f"  <StopOnIdleEnd>{_bool(setting('stop_on_idle_end', True))}</StopOnIdleEnd>",
                    f"  <RestartOnIdle>{_bool(setting('restart_on_idle', False))}</RestartOnIdle>",
                    "</IdleSettings>",
                ],
                1,
            )
            continue
        lines.append(f"  <{name}>{values[name]}</{name}>")

    if restart := setting("restart_on_failure"):
        # Interval-then-Count, appended last: the order and position the Task
        # Scheduler XSD prescribes and the GUI's own XML exports use.
        lines += _indent(
            [
                "<RestartOnFailure>",
                f"  <Interval>{restart['interval']}</Interval>",
                f"  <Count>{restart['count']}</Count>",
                "</RestartOnFailure>",
            ],
            1,
        )

    lines.append("</Settings>")
    return lines


def build_xml(task: dict[str, Any], defaults: dict[str, Any], user: str) -> str:
    name = task["name"]
    folder = task.get("folder", defaults.get("folder", "\\")).rstrip("\\")
    working_dir = (REPO_ROOT / task.get("working_dir", defaults.get("working_dir", "."))).resolve()

    registration = [
        "<RegistrationInfo>",
        f"  <Author>{escape(user)}</Author>",
        f"  <URI>{escape(folder)}\\{escape(name)}</URI>",
    ]
    if description := task.get("description"):
        registration.insert(1, f"  <Description>{escape(description)}</Description>")
    registration.append("</RegistrationInfo>")

    principals = [
        "<Principals>",
        '  <Principal id="Author">',
        f"    <UserId>{escape(task.get('user', user))}</UserId>",
        f"    <LogonType>{LOGON_TYPES[task.get('logon_type', defaults.get('logon_type', 'password'))]}</LogonType>",
        f"    <RunLevel>{RUN_LEVELS[task.get('run_level', defaults.get('run_level', 'highest'))]}</RunLevel>",
        "  </Principal>",
        "</Principals>",
    ]

    body = [
        *registration,
        *build_triggers(task, defaults, user),
        *principals,
        *build_settings(task, defaults),
        *build_actions(task, defaults, working_dir),
    ]

    lines = [
        '<?xml version="1.0" encoding="UTF-16"?>',
        f'<Task version="1.4" xmlns="{NS}">',
        *_indent(body, 1),
        "</Task>",
    ]
    return "\r\n".join(lines) + "\r\n"


def _display_path(path: Path) -> str:
    """Repo-relative when possible; --out may point anywhere."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def write_xml(path: Path, xml: str) -> None:
    # Task Scheduler expects UTF-16 with a BOM, matching the declared encoding.
    path.write_bytes(b"\xff\xfe" + xml.encode("utf-16-le"))


def read_password(user: str, from_stdin: bool) -> str:
    """Read the account password once, from stdin or an interactive prompt."""
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        if not password:
            raise ConfigError("--password-stdin was given but stdin was empty")
        return password

    # getpass reads the Windows console directly rather than stdin, so it blocks
    # forever without a terminal instead of failing.
    if not sys.stdin.isatty():
        raise ConfigError(
            "--register needs a terminal to prompt for the account password; "
            "run it from an interactive shell, or pipe the password in with "
            "--password-stdin"
        )
    password = getpass.getpass(f"Password for {user} (hidden -- paste works): ")
    if not password:
        raise ConfigError("no password entered")
    return password


def is_elevated() -> bool:
    """True if this process can register HighestAvailable tasks."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except AttributeError:  # not Windows
        return False


def _ps_quote(value: str) -> str:
    """Wrap a value in a PowerShell single-quoted literal."""
    return "'" + value.replace("'", "''") + "'"


def build_elevation_command(argv: list[str]) -> str:
    """PowerShell that reruns this script, with these arguments, elevated."""
    invocation = " ".join(
        _ps_quote(part) for part in [sys.executable, str(Path(__file__).resolve()), *argv]
    )
    # The marker stops the elevated run from trying to elevate again, so a
    # refused or ineffective UAC prompt cannot become a loop.
    child = f"$env:{ELEVATION_MARKER}='1'; & {invocation}"
    return (
        "Start-Process -FilePath 'powershell.exe' -Verb RunAs "
        f"-WorkingDirectory {_ps_quote(str(REPO_ROOT))} "
        f"-ArgumentList '-NoExit','-NoProfile','-Command',{_ps_quote(child)}"
    )


def relaunch_elevated(argv: list[str]) -> int:
    """Open an elevated console running this script; returns an exit code."""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", build_elevation_command(argv)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        print(f"error: could not open an elevated window: {detail}", file=sys.stderr)
        return 1
    print(
        "Opened an elevated window (accept the UAC prompt). Enter the account "
        "password there; this window is done."
    )
    return 0


def needs_password(task: dict[str, Any], defaults: dict[str, Any]) -> bool:
    """True if registering this task requires the account password.

    Only "run whether user is logged on or not" (LogonType Password) stores a
    credential; interactive and S4U tasks register without one.
    """
    return task.get("logon_type", defaults.get("logon_type", "password")) == "password"


# Register-ScheduledTask rather than `schtasks /RP` so the password is never in
# a command line, where it would be readable from any process listing. It is
# passed through the child's environment instead.
_REGISTER_PS = """
$ErrorActionPreference = 'Stop'
$xml = [System.IO.File]::ReadAllText($env:SHOEBOX_TASK_XML)
$params = @{
    Xml      = $xml
    TaskName = $env:SHOEBOX_TASK_NAME
    TaskPath = $env:SHOEBOX_TASK_PATH
    User     = $env:SHOEBOX_TASK_USER
    Force    = $true
}
if ($env:SHOEBOX_TASK_PASSWORD) { $params.Password = $env:SHOEBOX_TASK_PASSWORD }
Register-ScheduledTask @params | Out-Null
"""


def register(
    task: dict[str, Any],
    defaults: dict[str, Any],
    xml_path: Path,
    user: str,
    password: str | None,
) -> bool:
    folder = task.get("folder", defaults.get("folder", "\\")).rstrip("\\")
    print(f"  registering {folder}\\{task['name']}")

    env = {
        **os.environ,
        "SHOEBOX_TASK_XML": str(xml_path),
        "SHOEBOX_TASK_NAME": task["name"],
        "SHOEBOX_TASK_PATH": f"{folder}\\",
        "SHOEBOX_TASK_USER": task.get("user", user),
        "SHOEBOX_TASK_PASSWORD": password if needs_password(task, defaults) else "",
    }
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _REGISTER_PS],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print((result.stderr or result.stdout).strip(), file=sys.stderr)
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG, help="path to tasks.yaml")
    parser.add_argument("--out", type=Path, default=OUTPUT_DIR, help="directory for generated XML")
    parser.add_argument(
        "--only", action="append", metavar="NAME", help="limit to this task (repeatable)"
    )
    parser.add_argument(
        "--register", action="store_true", help="register the generated tasks with Task Scheduler"
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the account password from stdin instead of prompting for it",
    )
    parser.add_argument(
        "--no-elevate",
        action="store_true",
        help="fail instead of reopening in an elevated window when --register needs admin",
    )
    parser.add_argument(
        "--list", action="store_true", help="list the configured tasks; write nothing"
    )
    args = parser.parse_args()

    try:
        defaults, tasks = load_config(args.config)
        user = current_user()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.only:
        known = {t["name"] for t in tasks}
        if unknown := set(args.only) - known:
            print(f"error: unknown task(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 1
        tasks = [t for t in tasks if t["name"] in args.only]

    if args.list:
        for task in tasks:
            trigger = task.get("trigger") or {"type": "manual"}
            detail = trigger.get("at") or trigger.get("repeat_every") or ""
            run = task.get("run") or task.get("command", "")
            steps = [run] if isinstance(run, str) else run
            print(f"{task['name']:<32} {trigger.get('type', 'manual'):<8} {detail:<6} {steps[0]}")
            for step in steps[1:]:
                print(f"{'':<48} {step}")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)

    if args.register and not is_elevated():
        # Checked before prompting: registering would otherwise fail with a bare
        # "Access is denied" once per task, after asking for the password.
        if args.no_elevate or os.environ.get(ELEVATION_MARKER):
            print(
                "error: --register needs an elevated shell (the tasks run at "
                "HighestAvailable). Start one with:\n"
                "  Start-Process powershell -Verb RunAs",
                file=sys.stderr,
            )
            return 1
        if args.password_stdin:
            # ShellExecute's elevated process gets a fresh console; there is no
            # stdin to pipe into it.
            print(
                "error: --password-stdin cannot be piped into an elevated window; "
                "run it from a shell that is already elevated",
                file=sys.stderr,
            )
            return 1
        return relaunch_elevated(sys.argv[1:])

    password: str | None = None
    if args.register and any(needs_password(t, defaults) for t in tasks):
        # Read once and reused for every task; these all register under the
        # same account.
        try:
            password = read_password(user, args.password_stdin)
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    failures = 0
    for task in tasks:
        try:
            xml = build_xml(task, defaults, user)
        except (ConfigError, KeyError) as exc:
            print(f"error: {task.get('name', '<unnamed>')}: {exc}", file=sys.stderr)
            failures += 1
            continue

        xml_path = args.out / f"{task['name']}.xml"
        write_xml(xml_path, xml)
        print(f"wrote {_display_path(xml_path)}")

        if args.register and not register(task, defaults, xml_path, user, password):
            print(f"error: failed to register {task['name']}", file=sys.stderr)
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
