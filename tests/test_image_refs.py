"""materialize_image: local paths, bare scan numbers and gs:// uploads."""

from __future__ import annotations

import pytest

from shoebox.utils.image_refs import classify_image_ref, materialize_image
from tests.test_scan_path import _point_settings_at


def test_classify():
    assert classify_image_ref("") == "blank"
    assert classify_image_ref("   ") == "blank"
    assert classify_image_ref(None) == "blank"
    assert classify_image_ref("gs://b/uploads/x.jpg") == "gs"
    assert classify_image_ref("123") == "path"
    assert classify_image_ref("C:/scans/SCAN_0001.jpg") == "path"


def test_blank_raises(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, scans_dir=str(tmp_path / "scans"))
    with pytest.raises(ValueError):
        materialize_image("")


def test_existing_path_unchanged(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, scans_dir=str(tmp_path / "scans"))
    real = tmp_path / "front.png"
    real.write_bytes(b"x")
    assert materialize_image(str(real)) == real


def test_scan_number_uses_convention(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, scans_dir="scans")
    assert str(materialize_image("123")).replace("\\", "/") == "scans/SCAN_0123.jpg"


def test_gs_uri_downloads_into_app_uploads(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, scans_dir=str(tmp_path / "scans"))
    calls = []

    def fake_download(uri, dest):
        calls.append((uri, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpeg")
        return dest

    out = materialize_image("gs://bucket/uploads/2026-10/abc.jpg", download=fake_download)
    assert out == tmp_path / "scans" / "app_uploads" / "abc.jpg"
    assert out.read_bytes() == b"jpeg"
    assert calls == [("gs://bucket/uploads/2026-10/abc.jpg", out)]

    # Already present -> not downloaded again (idempotent re-runs).
    out2 = materialize_image("gs://bucket/uploads/2026-10/abc.jpg", download=fake_download)
    assert out2 == out
    assert len(calls) == 1


def test_gs_download_failure_propagates(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, scans_dir=str(tmp_path / "scans"))

    def failing(uri, dest):
        raise FileNotFoundError(f"Missing object: {uri}")

    with pytest.raises(FileNotFoundError):
        materialize_image("gs://bucket/uploads/missing.jpg", download=failing)


def test_gs_bucket_only_rejected(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, scans_dir=str(tmp_path / "scans"))
    with pytest.raises(ValueError):
        materialize_image("gs://bucket", download=lambda u, d: d)
