"""build_indexes / build_metadata_payload / metadata_version."""

from __future__ import annotations

import numpy as np
import pandas as pd

from shoebox.transforms.queue_enrichment import normalize_nulls
from shoebox.transforms.queue_indexes import (
    build_indexes,
    build_metadata_payload,
    cardno_sort_key,
    json_scalar,
    metadata_version,
    set_year_for,
    subset_sort_key,
)


def _frames():
    cdf = pd.DataFrame(
        [
            # set, subset, type, derived, number, player, team, note, note_check, year, ids
            ("2024 Set", "Base", "Base", "10", "10", "P Ten", "T", None, None, "2024", "c10", "s1"),
            ("2024 Set", "Base", "Base", "2", "2", "P Two", "T", "RC", "RC", "2024", "c2", "s1"),
            (
                "2024 Set",
                "Base",
                "Base",
                "1A",
                "1A",
                "P OneA",
                None,
                None,
                None,
                "2024",
                "c1a",
                "s1",
            ),
            ("2024 Set", "Base", "Base", None, "", "Blank", None, None, None, "2024", "cx", "s1"),
            ("2024 Set", "Base", "Base", "2", "2", "Dup Two", "T", None, None, "2024", "c2b", "s1"),
            ("2024 Set", "Stars", "Insert", "A", "A", "P A", "T", "SP", None, "2024", "ca", "s2"),
            ("2024 Set", "Autos", "Auto", "1", "1", "P Auto", "T", None, None, "2024", "au1", "s5"),
            ("2023 Set", "Base", "Base", "1", "1", "Old", "T", None, None, "2023", "o1", "s3"),
            (pd.NA, "Base", "Base", "1", "1", "NA set", "T", None, None, "2023", "na", "s4"),
        ],
        columns=[
            "set_name",
            "subset_name",
            "subset_type",
            "derived_card_number",
            "card_number",
            "player",
            "team",
            "note",
            "note_check",
            "set_year",
            "card_id",
            "subset_id",
        ],
    )
    cdf["card_number"] = cdf["card_number"].replace({"": pd.NA})
    pdf = pd.DataFrame(
        [
            ("2024 Set", "Base", "Gold", 50.0, "SP", "p1"),
            ("2024 Set", "Base", "Blue", np.nan, None, "p2"),
            ("2024 Set", "Base", "Gold", 50.0, "dup", "p1b"),
        ],
        columns=[
            "set_name",
            "subset_name",
            "parallel_variety",
            "print_run",
            "other_note",
            "parallel_id",
        ],
    )
    return cdf, pdf


def test_cardno_sort_key_buckets():
    keys = [cardno_sort_key(x) for x in ["10", "2", "1A", "a", "", None, float("nan")]]
    assert sorted(keys) == [keys[1], keys[0], keys[2], keys[3], keys[4], keys[5], keys[6]]
    assert cardno_sort_key("a") == cardno_sort_key("A")


def test_build_indexes_order_and_na_skipped():
    cdf, pdf = _frames()
    idx = build_indexes(cdf, pdf)
    assert idx["sets"] == ["2023 Set", "2024 Set"]
    assert idx["subsets_by_set"]["2024 Set"] == ["Base", "Autos", "Stars"], "Base first, then A-Z"
    assert idx["cardnos_by_set_subset"][("2024 Set", "Base")] == ["2", "10", "1A"]
    assert idx["parallels_by_set_subset"][("2024 Set", "Base")] == ["Blue", "Gold"]
    assert ("<NA>", "Base") not in idx["cardnos_by_set_subset"]


def test_metadata_payload_shape():
    cdf, pdf = _frames()
    payload = build_metadata_payload(cdf, pdf)
    assert payload["counts"] == {"checklist_rows": 9, "parallel_rows": 3, "sets": 2}
    sets = {s["set_name"]: s for s in payload["sets"]}
    assert list(sets) == ["2023 Set", "2024 Set"]
    assert [s["set_year"] for s in payload["sets"]] == ["2023", "2024"]
    assert [x["subset_name"] for x in sets["2024 Set"]["subsets"]] == ["Base", "Autos", "Stars"]
    base = sets["2024 Set"]["subsets"][0]
    assert base["subset_name"] == "Base"
    assert [c["card_number"] for c in base["cards"]] == ["2", "10", "1A"]
    two = base["cards"][0]
    assert two["player"] == "P Two"  # first row wins, like cg.iloc[0]
    assert two["note_check"] == "RC"
    assert two["set_year"] == "2024"
    assert two["subset_type"] == "Base"
    assert two["team"] == "T"
    assert base["cards"][2]["team"] is None
    assert [p["parallel_variety"] for p in base["parallels"]] == ["Blue", "Gold"]
    gold = base["parallels"][1]
    assert gold["print_run"] == 50 and isinstance(gold["print_run"], int)
    assert gold["other_note"] == "SP"
    assert base["parallels"][0]["print_run"] is None
    assert sets["2024 Set"]["subsets"][2]["parallels"] == []
    # JSON-serializable end to end
    import json

    json.dumps(payload)


def test_metadata_version_stable_and_sensitive():
    cdf, pdf = _frames()
    v1 = metadata_version(build_metadata_payload(cdf, pdf))
    v2 = metadata_version(build_metadata_payload(cdf, pdf))
    assert v1 == v2 and len(v1) == 16
    cdf2 = cdf.copy()
    cdf2.loc[0, "player"] = "Changed"
    assert metadata_version(build_metadata_payload(cdf2, pdf)) != v1


def test_json_scalar():
    assert json_scalar(pd.NA) is None
    assert json_scalar(np.int64(5)) == 5
    assert json_scalar(50.0) == 50
    assert json_scalar(" x ") == "x"
    assert json_scalar("<NA>") is None
    assert json_scalar(np.bool_(True)) is True


def test_normalize_nulls_handles_pd_na():
    out = normalize_nulls({"a": pd.NA, "b": "", "c": float("nan"), "d": 1})
    assert out == {"a": None, "b": None, "c": None, "d": 1}


def test_subset_sort_key_puts_base_first_case_insensitively():
    names = ["Stars", "base", "Autos", "Base", "Baseball Legends"]
    assert sorted(names, key=subset_sort_key) == [
        "Base",
        "base",
        "Autos",
        "Baseball Legends",
        "Stars",
    ]


def test_set_year_for_prefers_checklist_year_then_name_prefix():
    g = pd.DataFrame({"set_year": [None, 2019.0, "2020"]})
    assert set_year_for("2024 Something", g) == "2019"
    assert set_year_for("2024 Something", pd.DataFrame({"set_year": [None, pd.NA]})) == "2024"
    assert set_year_for(" 1999 Vintage", pd.DataFrame({"player": ["x"]})) == "1999"
    assert set_year_for("Topps Heritage", pd.DataFrame({"set_year": [None]})) is None
    assert set_year_for("20245 Not a year", pd.DataFrame({"set_year": [None]})) is None
