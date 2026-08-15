import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.web.async_client import AsyncWebClient

from shoebox.settings import get_settings

logger = logging.getLogger(__name__)

DEFAULT_ACCEPTED_REACTIONS = ["heart"]

APPROVE_ACTION_ID = "shoebox_approve"


def _with_rate_limit_retries(client: AsyncWebClient) -> AsyncWebClient:
    """Honor Slack 429s (~1 msg/sec/channel) by waiting Retry-After and retrying,
    so message bursts (e.g. order summaries) don't drop messages."""
    client.retry_handlers.append(AsyncRateLimitErrorRetryHandler(max_retry_count=3))
    return client


def _build_app(bot_token: str) -> AsyncApp:
    app = AsyncApp(token=bot_token)
    _with_rate_limit_retries(app.client)
    return app


@dataclass
class ApprovalResult:
    approved: bool  # True = Approve button clicked, False = reply with new value
    reply_text: str | None = None  # set when approved=False
    approved_by: str | None = None  # Slack user id, set when approved=True


async def send_message_only(
    bot_token: str,
    channel: str,
    text: str,
    thread_ts: str | None = None,
    unfurl_links: bool = False,
    blocks: list[dict] | None = None,
) -> str:
    """Post a message and return its ts (usable as thread_ts for replies).

    ``unfurl_links`` must be opted into: bot tokens don't expand text links by
    default, so a message whose value is the link preview (an eBay listing, say)
    gets no preview at all unless this is set.

    When ``blocks`` are given they become the rendered message and ``text`` is
    demoted to the push-notification and fallback string — so it must still read
    as a complete summary on its own.
    """
    client = _with_rate_limit_retries(AsyncWebClient(token=bot_token))
    resp = await client.chat_postMessage(
        channel=channel,
        text=text,
        thread_ts=thread_ts,
        unfurl_links=unfurl_links,
        unfurl_media=unfurl_links,
        blocks=blocks,
    )
    return resp["ts"]


async def send_and_await_reply(
    bot_token: str,
    app_token: str,
    channel: str,
    text: str,
    *,
    timeout_s: int = 300,
) -> str:
    """
    Sends `text` to `channel`, then waits for a *threaded reply* to that message.
    Returns the reply text.
    """
    app = _build_app(bot_token)
    reply_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    target_ts: str | None = None
    bot_user_id = (await app.client.auth_test())["user_id"]

    @app.event("message")
    async def on_message(event: dict) -> None:
        if event.get("subtype") is not None:
            return
        if target_ts is None:
            return
        if event.get("channel") != channel or event.get("thread_ts") != target_ts:
            return
        if event.get("user") == bot_user_id:
            return
        reply_text = (event.get("text") or "").strip()
        if reply_text and not reply_future.done():
            reply_future.set_result(reply_text)

    handler = AsyncSocketModeHandler(app, app_token)
    await handler.connect_async()

    try:
        sent = await app.client.chat_postMessage(channel=channel, text=text)
        target_ts = sent["ts"]
        return await asyncio.wait_for(reply_future, timeout=timeout_s)
    finally:
        await handler.disconnect_async()
        await handler.close_async()


async def send_and_await_reaction(
    bot_token: str,
    app_token: str,
    channel: str,
    text: str,
    *,
    accepted_reactions: list[str] | None = None,
    timeout_s: int = 300,
) -> bool:
    """
    Sends `text` to `channel`, then waits for a reaction on that message.
    A `:heart:` reaction is accepted by default.
    Returns True when an accepted reaction is received, raises asyncio.TimeoutError otherwise.
    """
    if accepted_reactions is None:
        accepted_reactions = DEFAULT_ACCEPTED_REACTIONS

    app = _build_app(bot_token)
    reaction_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    target_ts: str | None = None

    @app.event("reaction_added")
    async def on_reaction(event: dict) -> None:
        item = event.get("item", {})
        if item.get("channel") != channel or item.get("ts") != target_ts:
            return
        if event.get("reaction") in accepted_reactions and not reaction_future.done():
            reaction_future.set_result(True)

    handler = AsyncSocketModeHandler(app, app_token)
    await handler.connect_async()

    try:
        sent = await app.client.chat_postMessage(channel=channel, text=text)
        target_ts = sent["ts"]
        return await asyncio.wait_for(reaction_future, timeout=timeout_s)
    finally:
        await handler.disconnect_async()
        await handler.close_async()


