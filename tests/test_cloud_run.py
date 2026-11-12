"""clients/cloud_run.py: starting jobs and reading back their latest execution."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from shoebox.clients.cloud_run import CloudRunError, CloudRunJobs, Execution

NAME = "projects/p/locations/us-central1/jobs/sync-orders/executions/sync-orders-tr9xr"


def _execution(**over):
    base = {
        "name": NAME,
        "createTime": "2026-11-11T12:00:37.503680Z",
        "startTime": "2026-11-11T12:00:42.068060Z",
        "completionTime": "2026-11-11T12:03:41.255917123Z",
        "taskCount": 1,
        "succeededCount": 1,
        "logUri": "https://console.cloud.google.com/logs/viewer?project=p",
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


class TestExecutionFromApi:
    def test_succeeded(self):
        e = Execution.from_api(_execution())
        assert e.state == "succeeded" and not e.active
        assert e.name == "sync-orders-tr9xr"
        assert e.started_at == datetime(2026, 11, 11, 12, 0, 42, 68060, tzinfo=UTC)
        assert e.completed_at.second == 41  # nanoseconds parse
        assert e.log_uri.startswith("https://console.cloud.google.com/")

    def test_pending_and_running_are_active(self):
        pending = Execution.from_api(
            _execution(startTime=None, completionTime=None, succeededCount=None)
        )
        running = Execution.from_api(
            _execution(completionTime=None, succeededCount=None, runningCount=1)
        )
        assert (pending.state, running.state) == ("pending", "running")
        assert pending.active and running.active

    @pytest.mark.parametrize(
        "over, state",
        [
            ({"succeededCount": None, "failedCount": 1}, "failed"),
            ({"succeededCount": None, "cancelledCount": 1}, "cancelled"),
            # Completed with no counts at all (the container never started).
            ({"succeededCount": None}, "failed"),
            ({"taskCount": 2}, "failed"),
        ],
    )
    def test_finished_states(self, over, state):
        assert Execution.from_api(_execution(**over)).state == state


class TestLatestExecution:
    def _jobs(self, response=None, error=None):
        service = MagicMock()
        lister = service.projects.return_value.locations.return_value.jobs.return_value
        lister = lister.executions.return_value.list
        if error:
            lister.return_value.execute.side_effect = error
        else:
            lister.return_value.execute.return_value = response
        return CloudRunJobs("p", "us-central1", service=service), lister

    def test_reads_the_newest_execution(self):
        jobs, lister = self._jobs({"executions": [_execution()], "nextPageToken": "x"})
        latest = jobs.latest_execution("sync-orders")
        lister.assert_called_once_with(
            parent="projects/p/locations/us-central1/jobs/sync-orders", pageSize=1
        )
        assert latest.name == "sync-orders-tr9xr" and latest.state == "succeeded"

    def test_never_run_is_none(self):
        jobs, _ = self._jobs({})
        assert jobs.latest_execution("sync-orders") is None

    def test_api_errors_become_cloud_run_errors(self):
        jobs, _ = self._jobs(error=RuntimeError("403"))
        with pytest.raises(CloudRunError, match="could not list executions"):
            jobs.latest_execution("sync-orders")
