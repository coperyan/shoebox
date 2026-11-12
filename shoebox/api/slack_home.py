"""Slack App Home buttons that start Cloud Run jobs (docs/content/guides/slack.mdx).

A second Slack app, "Shoebox Commands", talks to this API over HTTP. Slack
POSTs to ``/slack/events`` whenever someone opens the app's Home tab, and we
publish one button per ``HOME_COMMANDS`` entry with how its job last ran; it
POSTs to ``/slack/interactions`` when a button is clicked, and we start that
Cloud Run job with the args deployed in deploy/jobs.yaml, then republish the
tab. Every request must carry a valid signature from the app's signing
secret, and only ``slack.allowed_user_ids`` see buttons or start anything --
nobody, while that list is empty.

It is a separate app from the bot in ``utils/slack.py`` on purpose: that one
uses Socket Mode, which delivers every click over the socket instead of to a
URL, so send-offers' prompts and an HTTP endpoint can't share it.

All work happens before the response (Cloud Run throttles the CPU once a
request returns) and Slack waits only 3 seconds, so a click checks and starts
one job and republishes the tab from the statuses stored in the view's
``private_metadata`` rather than asking Cloud Run about every job again.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from slack_sdk.signature import SignatureVerifier

from shoebox.api.deps import ApiContext, get_ctx
from shoebox.clients.cloud_run import Execution
from shoebox.settings import Settings

logger = logging.getLogger(__name__)

RUN_ACTION_PREFIX = "run:"
REFRESH_ACTION_ID = "refresh"
# Slack's cap on a view's private_metadata.
MAX_PRIVATE_METADATA = 3000
MAX_ERROR_CHARS = 300


@dataclass(frozen=True)
class HomeCommand:
    """One Home-tab button. ``job`` names a Cloud Run job in deploy/jobs.yaml,
    started with the args deployed there; Slack never supplies arguments."""

    job: str
    label: str
    # mrkdwn; {notify_channel} and {offers_channel} become channel links.
    description: str
    # Asked in a confirmation dialog before the job starts.
    confirm: str | None = None


HOME_COMMANDS: tuple[HomeCommand, ...] = (
    HomeCommand(
        job="orders-awaiting-shipment",
        label="Orders awaiting shipment",
        description="Posts the pull list to {notify_channel}.",
    ),
    HomeCommand(
        job="sync-orders",
        label="Sync orders",
        description="Refreshes orders in BigQuery. Also runs nightly.",
    ),
    HomeCommand(
        job="sync-active-listings",
        label="Sync active listings",
        description="Refreshes active listings in BigQuery. Also runs nightly.",
    ),
    HomeCommand(
        job="sync-watch-list",
        label="Sync watch list",
        description="Refreshes the watch list in BigQuery. Also runs nightly.",
    ),
    HomeCommand(
        job="send-offers",
        label="Send offers",
        description=(
            "Posts a prompt per eligible listing to {offers_channel}; reply in each "
            "thread with a price or `skip` within 10 minutes."
        ),
        confirm="This posts offer prompts now, and your replies send real eBay offers.",
    ),
)


def _escape(text: str) -> str:
    """Make arbitrary text (an exception message) safe inside mrkdwn."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _brief(exc: Exception) -> str:
    text = str(exc)
    if len(text) > MAX_ERROR_CHARS:
        text = text[: MAX_ERROR_CHARS - 1] + "…"
    return _escape(text)


def _slack_date(when: datetime | None) -> str:
    """A timestamp Slack renders in the viewer's own timezone."""
    if when is None:
        return "at an unknown time"
    fallback = when.strftime("%Y-%m-%d %H:%M UTC")
    return f"<!date^{int(when.timestamp())}^{{date_short_pretty}} at {{time}}|{fallback}>"


