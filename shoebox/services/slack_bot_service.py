"""Slack bot service: listens for messages and runs CLI commands."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys

from slack_sdk.web.async_client import AsyncWebClient

from shoebox.utils.slack import listen_for_commands
from shoebox.utils.slack_formatting import title

logger = logging.getLogger(__name__)

# Maps each allowed command to its accepted flags.
# The 'ui' command is intentionally excluded (requires an interactive display).
ALLOWED_COMMANDS: dict[str, list[str]] = {
    "sync-metadata": [],
    "end-oos-listings": [],
    "create-queue-excel": [],
    "create-listings": ["--dry-run", "--publish", "--schedule", "--scrape-prices"],
    "sync-active-listings": [],
    "sync-active-listing-details": [],
    "sync-orders": [],
    "orders-awaiting-shipment": [
        "--pull-order",
        "--buyer-order",
        "--display",
        "--message",
    ],
    # Value-taking flags (--only NAME, --config PATH) are omitted on purpose:
    # the validator below keeps only exact flag matches, so the value would be
    # silently dropped and the flag would error out.
    "watch-searches": ["--force", "--dry-run", "--list"],
}

_HELP_TEXT = "Available commands:\n" + "\n".join(
    f"  /{cmd}" + (f"  [{' | '.join(flags)}]" if flags else "")
    for cmd, flags in ALLOWED_COMMANDS.items()
)


def parse_command(command: str, args: list[str]) -> tuple[str, list[str]] | None:
    """Validate command and strip any unrecognized flags. Returns None if command unknown."""
    if command not in ALLOWED_COMMANDS:
        return None
    allowed_flags = set(ALLOWED_COMMANDS[command])
    valid_args = [a for a in args if a in allowed_flags]
    ignored = [a for a in args if a not in allowed_flags]
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
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=_HELP_TEXT)
        return

    parsed = parse_command(command, args)
    if parsed is None:
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"Unknown command: {command!r}\n\n{_HELP_TEXT}",
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


def start_bot_service() -> None:
    """Start the Slack command-listener bot. Runs until interrupted."""
    from shoebox.settings import get_settings

    channel = get_settings().slack.command_channel
    logger.info("Starting Slack bot service on channel=%s", channel)
    asyncio.run(listen_for_commands(dispatch=dispatch))
