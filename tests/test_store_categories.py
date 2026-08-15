from typing import Any

import pytest

from shoebox.clients.ebay_legacy import (
    STORE_ROOT_CATEGORY_ID,
    eBayLegacyClient,
    flatten_store_categories,
)


class FakeClient(eBayLegacyClient):
    """Client that records Trading calls instead of hitting eBay."""

    def __init__(self, payload: dict[str, Any] | None = None):
        self.token = "TOKEN"
        self.calls: list[dict[str, Any]] = []
        self.payload = payload or {"Ack": "Success"}

    def _trading_call(self, *, call_name, body, site_id, compatibility_level, timeout=60):
        self.calls.append({"call_name": call_name, "body": body})
        return self.payload


def last_body(client: FakeClient) -> str:
    return client.calls[-1]["body"]


def test_get_store_categories_parses_nested_tree():
    client = FakeClient(
        {
            "Ack": "Success",
            "Store": {
                "CustomCategories": {
                    "CustomCategory": [
                        {
                            "CategoryID": "1",
                            "Name": "Baseball",
                            "Order": "0",
                            "ChildCategory": {
                                "CategoryID": "2",
                                "Name": "Rookies",
                                "Order": "0",
                            },
                        },
                        {"CategoryID": "3", "Name": "Football", "Order": "1"},
                    ]
                }
            },
        }
    )

    tree = client.get_store_categories()

    assert client.calls[-1]["call_name"] == "GetStore"
    assert "<CategoryStructureOnly>true</CategoryStructureOnly>" in last_body(client)
    assert [c["name"] for c in tree] == ["Baseball", "Football"]
    assert tree[0]["children"][0]["category_id"] == "2"
    assert tree[1]["children"] == []


def test_get_store_categories_optional_filters():
    client = FakeClient()
    client.get_store_categories(root_category_id=42, level_limit=2)

    body = last_body(client)
    assert "<LevelLimit>2</LevelLimit>" in body
    assert "<RootCategoryID>42</RootCategoryID>" in body


def test_flatten_store_categories_tracks_parent_and_path():
    tree = [
        {
            "category_id": "1",
            "name": "Baseball",
            "order": "0",
            "children": [{"category_id": "2", "name": "Rookies", "order": "0", "children": []}],
        }
    ]

    flat = flatten_store_categories(tree)

    assert [c["category_id"] for c in flat] == ["1", "2"]
    assert flat[1]["parent_id"] == "1"
    assert flat[1]["level"] == 2
    assert flat[1]["path"] == ("Baseball", "Rookies")


def test_add_store_categories_defaults_to_top_level():
    client = FakeClient(
        {
            "Ack": "Success",
            "Status": "Complete",
            "TaskID": "0",
            "CustomCategory": [{"CategoryID": "10", "Name": "Vintage"}],
        }
    )

    result = client.add_store_categories(["Vintage", {"name": "Modern", "order": 2}])

    body = last_body(client)
    assert client.calls[-1]["call_name"] == "SetStoreCategories"
    assert "<Action>Add</Action>" in body
    assert (
        f"<DestinationParentCategoryID>{STORE_ROOT_CATEGORY_ID}"
        "</DestinationParentCategoryID>" in body
    )
    assert "<CustomCategory><Name>Vintage</Name></CustomCategory>" in body
    assert "<CustomCategory><Name>Modern</Name><Order>2</Order></CustomCategory>" in body
    assert result["categories"][0]["category_id"] == "10"
    assert result["status"] == "Complete"


def test_add_store_categories_escapes_names():
    client = FakeClient()
    client.add_store_categories(["Cards & Sets"])

    assert "<Name>Cards &amp; Sets</Name>" in last_body(client)


def test_add_store_categories_rejects_spec_without_name():
    client = FakeClient()
    with pytest.raises(ValueError):
        client.add_store_categories([{"order": 1}])


def test_delete_store_categories_with_item_destination():
    client = FakeClient()
    client.delete_store_categories([11, "12"], item_destination_category_id=99)

    body = last_body(client)
    assert "<Action>Delete</Action>" in body
    assert "<ItemDestinationCategoryID>99</ItemDestinationCategoryID>" in body
    assert "<CustomCategory><CategoryID>11</CategoryID></CustomCategory>" in body
    assert "<CustomCategory><CategoryID>12</CategoryID></CustomCategory>" in body
    # Delete must not send a destination parent.
    assert "DestinationParentCategoryID" not in body.replace("ItemDestinationCategoryID", "")


def test_move_store_categories_under_new_parent():
    client = FakeClient()
    client.move_store_categories([11], parent_category_id=5)

    body = last_body(client)
    assert "<Action>Move</Action>" in body
    assert "<DestinationParentCategoryID>5</DestinationParentCategoryID>" in body


def test_rename_store_category():
    client = FakeClient()
    client.rename_store_category(11, "Graded Cards")

    body = last_body(client)
    assert "<Action>Rename</Action>" in body
    assert (
        "<CustomCategory><CategoryID>11</CategoryID><Name>Graded Cards</Name></CustomCategory>"
        in body
    )


def test_invalid_category_id_rejected():
    client = FakeClient()
    with pytest.raises(ValueError):
        client.delete_store_categories(["not-an-id"])


def test_empty_category_list_rejected():
    client = FakeClient()
    with pytest.raises(ValueError):
        client.delete_store_categories([])


def test_get_store_category_update_status():
    client = FakeClient({"Ack": "Success", "Status": "InProgress"})

    result = client.get_store_category_update_status(777)

    assert client.calls[-1]["call_name"] == "GetStoreCategoryUpdateStatus"
    assert "<TaskID>777</TaskID>" in last_body(client)
    assert result["status"] == "InProgress"


def test_wait_for_store_category_update_polls_until_complete(monkeypatch):
    client = FakeClient()
    statuses = iter(["Pending", "InProgress", "Complete"])
    monkeypatch.setattr(
        client,
        "get_store_category_update_status",
        lambda task_id, **kw: {"task_id": task_id, "status": next(statuses)},
    )
    monkeypatch.setattr("shoebox.clients.ebay_legacy.time.sleep", lambda _: None)

    assert client.wait_for_store_category_update(1)["status"] == "Complete"


def test_wait_for_store_category_update_times_out(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(
        client,
        "get_store_category_update_status",
        lambda task_id, **kw: {"task_id": task_id, "status": "Pending"},
    )
    monkeypatch.setattr("shoebox.clients.ebay_legacy.time.sleep", lambda _: None)

    with pytest.raises(TimeoutError):
        client.wait_for_store_category_update(1, timeout=0)
