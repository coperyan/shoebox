"""Delete every message the shoebox bot posted in the given Slack channels.

Thread replies are deleted before their parent, so nothing is left behind as a
"This message was deleted" stub with orphaned replies under it.

A bot token can only delete the bot's own messages. Anything a person posted
is counted as skipped -- remove those by hand, or rerun with a user token
(``--token xoxp-...``) that has ``chat:write`` and the history scopes.

Dry run by default: it lists what it would delete and touches nothing. Pass
``--delete`` to actually delete. Deleted messages cannot be recovered.

    python scripts/clear_slack_channels.py barry-bonds buster-posey
    python scripts/clear_slack_channels.py barry-bonds buster-posey --delete

Channels may be names (with or without ``#``) or IDs. Resolving names needs
``channels:read`` (``groups:read`` for private channels); pass IDs if the bot
lacks it.

Or let a searches.yaml pick them: ``--disabled-from`` targets every channel a
disabled search posts to, except channels an enabled search still posts to.

    python scripts/clear_slack_channels.py --disabled-from ../shoebox-searches/searches.yaml
"""

from __future__ import annotations

import argparse
import sys

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from shoebox.models.saved_search import load_searches_file
from shoebox.settings import get_settings


def disabled_channels(path: str) -> dict[str, str]:
    """{label: channel ID} for channels only disabled searches post to."""
    sf = load_searches_file(path)
    searches = sf.resolved()
    live = {s.channel for s in searches if s.enabled}
    dead = {s.channel for s in searches if not s.enabled and s.channel} - live
    alias_of = {cid: alias for alias, cid in sf.channels.items()}
    return {alias_of.get(cid, cid): cid for cid in sorted(dead, key=lambda c: alias_of.get(c, c))}


def resolve_channels(client: WebClient, wanted: list[str]) -> dict[str, str]:
    """Map each requested name/ID to a channel ID."""
    names = [w.lstrip("#") for w in wanted]
    resolved = {n: n for n in names if n[:1] in ("C", "G") and n.isupper()}
    pending = {n for n in names if n not in resolved}
    if not pending:
        return resolved

    cursor = None
    while pending:
        resp = client.conversations_list(
            types="public_channel,private_channel",
            exclude_archived=True,
            limit=1000,
            cursor=cursor,
        )
        for ch in resp["channels"]:
            if ch["name"] in pending:
                resolved[ch["name"]] = ch["id"]
                pending.discard(ch["name"])
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    if pending:
        sys.exit(f"Could not find channel(s): {', '.join(sorted(pending))}")
    return resolved


def iter_messages(client: WebClient, channel: str):
    """Yield (ts, user, is_bot) for every message, replies before their parent."""
    cursor = None
    while True:
        resp = client.conversations_history(channel=channel, limit=200, cursor=cursor)
        for msg in resp["messages"]:
            if msg.get("reply_count"):
                yield from iter_replies(client, channel, msg["ts"])
            yield msg["ts"], msg.get("user"), "bot_id" in msg
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return


def iter_replies(client: WebClient, channel: str, thread_ts: str):
    cursor = None
    while True:
        resp = client.conversations_replies(
            channel=channel, ts=thread_ts, limit=200, cursor=cursor
        )
        for msg in resp["messages"]:
            if msg["ts"] != thread_ts:  # the parent is yielded by the caller
                yield msg["ts"], msg.get("user"), "bot_id" in msg
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return


def clear_channel(client: WebClient, name: str, channel_id: str, delete: bool) -> None:
    deleted = skipped = failed = 0
    for i, (ts, _user, is_bot) in enumerate(iter_messages(client, channel_id), 1):
        if i % 100 == 0:
            print(f"  #{name}: {i} messages so far...", flush=True)
        if not delete:
            # A dry run can't ask Slack what's deletable; bot posts are the
            # best guess for a bot token, everything else is likely a skip.
            if is_bot:
                deleted += 1
            else:
                skipped += 1
            continue
        try:
            client.chat_delete(channel=channel_id, ts=ts)
            deleted += 1
        except SlackApiError as exc:
            if exc.response["error"] in ("cant_delete_message", "message_not_found"):
                skipped += 1
            else:
                failed += 1
                print(f"  {name}: {ts} -> {exc.response['error']}", file=sys.stderr)

    verb = "deleted" if delete else "would delete (dry run)"
    print(f"#{name}: {deleted} {verb}, {skipped} skipped, {failed} failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("channels", nargs="*", help="channel names or IDs")
    parser.add_argument(
        "--disabled-from",
        metavar="SEARCHES_YAML",
        help="also clear every channel only disabled searches in this file post to",
    )
    parser.add_argument("--delete", action="store_true", help="actually delete (default: dry run)")
    parser.add_argument("--token", help="override slack.bot_token, e.g. a user xoxp- token")
    args = parser.parse_args()

    client = WebClient(token=args.token or get_settings().slack.bot_token)
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=10))

    targets = disabled_channels(args.disabled_from) if args.disabled_from else {}
    if args.channels:
        targets.update(resolve_channels(client, args.channels))
    if not targets:
        parser.error("give channel names/IDs or --disabled-from")

    for name, channel_id in targets.items():
        clear_channel(client, name, channel_id, args.delete)


if __name__ == "__main__":
    main()
