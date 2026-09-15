"""Slack bot service: listens for messages and runs CLI commands.

Environment variables (both unset on a normal checkout, so local behaviour is
the historical one):

- ``PORT``: serve a health endpoint on this port alongside the Socket Mode
  listener. Container platforms that manage long-running services decide
  whether a revision started by probing HTTP, and a Socket Mode bot dials *out*
  and would otherwise never answer.
- ``SHOEBOX_BOT_COMMANDS``: comma-separated subset of the commands below that
  this deployment may run. Commands the host cannot actually service should be
  withheld rather than offered and left to fail -- see the note on
  ``ALLOWED_COMMANDS``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys

from slack_sdk.web.async_client import AsyncWebClient

from shoebox.utils.slack import listen_for_commands
from shoebox.utils.slack_formatting import title

logger = logging.getLogger(__name__)

# Maps each allowed command to its accepted flags; the bool says whether the
# flag takes a value (``--reseed NAME``) or stands alone (``--force``).
# The 'ui' command is intentionally excluded (requires an interactive display).
# --config stays excluded: pointing the bot at an arbitrary file path from chat
# is not a capability it should have.
ALLOWED_COMMANDS: dict[str, dict[str, bool]] = {
    "sync-metadata": {},
    "end-oos-listings": {},
    "create-queue-excel": {},
    "create-listings": {
        "--dry-run": False,
        "--publish": False,
        "--schedule": False,
        "--scrape-prices": False,
    },
    "sync-active-listings": {},
    "sync-active-listing-details": {},
    "sync-orders": {},
    "orders-awaiting-shipment": {
        "--pull-order": False,
        "--buyer-order": False,
        "--display": False,
        "--message": False,
    },
    "watch-searches": {
        "--force": False,
        "--dry-run": False,
        "--list": False,
        "--only": True,
        "--reseed": True,
    },
}


def enabled_commands() -> dict[str, dict[str, bool]]:
    """``ALLOWED_COMMANDS``, narrowed by ``SHOEBOX_BOT_COMMANDS`` when set.

    A deployment can only service the commands its host is actually equipped
    for. ``create-listings`` and ``create-queue-excel`` read card scans and
    Excel workbooks from local paths and drive Chrome for price scraping, so a
    container running this bot has nothing for them to work on. Offering a
    command that is certain to fail is worse than not listing it: the failure
    arrives as a traceback in a Slack thread minutes later, and looks like a
    bug rather than a deployment boundary.

    Unknown names in the variable are ignored with a warning rather than being
    fatal -- a typo in a deploy config should not take the bot down.
    """
    raw = os.getenv("SHOEBOX_BOT_COMMANDS")
    if not raw or not raw.strip():
        return ALLOWED_COMMANDS

    wanted = [name.strip() for name in raw.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in ALLOWED_COMMANDS]
    if unknown:
        logger.warning("Ignoring unknown SHOEBOX_BOT_COMMANDS entries: %s", ", ".join(unknown))
    selected = {name: ALLOWED_COMMANDS[name] for name in wanted if name in ALLOWED_COMMANDS}
    if not selected:
        logger.warning("SHOEBOX_BOT_COMMANDS matched nothing; falling back to the full set")
        return ALLOWED_COMMANDS
    return selected


def help_text() -> str:
    return "Available commands:\n" + "\n".join(
        f"  /{cmd}"
        + (
            "  ["
            + " | ".join(f"{f} NAME" if takes_value else f for f, takes_value in flags.items())
            + "]"
            if flags
            else ""
        )
        for cmd, flags in enabled_commands().items()
    )


def parse_command(command: str, args: list[str]) -> tuple[str, list[str]] | None:
    """Validate command and strip any unrecognized flags. Returns None if command unknown.

    Value-taking flags accept both ``--reseed NAME`` and ``--reseed=NAME``. A
    value-taking flag with no value is dropped whole rather than passed on to
    argparse, which would abort the run over what was probably a typo in chat.
    """
    commands = enabled_commands()
    if command not in commands:
        return None
    allowed = commands[command]
    valid_args: list[str] = []
    ignored: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        flag, _, inline_value = arg.partition("=")
        if flag not in allowed:
            ignored.append(arg)
        elif not allowed[flag]:
            # Boolean flag: an =value form would be an error, so it's ignored too.
            (valid_args if arg == flag else ignored).append(arg)
        elif inline_value:
            valid_args += [flag, inline_value]
        elif i + 1 < len(args) and not args[i + 1].startswith("--"):
            valid_args += [flag, args[i + 1]]
            i += 1
        else:
            ignored.append(arg)
        i += 1
    if ignored:
        logger.warning("Ignoring unrecognized flags for %r: %s", command, ignored)
    return command, valid_args


def run_command(command: str, args: list[str]) -> str:
    """Run an shoebox command in a subprocess and return combined output."""
    argv = [sys.executable, "-m", "shoebox.cli", command, *args]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=600)
        output = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Command timed out after 600 seconds."
    except Exception as exc:
        return f"Error running command: {exc}"


async def dispatch(event: dict, client: AsyncWebClient, command: str, args: list[str]) -> None:
    """Validate and execute a command, replying in a thread on the triggering message."""
    channel = event["channel"]
    thread_ts = event["ts"]

    if command in ("help", "h"):
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=help_text())
        return

    parsed = parse_command(command, args)
    if parsed is None:
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"Unknown command: {command!r}\n\n{help_text()}",
        )
        return

    command, valid_args = parsed
    cmd_display = "shoebox " + " ".join([command] + valid_args)
    await client.chat_postMessage(
        channel=channel, thread_ts=thread_ts, text=f"Running: `{cmd_display}`"
    )
    logger.info("Slack triggered: %s", cmd_display)

    loop = asyncio.get_running_loop()
    output = await loop.run_in_executor(None, run_command, command, valid_args)

    if len(output) > 3900:
        output = "...(truncated)\n" + output[-3885:]
    await client.chat_postMessage(
        channel=channel, thread_ts=thread_ts, text=f"{title('Done')}\n```\n{output}\n```"
    )


async def serve_health(port: int) -> None:
    """Answer HTTP on ``port`` so a container platform can see the bot is up.

    Socket Mode opens an outbound WebSocket and never accepts a request, so a
    platform that decides "did this start?" by connecting to a port would kill
    an otherwise healthy bot. This says nothing about Slack connectivity -- it
    reports that the process is alive, which is precisely what a startup probe
    is asking.
    """
    from aiohttp import web

    async def ok(_request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/healthz", ok)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Health endpoint listening on port %d", port)


async def _run_service() -> None:
    port = os.getenv("PORT")
    if port:
        try:
            await serve_health(int(port))
        except ValueError:
            logger.warning("Ignoring non-numeric PORT=%r", port)
    await listen_for_commands(dispatch=dispatch)


def start_bot_service() -> None:
    """Start the Slack command-listener bot. Runs until interrupted."""
    from shoebox.settings import get_settings

    channel = get_settings().slack.command_channel
    logger.info("Starting Slack bot service on channel=%s", channel)
    asyncio.run(_run_service())
