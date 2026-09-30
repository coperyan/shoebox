"""scripts/deploy_cloud_run.py: jobs.yaml -> gcloud commands.

Everything here exercises the pure renderers; nothing touches gcloud or the
network. The script is loaded by path because scripts/ is a plain directory,
not a package (the same technique test_search_state uses).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "deploy_cloud_run", REPO_ROOT / "scripts" / "deploy_cloud_run.py"
)
deploy = importlib.util.module_from_spec(_spec)
# dataclasses look the defining module up in sys.modules (Python 3.13), so
# register it before executing.
sys.modules[_spec.name] = deploy
_spec.loader.exec_module(deploy)

GCLOUD = "gcloud"
PROJECT = "proj"

DEFAULTS = {
    "region": "us-central1",
    "image": "{region}-docker.pkg.dev/{project}/shoebox/shoebox:latest",
    "runner_service_account": "runner@{project}.iam.gserviceaccount.com",
    "scheduler_service_account": "sched@{project}.iam.gserviceaccount.com",
    "timezone": "America/Los_Angeles",
    "env": {"SHOEBOX_LOG_DIR": "-", "SHOEBOX_CONFIG_PATH": "/secrets/app/app.yaml"},
    "secrets": {
        "/secrets/app/app.yaml": "shoebox-app-yaml:latest",
        "/secrets/ebay-rest/ebay_rest.json": "shoebox-ebay-rest:latest",
    },
    "cpu": "1",
    "memory": "1Gi",
    "timeout": "1800s",
    "max_retries": 0,
    "attempt_deadline": "320s",
    "scheduler_retries": 0,
    "labels": {"app": "shoebox"},
    "enabled": True,
}

JOB = {
    "name": "sync-orders",
    "run": "sync-orders",
    "description": "Sync orders.",
    "schedule": "0 4 * * *",
}


def flag(argv: list[str], name: str) -> str:
    """Value following a ``--flag`` in an argv list."""
    return argv[argv.index(name) + 1]


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "jobs.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Config loading and validation
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_real_config_loads(self):
        defaults, jobs = deploy.load_config(REPO_ROOT / "deploy" / "jobs.yaml")
        assert {j["name"] for j in jobs} >= {"sync-orders", "end-oos-listings"}
        assert defaults["max_retries"] == 0
        # Public repo: the committed file must not pin a project.
        assert "project" not in defaults

    def test_duplicate_names_rejected(self, tmp_path):
        path = write_config(
            tmp_path,
            "jobs:\n  - {name: a, run: a, schedule: manual}\n  - {name: a, run: a, schedule: manual}\n",
        )
        with pytest.raises(deploy.ConfigError, match="duplicate"):
            deploy.load_config(path)

    def test_underscore_name_rejected(self, tmp_path):
        path = write_config(tmp_path, "jobs:\n  - {name: end_oos_listings, run: x}\n")
        with pytest.raises(deploy.ConfigError, match="lowercase letters, digits and hyphens"):
            deploy.load_config(path)

    def test_bad_cron_rejected(self, tmp_path):
        path = write_config(tmp_path, 'jobs:\n  - {name: a, run: a, schedule: "0 4 * *"}\n')
        with pytest.raises(deploy.ConfigError, match="5-field cron"):
            deploy.load_config(path)

    def test_manual_schedule_accepted(self, tmp_path):
        path = write_config(tmp_path, "jobs:\n  - {name: a, run: a, schedule: manual}\n")
        _, jobs = deploy.load_config(path)
        assert jobs[0]["schedule"] == "manual"

    def test_missing_run_rejected(self, tmp_path):
        path = write_config(tmp_path, "jobs:\n  - {name: a, schedule: manual}\n")
        with pytest.raises(deploy.ConfigError, match="`run`"):
            deploy.load_config(path)

    def test_select_unknown_job(self):
        with pytest.raises(deploy.ConfigError, match="unknown job"):
            deploy.select_jobs([JOB], ["nope"])

    def test_select_keeps_config_order(self):
        jobs = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
        assert [j["name"] for j in deploy.select_jobs(jobs, ["c", "a"])] == ["a", "c"]


class TestHelpers:
    def test_expand_fills_placeholders_recursively(self):
        value = {"img": "{region}/{project}", "list": ["{project}"], "n": 3}
        out = deploy.expand(value, project="p", region="r")
        assert out == {"img": "r/p", "list": ["p"], "n": 3}

    def test_expand_unknown_placeholder(self):
        with pytest.raises(deploy.ConfigError, match="unknown placeholder"):
            deploy.expand("{bucket}", project="p", region="r")

    def test_job_args_string_is_shell_split(self):
        job = {"run": 'sync-active-listing-details --workers 8 --note "two words"'}
        assert deploy.job_args(job) == [
            "sync-active-listing-details",
            "--workers",
            "8",
            "--note",
            "two words",
        ]

    def test_job_args_list_is_verbatim(self):
        assert deploy.job_args({"run": ["a", "--b", 8]}) == ["a", "--b", "8"]

    def test_gcloud_list_plain(self):
        assert deploy.gcloud_list(["a", "b"]) == "a,b"

    def test_gcloud_list_switches_delimiter_on_comma(self):
        assert deploy.gcloud_list(["a,b", "c"]) == "^|^a,b|c"

    def test_gcloud_list_rejects_unrenderable(self):
        with pytest.raises(deploy.ConfigError):
            deploy.gcloud_list(["a,b", "c|d"])

    def test_secrets_same_directory_rejected(self):
        defaults = {
            "secrets": {"/secrets/app.yaml": "a:latest", "/secrets/ebay_rest.json": "b:latest"}
        }
        with pytest.raises(deploy.ConfigError, match="share the directory"):
            deploy.secrets_for(JOB, defaults)

    def test_secrets_relative_path_rejected(self):
        with pytest.raises(deploy.ConfigError, match="absolute"):
            deploy.secrets_for(JOB, {"secrets": {"secrets/app.yaml": "a:latest"}})

    def test_parse_scheduler_list(self):
        out = "projects/p/locations/r/jobs/sync-orders\tENABLED\nprojects/p/locations/r/jobs/x\tPAUSED\n\n"
        assert deploy.parse_scheduler_list(out) == {"sync-orders": "ENABLED", "x": "PAUSED"}


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


class TestRenderJobDeploy:
    def render(self, job=JOB, defaults=DEFAULTS, image=None):
        return deploy.render_job_deploy(
            job, defaults, project=PROJECT, image=image, gcloud=GCLOUD
        ).argv

    def test_shape(self):
        argv = self.render()
        assert argv[:5] == [GCLOUD, "run", "jobs", "deploy", "sync-orders"]
        assert "--quiet" in argv
        assert flag(argv, "--project") == PROJECT
        assert flag(argv, "--region") == "us-central1"
        assert flag(argv, "--args") == "sync-orders"
        assert flag(argv, "--max-retries") == "0"
        assert flag(argv, "--tasks") == "1"
        assert flag(argv, "--parallelism") == "1"
        # ENTRYPOINT stays `shoebox`; only args are supplied.
        assert "--command" not in argv

    def test_placeholders_expanded(self):
        argv = self.render()
        assert flag(argv, "--image") == "us-central1-docker.pkg.dev/proj/shoebox/shoebox:latest"
        assert flag(argv, "--service-account") == "runner@proj.iam.gserviceaccount.com"

    def test_image_override_wins(self):
        argv = self.render(image="us-central1-docker.pkg.dev/proj/shoebox/shoebox:abc123")
        assert flag(argv, "--image").endswith(":abc123")

    def test_per_job_override_beats_defaults(self):
        job = {**JOB, "memory": "2Gi", "timeout": "3600s", "region": "us-east1"}
        argv = self.render(job)
        assert flag(argv, "--memory") == "2Gi"
        assert flag(argv, "--task-timeout") == "3600s"
        assert flag(argv, "--region") == "us-east1"
        assert flag(argv, "--image").startswith("us-east1-docker.pkg.dev/proj/")

    def test_env_and_secrets_sorted_for_stable_output(self):
        argv = self.render()
        assert (
            flag(argv, "--set-env-vars")
            == "SHOEBOX_CONFIG_PATH=/secrets/app/app.yaml,SHOEBOX_LOG_DIR=-"
        )
        assert flag(argv, "--set-secrets") == (
            "/secrets/app/app.yaml=shoebox-app-yaml:latest,"
            "/secrets/ebay-rest/ebay_rest.json=shoebox-ebay-rest:latest"
        )
        assert flag(argv, "--labels") == "app=shoebox"

    def test_job_env_merges_over_defaults(self):
        job = {**JOB, "env": {"SHOEBOX_LOG_DIR": "logs", "TZ": "UTC"}}
        assert flag(self.render(job), "--set-env-vars") == (
            "SHOEBOX_CONFIG_PATH=/secrets/app/app.yaml,SHOEBOX_LOG_DIR=logs,TZ=UTC"
        )

    def test_multi_arg_run_is_comma_joined(self):
        job = {**JOB, "run": ["orders-awaiting-shipment", "--pull-order", "--message"]}
        assert flag(self.render(job), "--args") == "orders-awaiting-shipment,--pull-order,--message"

    def test_missing_image_is_config_error(self):
        defaults = {k: v for k, v in DEFAULTS.items() if k != "image"}
        with pytest.raises(deploy.ConfigError, match="no image"):
            self.render(defaults=defaults)


class TestRenderScheduler:
    def test_invoker_binding(self):
        cmd = deploy.render_invoker_binding(JOB, DEFAULTS, project=PROJECT, gcloud=GCLOUD)
        assert cmd.argv[:5] == [GCLOUD, "run", "jobs", "add-iam-policy-binding", "sync-orders"]
        assert flag(cmd.argv, "--member") == "serviceAccount:sched@proj.iam.gserviceaccount.com"
        assert flag(cmd.argv, "--role") == "roles/run.invoker"

    @pytest.mark.parametrize(("exists", "verb"), [(False, "create"), (True, "update")])
    def test_upsert_verb(self, exists, verb):
        cmd = deploy.render_scheduler_upsert(
            JOB, DEFAULTS, project=PROJECT, exists=exists, gcloud=GCLOUD
        )
        assert cmd.argv[:6] == [GCLOUD, "scheduler", "jobs", verb, "http", "sync-orders"]

    def test_upsert_flags(self):
        argv = deploy.render_scheduler_upsert(
            JOB, DEFAULTS, project=PROJECT, exists=False, gcloud=GCLOUD
        ).argv
        assert flag(argv, "--location") == "us-central1"
        assert flag(argv, "--schedule") == "0 4 * * *"
        assert flag(argv, "--time-zone") == "America/Los_Angeles"
        assert flag(argv, "--uri") == (
            "https://run.googleapis.com/v2/projects/proj/locations/us-central1/jobs/sync-orders:run"
        )
        assert flag(argv, "--http-method") == "POST"
        assert flag(argv, "--oauth-service-account-email") == "sched@proj.iam.gserviceaccount.com"
        assert flag(argv, "--attempt-deadline") == "320s"
        assert flag(argv, "--max-retry-attempts") == "0"
        assert flag(argv, "--description") == "Sync orders."
        assert "--quiet" in argv

    def test_create_and_update_take_identical_flags(self):
        create = deploy.render_scheduler_upsert(
            JOB, DEFAULTS, project=PROJECT, exists=False, gcloud=GCLOUD
        ).argv
        update = deploy.render_scheduler_upsert(
            JOB, DEFAULTS, project=PROJECT, exists=True, gcloud=GCLOUD
        ).argv
        assert create[6:] == update[6:]

    @pytest.mark.parametrize(
        ("enabled", "state", "verb"),
        [
            (False, "ENABLED", "pause"),
            (True, "PAUSED", "resume"),
            (True, "ENABLED", None),
            (False, "PAUSED", None),
            (True, None, None),
            (False, None, None),  # plan() never passes None for a new schedule
        ],
    )
    def test_state_transitions(self, enabled, state, verb):
        job = {**JOB, "enabled": enabled}
        cmd = deploy.render_scheduler_state(
            job, DEFAULTS, project=PROJECT, current_state=state, gcloud=GCLOUD
        )
        if verb is None:
            assert cmd is None
        else:
            assert cmd.argv[:5] == [GCLOUD, "scheduler", "jobs", verb, "sync-orders"]


# ---------------------------------------------------------------------------
# Planning and execution
# ---------------------------------------------------------------------------


def verbs(commands) -> list[str]:
    return [" ".join(c.argv[1:4]) for c in commands]


class TestPlan:
    def test_full_mode_order_for_new_job(self):
        commands = deploy.plan(
            [JOB], DEFAULTS, project=PROJECT, image=None, mode="all", existing={}, gcloud=GCLOUD
        )
        assert verbs(commands) == [
            "run jobs deploy",
            "run jobs add-iam-policy-binding",
            "scheduler jobs create",
        ]

    def test_existing_schedule_is_updated_and_resumed(self):
        commands = deploy.plan(
            [JOB],
            DEFAULTS,
            project=PROJECT,
            image=None,
            mode="all",
            existing={"sync-orders": "PAUSED"},
            gcloud=GCLOUD,
        )
        assert verbs(commands)[-2:] == ["scheduler jobs update", "scheduler jobs resume"]

    def test_jobs_only(self):
        commands = deploy.plan(
            [JOB], DEFAULTS, project=PROJECT, image=None, mode="jobs", existing=None, gcloud=GCLOUD
        )
        assert verbs(commands) == ["run jobs deploy"]

    def test_scheduler_only(self):
        commands = deploy.plan(
            [JOB],
            DEFAULTS,
            project=PROJECT,
            image=None,
            mode="scheduler",
            existing={},
            gcloud=GCLOUD,
        )
        assert verbs(commands) == ["run jobs add-iam-policy-binding", "scheduler jobs create"]

    def test_new_disabled_schedule_is_created_then_paused(self):
        job = {**JOB, "enabled": False}
        commands = deploy.plan(
            [job],
            DEFAULTS,
            project=PROJECT,
            image=None,
            mode="scheduler",
            existing={},
            gcloud=GCLOUD,
        )
        assert verbs(commands) == [
            "run jobs add-iam-policy-binding",
            "scheduler jobs create",
            "scheduler jobs pause",
        ]

    def test_manual_job_gets_no_schedule(self):
        manual = {**JOB, "name": "orders-awaiting-shipment", "schedule": "manual"}
        commands = deploy.plan(
            [manual], DEFAULTS, project=PROJECT, image=None, mode="all", existing={}, gcloud=GCLOUD
        )
        assert verbs(commands) == ["run jobs deploy"]


class TestExecute:
    def test_dry_run_prints_shell_and_runs_nothing(self, monkeypatch, capsys):
        def boom(*args, **kwargs):
            raise AssertionError("subprocess.run must not be called in a dry run")

        monkeypatch.setattr(deploy.subprocess, "run", boom)
        cmd = deploy.Command("say hi", [GCLOUD, "run", "jobs", "deploy", "x", "--args", "a b"])
        deploy.execute([cmd], dry_run=True)
        out = capsys.readouterr().out.splitlines()
        assert out == ["# say hi", "gcloud run jobs deploy x --args 'a b'"]

    def test_real_run_calls_each_command(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(deploy.subprocess, "run", lambda argv, check: calls.append(list(argv)))
        cmds = [deploy.Command("a", ["gcloud", "a"]), deploy.Command("b", ["gcloud", "b"])]
        deploy.execute(cmds, dry_run=False)
        assert calls == [["gcloud", "a"], ["gcloud", "b"]]


class TestMain:
    def test_dry_run_from_cli_needs_no_gcloud(self, monkeypatch, capsys):
        monkeypatch.setattr(deploy.shutil, "which", lambda name: None)
        monkeypatch.setattr(
            deploy.sys, "argv", ["deploy_cloud_run.py", "--dry-run", "--only", "sync-orders"]
        )
        assert deploy.main() == 0
        out = capsys.readouterr().out
        assert "gcloud run jobs deploy sync-orders" in out
        assert "gcloud scheduler jobs create http sync-orders" in out
        assert "--project PROJECT" in out

    def test_list(self, monkeypatch, capsys):
        monkeypatch.setattr(deploy.sys, "argv", ["deploy_cloud_run.py", "--list"])
        assert deploy.main() == 0
        assert "sync-orders" in capsys.readouterr().out

    def test_unknown_only_is_an_error(self, monkeypatch, capsys):
        monkeypatch.setattr(deploy.sys, "argv", ["deploy_cloud_run.py", "--list", "--only", "zz"])
        assert deploy.main() == 1
        assert "unknown job" in capsys.readouterr().err
