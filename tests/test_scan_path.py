"""Tests for scan-filename resolution in ui.helpers.handle_image_path."""

from __future__ import annotations

import yaml

from shoebox.settings import get_settings
from shoebox.ui.helpers import handle_image_path


def _point_settings_at(tmp_path, monkeypatch, **path_overrides):
    """Write a config with the given paths overrides and make the no-arg
    get_settings() (used by handle_image_path) load it."""
    with open("configs/app.example.yml") as f:
        raw = yaml.safe_load(f)
    raw["paths"].update(path_overrides)
    cfg = tmp_path / "app.yaml"
    cfg.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("SHOEBOX_CONFIG_PATH", str(cfg))
    get_settings.cache_clear()


def test_default_scan_naming(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, scans_dir="scans")
    assert handle_image_path("123").replace("\\", "/") == "scans/SCAN_0123.jpg"


def test_custom_prefix_padding_and_extension(tmp_path, monkeypatch):
    _point_settings_at(
        tmp_path,
        monkeypatch,
        scans_dir="scans",
        scan_prefix="IMG",
        scan_number_padding=5,
        scan_extension="png",  # leading dot added automatically
    )
    assert handle_image_path("123").replace("\\", "/") == "scans/IMG00123.png"


def test_padding_disabled(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, scans_dir="scans", scan_number_padding=0)
    assert handle_image_path("123").replace("\\", "/") == "scans/SCAN_123.jpg"


def test_existing_path_is_returned_as_is(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, scans_dir="scans")
    real = tmp_path / "already_here.png"
    real.write_text("x")
    assert handle_image_path(str(real)) == str(real)
