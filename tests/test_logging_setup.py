"""Log destination resolution.

A container collects stdout itself and throws its filesystem away, so file
logging has to be switchable off -- without changing what a local run does.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from shoebox.utils.logging_setup import _resolve_log_dir, setup_logging


@pytest.fixture
def bare_root():
    """Hand back a root logger with no handlers, restoring pytest's afterwards.

    ``setup_logging`` no-ops when the root logger already has handlers, and
    pytest's own capture handler is installed for the duration of the call
    phase -- i.e. after fixture setup finishes. So the clearing has to happen
    inside the test body, which is what this fixture is for.
    """
    root = logging.getLogger()
    saved = root.handlers[:]
    level = root.level

    def clear() -> logging.Logger:
        root.handlers.clear()
        return root

    yield clear
    root.handlers.clear()
    root.handlers.extend(saved)
    root.setLevel(level)


class TestResolveLogDir:
    def test_default_is_logs(self, monkeypatch):
        monkeypatch.delenv("SHOEBOX_LOG_DIR", raising=False)
        assert _resolve_log_dir(None) == Path("logs")

    def test_env_overrides_the_default(self, monkeypatch):
        monkeypatch.setenv("SHOEBOX_LOG_DIR", "/var/log/shoebox")
        assert _resolve_log_dir(None) == Path("/var/log/shoebox")

    @pytest.mark.parametrize("value", ["", "-", "  ", " - "])
    def test_console_only_markers(self, monkeypatch, value):
        monkeypatch.setenv("SHOEBOX_LOG_DIR", value)
        assert _resolve_log_dir(None) is None

    def test_explicit_argument_beats_the_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SHOEBOX_LOG_DIR", "-")
        assert _resolve_log_dir(tmp_path) == tmp_path


class TestSetupLogging:
    def test_local_default_still_writes_a_file(self, monkeypatch, tmp_path, bare_root):
        monkeypatch.delenv("SHOEBOX_LOG_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        root = bare_root()

        setup_logging()
        assert any(isinstance(h, logging.FileHandler) for h in root.handlers)
        assert list((tmp_path / "logs").glob("*.log"))

    def test_console_only_creates_no_directory(self, monkeypatch, tmp_path, bare_root):
        monkeypatch.setenv("SHOEBOX_LOG_DIR", "-")
        monkeypatch.chdir(tmp_path)
        root = bare_root()

        setup_logging()
        assert not any(isinstance(h, logging.FileHandler) for h in root.handlers)
        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
        assert not (tmp_path / "logs").exists()

    def test_explicit_dir_still_wins_in_a_container(self, monkeypatch, tmp_path, bare_root):
        monkeypatch.setenv("SHOEBOX_LOG_DIR", "-")
        root = bare_root()

        setup_logging(log_dir=tmp_path / "explicit")
        assert any(isinstance(h, logging.FileHandler) for h in root.handlers)
        assert list((tmp_path / "explicit").glob("*.log"))

    def test_existing_handlers_are_left_alone(self, monkeypatch, tmp_path, bare_root):
        monkeypatch.chdir(tmp_path)
        root = bare_root()
        sentinel = logging.NullHandler()
        root.addHandler(sentinel)

        setup_logging()
        assert root.handlers == [sentinel]
