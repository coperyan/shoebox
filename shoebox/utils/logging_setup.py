"""Central logging configuration for CLI and pipeline entrypoints.

Call ``setup_logging()`` once from an entrypoint (the CLI does this for every
subcommand). Importing library modules never configures logging.

Environment variables:
- SHOEBOX_LOG_DIR: directory for the per-run log file (defaults to ``logs``).
  Set it to ``-`` (or empty) to log to the console only, which is what a
  container wants: its stdout/stderr is already collected by the platform, so a
  file handler writes a log nobody reads to a filesystem that is discarded when
  the execution ends.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

_FORMAT = "%(asctime)s - %(module)-s - %(levelname)-2s - %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Values of SHOEBOX_LOG_DIR that mean "console only".
_CONSOLE_ONLY = {"", "-"}


def _resolve_log_dir(log_dir: str | Path | None) -> Path | None:
    """Where the file handler should write, or None for console-only.

    An explicit ``log_dir=`` argument always wins, so a caller that asks for a
    directory still gets one.
    """
    if log_dir is not None:
        return Path(log_dir)
    env = os.getenv("SHOEBOX_LOG_DIR")
    if env is None:
        return Path("logs")
    if env.strip() in _CONSOLE_ONLY:
        return None
    return Path(env)


def setup_logging(*, log_dir: str | Path | None = None, level: int = logging.INFO) -> None:
    """Configure the root logger with a console handler and a timestamped log file.

    Safe to call more than once: if the root logger already has handlers,
    this is a no-op so embedding applications keep their own configuration.
    """
    root = logging.getLogger()
    if root.handlers:
        return

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    log_path = _resolve_log_dir(log_dir)
    if log_path is not None:
        log_path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            log_path / datetime.now().strftime("%Y-%m-%d_%H_%M.log"), encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.setLevel(level)
