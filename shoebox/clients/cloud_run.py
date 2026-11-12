"""Start Cloud Run job executions and read back the latest one.

The API uses this to kick off ``create-listings``, and the Slack Home tab
(``api/slack_home.py``) to start a job from a button and show how its last
run went. Uses the Cloud Run Admin API v2 through the discovery client that
the Google Calendar integration already depends on, so no extra library. The
caller's identity needs ``run.jobs.run`` on the job (``roles/run.invoker``,
granted by ``bindings:`` in deploy/jobs.yaml), plus ``run.executions.list``
(``roles/run.viewer``) for ``latest_execution``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class CloudRunError(RuntimeError):
    pass


def _timestamp(value: str | None) -> datetime | None:
    # RFC 3339 with up to nanoseconds and a Z; fromisoformat takes both (3.11+).
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class Execution:
    """One job execution, reduced to what a status line needs."""

    name: str  # short form, e.g. sync-orders-tr9xr
    state: str  # pending | running | succeeded | failed | cancelled
    created_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    log_uri: str | None = None

    @property
    def active(self) -> bool:
        return self.state in ("pending", "running")

    @classmethod
    def from_api(cls, execution: dict[str, Any]) -> Execution:
        """Build from a v2 ``Execution`` resource. The API leaves zero counts
        out, and an execution has no ``completionTime`` until it ends."""
        started = _timestamp(execution.get("startTime"))
        completed = _timestamp(execution.get("completionTime"))
        if completed is None:
            state = "running" if started else "pending"
        elif execution.get("cancelledCount"):
            state = "cancelled"
        elif not execution.get("failedCount") and (execution.get("succeededCount") or 0) >= (
            execution.get("taskCount") or 1
        ):
            state = "succeeded"
        else:
            state = "failed"
        return cls(
            name=str(execution.get("name", "")).rsplit("/", 1)[-1],
            state=state,
            created_at=_timestamp(execution.get("createTime")),
            started_at=started,
            completed_at=completed,
            log_uri=execution.get("logUri"),
        )


class CloudRunJobs:
    def __init__(self, project: str, region: str, *, service: Any = None):
        self.project = project
        self.region = region
        self._service = service

    @property
    def service(self) -> Any:
        if self._service is None:
            from googleapiclient.discovery import build

            self._service = build("run", "v2", cache_discovery=False)
        return self._service

    def job_name(self, name: str) -> str:
        return f"projects/{self.project}/locations/{self.region}/jobs/{name}"

    def run_job(self, name: str) -> str | None:
        """Start one execution; returns its name when the API reports it."""
        full = self.job_name(name)
        try:
            operation = self.service.projects().locations().jobs().run(name=full, body={}).execute()
        except Exception as exc:  # googleapiclient.errors.HttpError and friends
            raise CloudRunError(f"could not start Cloud Run job {full}: {exc}") from exc
        execution = (operation or {}).get("metadata", {}).get("name")
        logger.info("Started Cloud Run job %s (execution %s)", full, execution)
        return execution

    def latest_execution(self, name: str) -> Execution | None:
        """The job's newest execution, or None if it has never run. The API
        lists executions newest first, so one is enough."""
        full = self.job_name(name)
        try:
            response = (
                self.service.projects()
                .locations()
                .jobs()
                .executions()
                .list(parent=full, pageSize=1)
                .execute()
            )
        except Exception as exc:
            raise CloudRunError(f"could not list executions of {full}: {exc}") from exc
        executions = (response or {}).get("executions") or []
        return Execution.from_api(executions[0]) if executions else None