async def _replace_with_outcome(
    client: AsyncWebClient, channel: str, ts: str, text: str, status: str
) -> None:
    """Rewrite an approval message to show its outcome, dropping the button
    so it can't be clicked again (or after the waiter has gone away)."""
    try:
        await client.chat_update(
            channel=channel,
            ts=ts,
            text=f"{text}\n\n{status}",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": status}]},
            ],
        )
    except Exception:
        pass  # cosmetic update; never mask the actual result


async def send_and_await_approval(
    bot_token: str,
    app_token: str,
    channel: str,
    text: str,
    *,
    approve_label: str = "Approve",
    timeout_s: int = 300,
) -> ApprovalResult:
    """
    Sends `text` with an Approve button, then waits for whichever comes first:
      - Button click           -> ApprovalResult(approved=True, approved_by=<user id>)
      - A threaded reply       -> ApprovalResult(approved=False, reply_text=<reply>)
    Raises asyncio.TimeoutError if neither arrives within timeout_s.
    The message is edited afterwards to show the outcome and remove the button.

    Requires Interactivity enabled on the Slack app (with Socket Mode, no
    Request URL is needed -- just the toggle).
    """
    app = _build_app(bot_token)
    result_future: asyncio.Future[ApprovalResult] = asyncio.get_running_loop().create_future()
    target_ts: str | None = None
    bot_user_id = (await app.client.auth_test())["user_id"]

    @app.action(APPROVE_ACTION_ID)
    async def on_approve(ack, body: dict) -> None:
        await ack()
        if body.get("channel", {}).get("id") != channel:
            return
        if body.get("message", {}).get("ts") != target_ts:
            return
        if not result_future.done():
            result_future.set_result(
                ApprovalResult(approved=True, approved_by=body.get("user", {}).get("id"))
            )

    @app.event("message")
    async def on_reply(event: dict) -> None:
        if event.get("subtype") is not None:
            return
        if target_ts is None:
            return
        if event.get("channel") != channel or event.get("thread_ts") != target_ts:
            return
        if event.get("user") == bot_user_id:
            return
        reply_text = (event.get("text") or "").strip()
        if reply_text and not result_future.done():
            result_future.set_result(ApprovalResult(approved=False, reply_text=reply_text))

    handler = AsyncSocketModeHandler(app, app_token)
    await handler.connect_async()

    try:
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": approve_label},
                        "style": "primary",
                        "action_id": APPROVE_ACTION_ID,
                    }
                ],
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "…or reply in this thread with a new value."}
                ],
            },
        ]
        sent = await app.client.chat_postMessage(channel=channel, text=text, blocks=blocks)
        target_ts = sent["ts"]

        try:
            result = await asyncio.wait_for(result_future, timeout=timeout_s)
        except TimeoutError:
            await _replace_with_outcome(
                app.client, channel, target_ts, text, "⏰ Timed out — no action taken."
            )
            raise

        if result.approved:
            who = f" by <@{result.approved_by}>" if result.approved_by else ""
            status = f"✅ Approved{who}."
        else:
            status = f"✏️ Override received: {result.reply_text}"
        await _replace_with_outcome(app.client, channel, target_ts, text, status)
        return result
    finally:
        await handler.disconnect_async()
        await handler.close_async()


@dataclass
class BatchPrompt:
    """One prompt message in a batch: ``key`` is the caller's identifier
    (e.g. a listing id), ``text`` the mrkdwn body (also the notification
    fallback when ``blocks`` are given)."""

    key: str
    text: str
    blocks: list[dict] | None = None


# Posts text into the prompt's own reply thread (hints, validation errors).
PostThreadFn = Callable[[str], Awaitable[None]]

# (key, reply_text, post_thread) -> outcome string to stamp on the prompt
# message (resolves the item), or None to leave it pending for another reply.
BatchReplyHandler = Callable[[str, str, PostThreadFn], Awaitable[str | None]]

EXPIRED_STATUS = "⌛ Expired — no reply this run."