def _duration(start: datetime, end: datetime) -> str:
    minutes, seconds = divmod(max(0, int((end - start).total_seconds())), 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def status_line(execution: Execution | None) -> str:
    """One mrkdwn line for a job's latest execution."""
    if execution is None:
        return "_Never run_"
    if execution.state == "pending":
        return f"⏳ Starting (requested {_slack_date(execution.created_at)})"
    if execution.state == "running":
        return f"⏳ Running since {_slack_date(execution.started_at)}"
    finished = _slack_date(execution.completed_at)
    if execution.state == "succeeded":
        start = execution.started_at or execution.created_at
        took = (
            f" · took {_duration(start, execution.completed_at)}"
            if start and execution.completed_at
            else ""
        )
        return f"✅ Succeeded {finished}{took}"
    if execution.state == "cancelled":
        return f"⚪ Cancelled {finished}"
    logs = f" · <{execution.log_uri}|logs>" if execution.log_uri else ""
    return f"❌ Failed {finished}{logs}"


def _private_metadata(statuses: dict[str, str], as_of: datetime) -> str:
    # A tab of failures with long log links could outgrow Slack's cap; then
    # the next click re-reads every status instead.
    doc = json.dumps({"statuses": statuses, "as_of": as_of.isoformat()})
    if len(doc) > MAX_PRIVATE_METADATA:
        doc = json.dumps({"as_of": as_of.isoformat()})
    return doc


def _stored_statuses(view: dict[str, Any]) -> tuple[dict[str, str] | None, datetime | None]:
    """The statuses (and their time) a published tab was rendered with."""
    try:
        doc = json.loads(view.get("private_metadata") or "{}")
        statuses = doc.get("statuses")
        as_of = datetime.fromisoformat(doc["as_of"]) if doc.get("as_of") else None
    except (TypeError, ValueError, AttributeError):
        return None, None
    if not isinstance(statuses, dict):
        return None, as_of
    return {str(k): str(v) for k, v in statuses.items()}, as_of


def home_view(
    commands: tuple[HomeCommand, ...],
    statuses: dict[str, str],
    *,
    settings: Settings,
    as_of: datetime,
    notice: str | None = None,
) -> dict[str, Any]:
    """The Home tab for an allowed user: a button and status line per command."""
    channels = {
        "notify_channel": f"<#{settings.slack.notify_channel}>",
        "offers_channel": f"<#{settings.slack.offers_channel or settings.slack.pricing_channel}>",
    }
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Shoebox commands"}}
    ]
    if notice:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": notice}})
    blocks.append({"type": "divider"})
    for cmd in commands:
        button: dict[str, Any] = {
            "type": "button",
            "text": {"type": "plain_text", "text": "Run"},
            "action_id": f"{RUN_ACTION_PREFIX}{cmd.job}",
            "value": cmd.job,
        }
        if cmd.confirm:
            button["confirm"] = {
                "title": {"type": "plain_text", "text": f"{cmd.label}?"},
                "text": {"type": "mrkdwn", "text": cmd.confirm},
                "confirm": {"type": "plain_text", "text": "Run"},
                "deny": {"type": "plain_text", "text": "Cancel"},
            }
        status = statuses.get(cmd.job, "⚠️ _Status unavailable_")
        text = f"*{cmd.label}*\n{cmd.description.format(**channels)}\n{status}"
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": text}, "accessory": button}
        )
    blocks += [
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Refresh"},
                    "action_id": REFRESH_ACTION_ID,
                }
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Statuses as of {_slack_date(as_of)}. Each button starts the "
                        "Cloud Run job of the same name."
                    ),
                }
            ],
        },
    ]
    return {
        "type": "home",
        "private_metadata": _private_metadata(statuses, as_of),
        "blocks": blocks,
    }


def not_allowed_view(user_id: str, *, empty_allowlist: bool) -> dict[str, Any]:
    """The Home tab for everyone else. Shows their member ID, which is what
    goes into ``slack.allowed_user_ids``."""
    why = (
        "Nobody can run commands yet: `slack.allowed_user_ids` in the cloud app.yaml is empty."
        if empty_allowlist
        else "You aren't in `slack.allowed_user_ids`, so there's nothing to run here."
    )
    return {
        "type": "home",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Shoebox commands"}},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{why}\nYour Slack member ID is `{user_id}`."},
            },
        ],
    }


