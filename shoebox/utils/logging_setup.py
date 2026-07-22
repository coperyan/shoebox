"""Central logging configuration for CLI and pipeline entrypoints.

Call ``setup_logging()`` once from an entrypoint (the CLI does this for every
subcommand). Importing library modules never configures logging.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

_FORMAT = "%(asctime)s - %(module)-s - %(levelname)-2s - %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(*, log_dir: str | Path = "logs", level: int = logging.INFO) -> None:
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

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(
        log_path / datetime.now().strftime("%Y-%m-%d_%H_%M.log"), encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    root.setLevel(level)