async def send_batch_and_await_replies(
    bot_token: str,
    app_token: str,
    channel: str,
    prompts: list[BatchPrompt],
    on_reply: BatchReplyHandler,
    *,
    timeout_s: int = 900,
    pacing_seconds: float = 1.0,
    sweep_interval_s: float = 15.0,
) -> dict[str, str]:
    """Post every prompt up front, then dispatch threaded replies to any of
    them as they arrive, until all are resolved or the deadline passes.

    Each reply to a prompt's thread invokes ``on_reply``; an outcome string
    resolves that prompt (its message is rewritten to show the outcome), None
    keeps it pending so the user can reply again. Returns {key: outcome} —
    prompts still pending at the deadline are stamped and returned with
    EXPIRED_STATUS rather than raising.

    Socket Mode events are the fast path, but Slack load-balances events
    across ALL of an app's open Socket Mode connections (the always-on
    slack-bot service typically holds one), so replies can be delivered to a
    connection this waiter never sees. A conversations.replies poll of every
    pending thread runs every ``sweep_interval_s`` to pick those up.
    """
    app = _build_app(bot_token)
    bot_user_id = (await app.client.auth_test())["user_id"]

    pending: dict[str, BatchPrompt] = {}  # prompt ts -> prompt
    in_flight: set[str] = set()
    last_seen: dict[str, str] = {}  # prompt ts -> newest handled reply ts
    results: dict[str, str] = {}
    all_resolved = asyncio.Event()

    async def process_reply(
        prompt_ts: str | None, reply_ts: str, reply_text: str, user: str | None
    ) -> None:
        """Shared by the event handler and the sweep. The guards and state
        mutations below have no awaits between them, so the two paths can't
        double-process a reply."""
        if user == bot_user_id or not reply_text:
            return
        if prompt_ts not in pending or prompt_ts in in_flight:
            return
        # Slack ts strings ("1723412345.123456") sort lexicographically.
        if reply_ts <= last_seen.get(prompt_ts, ""):
            return
        in_flight.add(prompt_ts)
        last_seen[prompt_ts] = reply_ts
        try:
            prompt = pending[prompt_ts]

            async def post_thread(msg: str) -> None:
                await app.client.chat_postMessage(channel=channel, text=msg, thread_ts=prompt_ts)

            try:
                outcome = await on_reply(prompt.key, reply_text, post_thread)
            except Exception:
                logger.exception("Reply handler failed for %s; leaving it pending", prompt.key)
                return
            if outcome is not None:
                await _replace_with_outcome(app.client, channel, prompt_ts, prompt.text, outcome)
                pending.pop(prompt_ts, None)
                results[prompt.key] = outcome
                if not pending:
                    all_resolved.set()
        finally:
            in_flight.discard(prompt_ts)

    @app.event("message")
    async def on_message(event: dict) -> None:
        if event.get("subtype") is not None or event.get("channel") != channel:
            return
        await process_reply(
            event.get("thread_ts"),
            event.get("ts") or "",
            (event.get("text") or "").strip(),
            event.get("user"),
        )

    async def sweep() -> None:
        """Poll pending threads for replies the socket connection never saw."""
        for prompt_ts in list(pending):
            if prompt_ts in in_flight:
                continue
            try:
                resp = await app.client.conversations_replies(
                    channel=channel,
                    ts=prompt_ts,
                    oldest=last_seen.get(prompt_ts) or prompt_ts,
                    limit=20,
                )
            except Exception as e:
                logger.warning("Reply sweep failed for thread %s: %s", prompt_ts, e)
                continue
            for msg in resp.get("messages") or []:
                if msg.get("subtype") is not None or msg.get("ts") == prompt_ts:
                    continue
                await process_reply(
                    prompt_ts,
                    msg.get("ts") or "",
                    (msg.get("text") or "").strip(),
                    msg.get("user"),
                )
                if prompt_ts not in pending:
                    break

    handler = AsyncSocketModeHandler(app, app_token)
    await handler.connect_async()

    try:
        for i, prompt in enumerate(prompts):
            if i and pacing_seconds:
                await asyncio.sleep(pacing_seconds)
            sent = await app.client.chat_postMessage(
                channel=channel, text=prompt.text, blocks=prompt.blocks
            )
            pending[sent["ts"]] = prompt

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(
                    all_resolved.wait(), timeout=min(sweep_interval_s, remaining)
                )
            except TimeoutError:
                await sweep()
            else:
                if pending:
                    # Set prematurely: every prompt posted *so far* resolved
                    # while the posting loop was still running.
                    all_resolved.clear()

        for ts, prompt in list(pending.items()):
            await _replace_with_outcome(app.client, channel, ts, prompt.text, EXPIRED_STATUS)
            results[prompt.key] = EXPIRED_STATUS
        return results
    finally:
        await handler.disconnect_async()
        await handler.close_async()