def _json(raw: str | bytes) -> dict[str, Any]:
    try:
        doc = json.loads(raw)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def create_slack_router(
    *,
    signing_secret: str,
    slack: Any,
    commands: tuple[HomeCommand, ...] = HOME_COMMANDS,
) -> APIRouter:
    """``/slack/events`` and ``/slack/interactions``. ``slack`` is a
    ``slack_sdk.WebClient`` holding the Commands app's bot token; Cloud Run
    calls go through the API's own job runner."""
    verifier = SignatureVerifier(signing_secret)
    by_job = {cmd.job: cmd for cmd in commands}
    # One Cloud Run call at a time: the discovery client's HTTP object isn't
    # thread-safe, and holding it across check-then-start means a double
    # click can't start a job twice.
    cloud_run_lock = threading.Lock()

    async def verified_body(request: Request) -> bytes:
        body = await request.body()
        if not verifier.is_valid(
            body,
            request.headers.get("X-Slack-Request-Timestamp"),
            request.headers.get("X-Slack-Signature"),
        ):
            raise HTTPException(status_code=401, detail="invalid Slack signature")
        return body

    def allowed(ctx: ApiContext, user_id: str) -> bool:
        return user_id in ctx.settings.slack.allowed_user_ids

    def fetch_statuses(ctx: ApiContext) -> dict[str, str]:
        statuses: dict[str, str] = {}
        for cmd in commands:
            try:
                statuses[cmd.job] = status_line(ctx.job_runner.latest_execution(cmd.job))
            except Exception:
                logger.exception("could not read the latest execution of %s", cmd.job)
                statuses[cmd.job] = "⚠️ _Status unavailable_"
        return statuses

    def publish(user_id: str, view: dict[str, Any]) -> None:
        # Slack ignores the response body, so a failed publish is only logged.
        try:
            slack.views_publish(user_id=user_id, view=view)
        except Exception:
            logger.exception("views.publish for %s failed", user_id)

    def publish_home(ctx: ApiContext, user_id: str) -> None:
        if not allowed(ctx, user_id):
            empty = not ctx.settings.slack.allowed_user_ids
            publish(user_id, not_allowed_view(user_id, empty_allowlist=empty))
            return
        with cloud_run_lock:
            statuses = fetch_statuses(ctx)
        view = home_view(commands, statuses, settings=ctx.settings, as_of=ctx.clock())
        publish(user_id, view)

    def start_job(
        ctx: ApiContext, cmd: HomeCommand, statuses: dict[str, str], user_id: str
    ) -> str | None:
        """Start ``cmd``'s job unless an execution is already in flight. Updates
        its status line; returns a notice for anything but a clean start."""
        try:
            latest = ctx.job_runner.latest_execution(cmd.job)
        except Exception as exc:
            logger.exception("could not read the latest execution of %s", cmd.job)
            return (
                f"⚠️ Didn't start *{cmd.label}*: couldn't check whether it's already "
                f"running ({_brief(exc)})."
            )
        if latest is not None and latest.active:
            statuses[cmd.job] = status_line(latest)
            return f"⚠️ *{cmd.label}* is already running, so I didn't start another."
        try:
            execution = ctx.job_runner.run_job(cmd.job)
        except Exception as exc:
            logger.exception("could not start %s", cmd.job)
            statuses[cmd.job] = status_line(latest)
            return f"❌ Couldn't start *{cmd.label}*: {_brief(exc)}"
        logger.info("Slack user %s started %s (execution %s)", user_id, cmd.job, execution)
        name = (execution or "").rsplit("/", 1)[-1]
        statuses[cmd.job] = f"⏳ Started {_slack_date(ctx.clock())}" + (
            f" · `{name}`" if name else ""
        )
        return None

    def run_from_click(ctx: ApiContext, user_id: str, job: str, view: dict[str, Any]) -> None:
        statuses, as_of = _stored_statuses(view)
        with cloud_run_lock:
            if statuses is None or as_of is None:
                statuses, as_of = fetch_statuses(ctx), ctx.clock()
            cmd = by_job.get(job)
            if cmd is None:
                # A tab published before a deploy that dropped this button.
                notice = "⚠️ That button is out of date. Here's the current list."
            else:
                notice = start_job(ctx, cmd, statuses, user_id)
        publish(
            user_id,
            home_view(commands, statuses, settings=ctx.settings, as_of=as_of, notice=notice),
        )

    router = APIRouter(prefix="/slack", include_in_schema=False)

    @router.post("/events")
    async def events(request: Request) -> Response:
        payload = _json(await verified_body(request))
        if payload.get("type") == "url_verification":
            return JSONResponse({"challenge": payload.get("challenge", "")})
        event = payload.get("event") or {}
        # Slack retries an event it got no answer to within 3 seconds (a cold
        # start); every delivery just republishes the tab, so retries are harmless.
        if event.get("type") == "app_home_opened" and event.get("tab") == "home":
            await run_in_threadpool(publish_home, get_ctx(request), str(event.get("user", "")))
        return Response(status_code=200)

    @router.post("/interactions")
    async def interactions(request: Request) -> Response:
        form = parse_qs((await verified_body(request)).decode("utf-8"))
        payload = _json((form.get("payload") or ["{}"])[0])
        if payload.get("type") != "block_actions":
            return Response(status_code=200)
        ctx = get_ctx(request)
        user_id = str((payload.get("user") or {}).get("id", ""))
        if not allowed(ctx, user_id):
            logger.warning("Ignoring a Slack click from unauthorized user %s", user_id)
            return Response(status_code=200)
        for action in payload.get("actions") or []:
            action_id = str(action.get("action_id", ""))
            if action_id == REFRESH_ACTION_ID:
                await run_in_threadpool(publish_home, ctx, user_id)
            elif action_id.startswith(RUN_ACTION_PREFIX):
                job = action_id.removeprefix(RUN_ACTION_PREFIX)
                await run_in_threadpool(
                    run_from_click, ctx, user_id, job, payload.get("view") or {}
                )
        return Response(status_code=200)

    return router
