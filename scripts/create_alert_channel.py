"""Create the Cloud Monitoring notification channel that posts alerts to Slack.

Cloud Monitoring can post to Slack two ways. The console offers an OAuth flow
that installs Google's own "Google Cloud Monitoring" Slack app, which then has
to be invited to the channel. This script takes the other route: it reuses the
bot this project already has. ``slack.bot_token`` already carries ``chat:write``
and ``notificationsbot`` is already in ``slack.notify_channel``, so alerts
arrive from the same bot, in the same channel, as the pipelines' own "Starting"
and "Completed" messages -- with no second Slack app to install or invite.

The token is read from the same config the pipelines use and sent only to the
Monitoring API in your own project, which stores it obfuscated (reads return a
few characters). It is never printed or written to disk here.

    python scripts/create_alert_channel.py --dry-run   # show what would be sent
    python scripts/create_alert_channel.py             # create it
    python scripts/create_alert_channel.py --attach-to-policies

``--attach-to-policies`` adds the new channel to every alerting policy labelled
``app=shoebox`` that has no channel yet, which is what ``deploy/alerts/*.yaml``
produce when applied without ``--notification-channels``.

Re-running is safe: an existing channel with the same display name is reused
rather than duplicated. Rotate the token by deleting the channel and re-running.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

DISPLAY_NAME = "shoebox pipeline alerts (Slack)"
API = "https://monitoring.googleapis.com/v3"


class ChannelError(Exception):
    """Raised when the channel cannot be created."""


def access_token() -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=False
    )
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise ChannelError("could not get a gcloud access token; run `gcloud auth login`")
    return token


def call(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:500]
        raise ChannelError(
            f"{method} {url.split('/v3/')[-1]} -> HTTP {exc.code}: {detail}"
        ) from None


def find_existing(project: str, token: str) -> str | None:
    listing = call("GET", f"{API}/projects/{project}/notificationChannels", token)
    for channel in listing.get("notificationChannels", []):
        if channel.get("displayName") == DISPLAY_NAME:
            return channel["name"]
    return None


def create(project: str, channel_id: str, bot_token: str, token: str) -> str:
    body = {
        "type": "slack",
        "displayName": DISPLAY_NAME,
        "description": (
            "Posts Cloud Run job alerts to the channel the shoebox pipelines already "
            "report to, using this project's existing Slack bot token."
        ),
        "labels": {"channel_name": channel_id, "auth_token": bot_token},
        "userLabels": {"app": "shoebox"},
        "enabled": True,
    }
    return call("POST", f"{API}/projects/{project}/notificationChannels", token, body)["name"]


def attach_to_policies(project: str, channel: str, token: str) -> list[str]:
    """Add the channel to shoebox policies that have none. Returns those updated."""
    listing = call("GET", f"{API}/projects/{project}/alertPolicies", token)
    updated = []
    for policy in listing.get("alertPolicies", []):
        if policy.get("userLabels", {}).get("app") != "shoebox":
            continue
        if policy.get("notificationChannels"):
            continue
        name = policy["name"]
        call(
            "PATCH",
            f"{API}/{name}?updateMask=notificationChannels",
            token,
            {"notificationChannels": [channel]},
        )
        updated.append(policy["displayName"])
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be created; change nothing"
    )
    parser.add_argument(
        "--attach-to-policies",
        action="store_true",
        help="also add the channel to shoebox alerting policies that have none",
    )
    parser.add_argument(
        "--channel-name",
        help=(
            "Slack channel to post to, overriding slack.notify_channel. "
            "Use this if the channel id is rejected and you need the #name form"
        ),
    )
    args = parser.parse_args()

    try:
        from shoebox.settings import get_settings

        settings = get_settings()
        project = settings.gcp.project_id
        slack_channel = args.channel_name or settings.slack.notify_channel
        if not settings.slack.bot_token:
            raise ChannelError("slack.bot_token is empty in the app config")

        print(f"project        : {project}")
        print(f"slack channel  : {slack_channel}")
        print(f"display name   : {DISPLAY_NAME}")

        if args.dry_run:
            print("\ndry run: nothing created.")
            return 0

        token = access_token()
        existing = find_existing(project, token)
        if existing:
            print(f"\nalready exists : {existing}")
            channel = existing
        else:
            channel = create(project, slack_channel, settings.slack.bot_token, token)
            print(f"\ncreated        : {channel}")

        if args.attach_to_policies:
            updated = attach_to_policies(project, channel, token)
            print("attached to    :", ", ".join(updated) if updated else "no policies needed it")
        else:
            print("\nAttach it to the alerting policies with:")
            print(
                f"  gcloud monitoring policies update POLICY_ID --set-notification-channels={channel}"
            )
            print("or re-run this script with --attach-to-policies.")
    except ChannelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
