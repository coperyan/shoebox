"""The Slack Commands app's Home tab: signed requests in, Cloud Run jobs out."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest
import yaml
from fastapi.testclient import TestClient
from slack_sdk.signature import SignatureVerifier

from shoebox.api.app import SLACK_BOT_TOKEN_ENV, SLACK_SIGNING_SECRET_ENV, create_app
from shoebox.api.slack_home import HOME_COMMANDS
from shoebox.clients.cloud_run import Execution
from tests.test_api import KEY, FakeClock, FakeGCS, _frames, _settings

SECRET = "slack-signing-secret"
ME = "U0ME"
OTHER = "U0OTHER"
JOBS = [cmd.job for cmd in HOME_COMMANDS]


def _at(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, 15, 11, minute, second, tzinfo=UTC)


SUCCEEDED = Execution("sync-orders-aaaaa", "succeeded", _at(0), _at(0, 5), _at(2, 18))
FAILED = Execution(
    "send-offers-bbbbb", "failed", _at(1), _at(1, 5), _at(3), log_uri="https://logs.example/x"
)
RUNNING = Execution("sync-watch-list-ccccc", "running", _at(4), _at(4, 9), None)


class FakeRunner:
    def __init__(self):
        self.latest: dict[str, Execution | None] = {}
        self.fail_status: set[str] = set()
        self.fail_run = False
        self.status_calls: list[str] = []
        self.started: list[str] = []

    def latest_execution(self, name):
        self.status_calls.append(name)
        if name in self.fail_status:
            raise RuntimeError("could not list executions: 403")
        return self.latest.get(name)

    def run_job(self, name):
        self.started.append(name)
        if self.fail_run:
            raise RuntimeError("<HttpError 403 run.jobs.run denied>")
        return f"projects/p/locations/r/jobs/{name}/executions/{name}-abc12"


class FakeSlack:
    def __init__(self, fail=False):
        self.fail = fail
        self.published: list[tuple[str, dict]] = []

    def views_publish(self, *, user_id, view):
        if self.fail:
            raise RuntimeError("invalid_auth")
        self.published.append((user_id, view))


def _app(*, allowed=(ME,), runner=None, slack=None, **kwargs):
    settings = _settings()
    settings.slack.notify_channel = "C0NOTIFY"
    settings.slack.offers_channel = "C0OFFERS"
    settings.slack.allowed_user_ids = list(allowed)
    return create_app(
        settings=settings,
        api_key=KEY,
        metadata_loader=_frames,
        gcs=FakeGCS(),
        clock=FakeClock(),
        job_runner=runner,
        slack_client=slack,
        **kwargs,
    )


@pytest.fixture
def slack_env():
    runner = FakeRunner()
    runner.latest = {"sync-orders": SUCCEEDED, "send-offers": FAILED, "sync-watch-list": RUNNING}
    runner.fail_status = {"sync-active-listings"}
    slack = FakeSlack()
    client = TestClient(_app(runner=runner, slack=slack, slack_signing_secret=SECRET))
    return client, runner, slack


def _post(client, path, body, content_type, *, secret=SECRET, timestamp=None):
    timestamp = str(int(time.time())) if timestamp is None else timestamp
    signature = SignatureVerifier(secret).generate_signature(timestamp=timestamp, body=body)
    return client.post(
        path,
        content=body,
        headers={
            "Content-Type": content_type,
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
        },
    )


def send_event(client, event, **kwargs):
    body = json.dumps({"type": "event_callback", "event": event})
    return _post(client, "/slack/events", body, "application/json", **kwargs)


def open_home(client, user=ME, **kwargs):
    return send_event(client, {"type": "app_home_opened", "user": user, "tab": "home"}, **kwargs)


def click(client, action_id, *, user=ME, view=None):
    payload = {
        "type": "block_actions",
        "user": {"id": user},
        "actions": [{"action_id": action_id, "type": "button"}],
        "view": view or {},
    }
    body = urlencode({"payload": json.dumps(payload)})
    return _post(client, "/slack/interactions", body, "application/x-www-form-urlencoded")


def sections(view) -> list[str]:
    return [b["text"]["text"] for b in view["blocks"] if b["type"] == "section"]


def line_for(view, label) -> str:
    return next(t for t in sections(view) if t.startswith(f"*{label}*"))


def action_ids(view) -> list[str]:
    ids = []
    for block in view["blocks"]:
        if block.get("accessory"):
            ids.append(block["accessory"]["action_id"])
        for element in block.get("elements", []):
            if element.get("type") == "button":
                ids.append(element["action_id"])
    return ids


# ---- wiring ----


def test_no_slack_routes_without_a_signing_secret(monkeypatch):
    monkeypatch.delenv(SLACK_SIGNING_SECRET_ENV, raising=False)
    client = TestClient(_app(runner=FakeRunner()))
    assert client.post("/slack/events", json={"type": "url_verification"}).status_code == 404


def test_signing_secret_needs_a_bot_token(monkeypatch):
    monkeypatch.setenv(SLACK_SIGNING_SECRET_ENV, SECRET)
    monkeypatch.delenv(SLACK_BOT_TOKEN_ENV, raising=False)
    with pytest.raises(RuntimeError, match=SLACK_BOT_TOKEN_ENV):
        _app(runner=FakeRunner())


def test_secrets_from_env_are_stripped(monkeypatch):
    monkeypatch.setenv(SLACK_SIGNING_SECRET_ENV, SECRET + "\n")
    monkeypatch.setenv(SLACK_BOT_TOKEN_ENV, "xoxb-test\n")
    client = TestClient(_app(runner=FakeRunner()))
    body = json.dumps({"type": "url_verification", "challenge": "abc"})
    assert _post(client, "/slack/events", body, "application/json").json() == {"challenge": "abc"}


def test_home_commands_are_deployed_jobs():
    with open("deploy/jobs.yaml") as f:
        raw = yaml.safe_load(f)
    jobs = {job["name"]: job for job in raw["jobs"]}
    assert set(JOBS) <= set(jobs)
    assert (
        jobs["orders-awaiting-shipment"]["run"] == "orders-awaiting-shipment --pull-order --message"
    )
    api = next(svc for svc in raw["services"] if svc["name"] == "shoebox-api")
    assert {SLACK_SIGNING_SECRET_ENV, SLACK_BOT_TOKEN_ENV} <= set(api["env_secrets"])


# ---- signatures ----


@pytest.mark.parametrize("path", ["/slack/events", "/slack/interactions"])
def test_unsigned_or_missigned_requests_are_rejected(slack_env, path):
    client, runner, slack = slack_env
    body = json.dumps({"type": "url_verification", "challenge": "abc"})
    assert client.post(path, content=body).status_code == 401
    assert _post(client, path, body, "application/json", secret="wrong").status_code == 401
    stale = str(int(time.time()) - 600)
    assert _post(client, path, body, "application/json", timestamp=stale).status_code == 401
    assert runner.started == [] and slack.published == []


def test_url_verification_echoes_the_challenge(slack_env):
    client, *_ = slack_env
    body = json.dumps({"type": "url_verification", "challenge": "3eZbrw1aB"})
    r = _post(client, "/slack/events", body, "application/json")
    assert r.status_code == 200
    assert r.json() == {"challenge": "3eZbrw1aB"}


# ---- the Home tab ----


def test_home_tab_lists_every_command_with_its_last_run(slack_env):
    client, runner, slack = slack_env
    assert open_home(client).status_code == 200

    assert runner.status_calls == JOBS
    [(user, view)] = slack.published
    assert user == ME and view["type"] == "home"
    assert action_ids(view) == [f"run:{job}" for job in JOBS] + ["refresh"]

    assert "<#C0NOTIFY>" in line_for(view, "Orders awaiting shipment")
    assert "_Never run_" in line_for(view, "Orders awaiting shipment")
    assert "✅ Succeeded" in line_for(view, "Sync orders")
    assert "took 2m 13s" in line_for(view, "Sync orders")
    assert "Status unavailable" in line_for(view, "Sync active listings")
    assert "⏳ Running since" in line_for(view, "Sync watch list")
    offers = line_for(view, "Send offers")
    assert "<#C0OFFERS>" in offers
    assert "❌ Failed" in offers and "<https://logs.example/x|logs>" in offers

    confirm = next(
        b["accessory"]["confirm"]
        for b in view["blocks"]
        if b.get("accessory", {}).get("value") == "send-offers"
    )
    assert "real eBay offers" in confirm["text"]["text"]
    stored = json.loads(view["private_metadata"])
    assert set(stored["statuses"]) == set(JOBS)


def test_someone_else_sees_their_member_id_and_no_buttons(slack_env):
    client, runner, slack = slack_env
    open_home(client, user=OTHER)
    [(user, view)] = slack.published
    assert user == OTHER
    assert action_ids(view) == []
    assert f"`{OTHER}`" in sections(view)[0]
    assert runner.status_calls == []


def test_an_empty_allowlist_lets_nobody_in():
    slack = FakeSlack()
    runner = FakeRunner()
    client = TestClient(_app(allowed=(), runner=runner, slack=slack, slack_signing_secret=SECRET))
    open_home(client)
    [(_, view)] = slack.published
    assert action_ids(view) == []
    assert "Nobody can run commands yet" in sections(view)[0]
    click(client, "run:sync-orders")
    assert runner.started == []


def test_other_events_and_tabs_are_ignored(slack_env):
    client, runner, slack = slack_env
    send_event(client, {"type": "app_home_opened", "user": ME, "tab": "messages"})
    send_event(client, {"type": "message", "user": ME, "text": "hi"})
    assert slack.published == [] and runner.status_calls == []


def test_a_failed_publish_still_answers_200():
    client = TestClient(
        _app(runner=FakeRunner(), slack=FakeSlack(fail=True), slack_signing_secret=SECRET)
    )
    assert open_home(client).status_code == 200


# ---- buttons ----


def _home_view(client, slack, runner):
    open_home(client)
    view = slack.published.pop()[1]
    runner.status_calls.clear()
    return view


def test_run_starts_the_job_and_keeps_the_other_statuses(slack_env):
    client, runner, slack = slack_env
    view = _home_view(client, slack, runner)

    assert click(client, "run:sync-orders", view=view).status_code == 200

    assert runner.started == ["sync-orders"]
    # Only the clicked job is re-read; the rest come from the stored view.
    assert runner.status_calls == ["sync-orders"]
    [(user, updated)] = slack.published
    assert user == ME
    orders = line_for(updated, "Sync orders")
    assert "⏳ Started" in orders and "`sync-orders-abc12`" in orders
    assert line_for(updated, "Send offers") == line_for(view, "Send offers")
    assert len(sections(updated)) == len(HOME_COMMANDS)  # no notice


def test_run_refuses_while_the_job_is_running(slack_env):
    client, runner, slack = slack_env
    view = _home_view(client, slack, runner)
    click(client, "run:sync-watch-list", view=view)
    assert runner.started == []
    [(_, updated)] = slack.published
    assert "already running" in sections(updated)[0]


def test_run_reports_a_start_failure(slack_env):
    client, runner, slack = slack_env
    runner.fail_run = True
    view = _home_view(client, slack, runner)
    click(client, "run:sync-orders", view=view)
    assert runner.started == ["sync-orders"]
    [(_, updated)] = slack.published
    notice = sections(updated)[0]
    assert "Couldn't start *Sync orders*" in notice
    assert "&lt;HttpError 403" in notice and "<HttpError" not in notice
    assert "✅ Succeeded" in line_for(updated, "Sync orders")


def test_run_does_not_start_when_the_status_check_fails(slack_env):
    client, runner, slack = slack_env
    view = _home_view(client, slack, runner)
    click(client, "run:sync-active-listings", view=view)
    assert runner.started == []
    [(_, updated)] = slack.published
    assert "couldn't check whether it's already running" in sections(updated)[0]


def test_run_without_stored_statuses_reads_them_all(slack_env):
    client, runner, slack = slack_env
    click(client, "run:orders-awaiting-shipment", view={})
    assert runner.started == ["orders-awaiting-shipment"]
    assert runner.status_calls == JOBS + ["orders-awaiting-shipment"]
    [(_, updated)] = slack.published
    assert "✅ Succeeded" in line_for(updated, "Sync orders")


def test_unknown_or_unauthorized_clicks_start_nothing(slack_env):
    client, runner, slack = slack_env
    view = _home_view(client, slack, runner)
    click(client, "run:sync-orders", user=OTHER, view=view)
    assert runner.started == [] and slack.published == []

    click(client, "run:rm-rf", view=view)
    assert runner.started == []
    [(_, updated)] = slack.published
    assert "out of date" in sections(updated)[0]


def test_refresh_rereads_every_status(slack_env):
    client, runner, slack = slack_env
    view = _home_view(client, slack, runner)
    runner.latest["sync-orders"] = RUNNING
    click(client, "refresh", view=view)
    assert runner.status_calls == JOBS
    [(_, updated)] = slack.published
    assert "⏳ Running since" in line_for(updated, "Sync orders")
