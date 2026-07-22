"""Shared test configuration.

Point the settings loader at the committed example config so tests that build
listing payloads have deterministic, placeholder store values to read.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "configs" / "app.yaml.example"

# Must be set before shoebox.settings is imported anywhere.
os.environ.setdefault("SHOEBOX_CONFIG_PATH", str(EXAMPLE_CONFIG))


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Ensure each test sees a fresh settings load."""
    from shoebox.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
