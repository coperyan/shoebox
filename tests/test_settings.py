import pytest
import yaml
from pydantic import ValidationError

from shoebox.settings import Settings, get_settings


def _example_raw():
    with open("configs/app.example.yml") as f:
        return yaml.safe_load(f)


def test_example_config_loads_with_store_section():
    s = get_settings("configs/app.example.yml")
    assert s.store.category_id == "261328"
    assert s.store.merchant_location_key == "YOUR_LOCATION_KEY"
    assert s.store.policies.payment_policy_id == "0000000000"


def test_missing_store_section_falls_back_to_defaults(tmp_path):
    raw = _example_raw()
    raw.pop("store", None)
    cfg = tmp_path / "no_store.yaml"
    cfg.write_text(yaml.safe_dump(raw))

    s = get_settings(str(cfg))
    # Placeholder defaults from StoreSettings kick in.
    assert s.store.name == "Your Store"
    assert s.store.merchant_location_key == "REPLACE_MERCHANT_LOCATION_KEY"


def test_unknown_top_level_key_is_rejected():
    raw = _example_raw()
    raw["not_a_real_section"] = {"x": 1}
    try:
        Settings(**raw)
    except Exception:
        return
    raise AssertionError("extra top-level key should have been rejected")


def test_queue_uri_default_none():
    assert Settings(**_example_raw()).paths.queue_uri is None


@pytest.mark.parametrize(
    "value",
    ["gs://bucket/queue/listing_queue_enriched.jsonl", "gs://b/x.jsonl"],
)
def test_queue_uri_accepts_object_uri(value):
    raw = _example_raw()
    raw["paths"]["queue_uri"] = value
    assert Settings(**raw).paths.queue_uri == value


@pytest.mark.parametrize("value", ["gs://bucket", "gs://bucket/", "exports/x.jsonl", "gs:///x"])
def test_queue_uri_rejects_non_object(value):
    raw = _example_raw()
    raw["paths"]["queue_uri"] = value
    with pytest.raises(ValidationError):
        Settings(**raw)


def test_onedrive_and_cloud_run_sections_default():
    s = Settings(**_example_raw())
    assert s.onedrive.enabled is False
    assert s.onedrive.authority.endswith("/consumers")
    assert s.onedrive.scopes == ["Files.ReadWrite"]  # the job moves listed scans
    assert s.onedrive.listed_subfolder == "Listed"
    assert s.onedrive.token_cache_uri is None
    assert (s.cloud_run.region, s.cloud_run.listings_job) == ("us-central1", "create-listings")

    raw = _example_raw()
    del raw["onedrive"]
    del raw["cloud_run"]
    assert Settings(**raw).onedrive.enabled is False  # old configs still load


def test_onedrive_enabled_and_token_uri_validation():
    raw = _example_raw()
    raw["onedrive"].update({"client_id": "abc", "scans_folder": "Scans"})
    raw["onedrive"]["token_cache_uri"] = "gs://b/queue/onedrive_token_cache.json"
    s = Settings(**raw)
    assert s.onedrive.enabled is True
    raw["onedrive"]["token_cache_uri"] = "gs://b"
    with pytest.raises(ValidationError):
        Settings(**raw)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Scans", "Scans"),
        (" /Pictures/Card Scans/ ", "Pictures/Card Scans"),
        ("Pictures//Card Scans", "Pictures/Card Scans"),
    ],
)
def test_normalize_onedrive_folder(value, expected):
    from shoebox.settings import normalize_onedrive_folder

    assert normalize_onedrive_folder(value) == expected


@pytest.mark.parametrize(
    "value", ["", " / ", "a/../b", "./a", "a\\b", "a:b", "a|b", "a\nb", "x" * 401]
)
def test_normalize_onedrive_folder_rejects(value):
    from shoebox.settings import normalize_onedrive_folder

    with pytest.raises(ValueError):
        normalize_onedrive_folder(value)
