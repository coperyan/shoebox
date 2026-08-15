import yaml

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