def notify(
    channel: str,
    message: str,
    thread_ts: str | None = None,
    unfurl_links: bool = False,
    blocks: list[dict] | None = None,
) -> str:
    """Post a message; returns its ts so follow-ups can thread under it.

    Pass ``unfurl_links=True`` when the message's links are the point (link
    previews are off by default for bot tokens), or ``blocks`` to control the
    rendering — in which case ``message`` is the notification/fallback text.
    """
    slack = get_settings().slack
    return asyncio.run(
        send_message_only(
            slack.bot_token,
            channel,
            message,
            thread_ts=thread_ts,
            unfurl_links=unfurl_links,
            blocks=blocks,
        )
    )


def notify_best_effort(channel: str, message: str) -> None:
    """``notify()``, but a Slack failure logs instead of raising.

    For progress pings from data pipelines: the sync is the job, the ping is a
    courtesy, and a Slack outage must not abort an eBay/BigQuery run."""
    try:
        notify(channel, message)
    except Exception:
        logger.warning("Slack notify failed (channel=%s); continuing", channel, exc_info=True)


def notify_and_wait(channel: str, message: str, timeout_s: int = 600) -> str:
    slack = get_settings().slack
    return asyncio.run(
        send_and_await_reply(
            slack.bot_token, slack.app_token, channel, message, timeout_s=timeout_s
        )
    )


def notify_and_wait_approval(
    channel: str,
    message: str,
    timeout_s: int = 600,
    approve_label: str = "Approve",
) -> ApprovalResult:
    slack = get_settings().slack
    return asyncio.run(
        send_and_await_approval(
            slack.bot_token,
            slack.app_token,
            channel,
            message,
            approve_label=approve_label,
            timeout_s=timeout_s,
        )
    )


def notify_batch_and_wait(
    channel: str,
    prompts: list[BatchPrompt],
    on_reply: BatchReplyHandler,
    timeout_s: int = 900,
) -> dict[str, str]:
    """Sync wrapper for send_batch_and_await_replies. ``on_reply`` is still an
    async callable (wrap blocking work in asyncio.to_thread)."""
    slack = get_settings().slack
    return asyncio.run(
        send_batch_and_await_replies(
            slack.bot_token,
            slack.app_token,
            channel,
            prompts,
            on_reply,
            timeout_s=timeout_s,
        )
    )


def notify_and_wait_reaction(
    channel: str,
    message: str,
    timeout_s: int = 600,
    accepted_reactions: list[str] | None = None,
) -> bool:
    slack = get_settings().slack
    return asyncio.run(
        send_and_await_reaction(
            slack.bot_token,
            slack.app_token,
            channel,
            message,
            accepted_reactions=accepted_reactions,
            timeout_s=timeout_s,
        )
    )


CommandDispatch = Callable[[dict, AsyncWebClient, str, list[str]], Awaitable[None]]


async def listen_for_commands(dispatch: CommandDispatch) -> None:
    """
    Long-running Socket Mode listener. For each top-level (non-threaded) message
    posted in settings.slack.command_channel, parses it as a command (with optional
    flags) and calls dispatch(event, client, command, args).

    When settings.slack.allowed_user_ids is non-empty, messages from any other
    user are ignored (logged at WARNING).

    Supports both '/command --flag' and 'command --flag' formats.
    Cancel the coroutine (or Ctrl-C) to stop.
    """
    slack = get_settings().slack
    app = _build_app(slack.bot_token)

    @app.event("message")
    async def on_message(event: dict) -> None:
        if event.get("subtype") is not None:
            return
        if event.get("channel") != slack.command_channel:
            return
        if event.get("thread_ts") is not None:
            return
        user = event.get("user")
        if slack.allowed_user_ids and user not in slack.allowed_user_ids:
            logger.warning("Ignoring command from unauthorized user %s", user)
            return
        text = (event.get("text") or "").strip()
        if not text:
            return
        if text.startswith("/"):
            text = text[1:]
        parts = text.split()
        if not parts:
            return
        command, args = parts[0].lower(), parts[1:]
        await dispatch(event, app.client, command, args)

    handler = AsyncSocketModeHandler(app, slack.app_token)
    try:
        await handler.start_async()
    finally:
        await handler.close_async()
