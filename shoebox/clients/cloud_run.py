"""Start a Cloud Run job execution (the API kicking off ``create-listings``).

Uses the Cloud Run Admin API v2 through the discovery client that the
Google Calendar integration already depends on, so no extra library. The
caller's identity needs ``run.jobs.run`` on the job (``roles/run.invoker``,
granted by ``bindings:`` in deploy/jobs.yaml).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class CloudRunError(RuntimeError):
    pass


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
