from typing import Any

import pytest

from shoebox.clients.ebay_rest.stores import (
    StoresClient,
    flatten_store_categories,
    parse_store_categories,
)

TREE_RESPONSE = {
    "store_categories": [
        {
            "category_id": "1",
            "category_name": "Baseball",
            "order": 0,
            "level": 1,
            "children_categories": [
                {
                    "category_id": "2",
                    "category_name": "Rookies",
                    "order": 0,
                    "level": 2,
                    "children_categories": None,
                }
            ],
        },
        {
            "category_id": "3",
            "category_name": "Football",
            "order": 1,
            "level": 1,
            "children_categories": None,
        },
    ]
}


class FakeApi:
    """Records sell_stores_* calls and replays queued responses."""

    def __init__(self, responses: dict[str, Any] | None = None):
        self.calls: list[tuple[str, tuple, dict[str, Any]]] = []
        self.responses = responses or {}

    def __getattr__(self, name: str):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            result = self.responses.get(name)
            if isinstance(result, Exception):
                raise result
            if callable(result):
                return result(*args, **kwargs)
            return result

        return call


class FakeSession:
    def __init__(self, api: FakeApi):
        self.api = api
        self.Error = Exception


def build_client(responses=None):
    api = FakeApi(responses)
    return StoresClient(FakeSession(api)), api


def test_parse_store_categories_handles_nesting_and_none_children():
    tree = parse_store_categories(TREE_RESPONSE["store_categories"])

    assert [c["name"] for c in tree] == ["Baseball", "Football"]
    assert tree[0]["children"][0]["category_id"] == "2"
    assert tree[0]["level"] == 1
    assert tree[1]["children"] == []


def test_get_store_categories_reads_store_categories_key():
    client, api = build_client({"sell_stores_get_store_categories": TREE_RESPONSE})

    tree = client.get_store_categories()

    assert api.calls[-1][0] == "sell_stores_get_store_categories"
    assert [c["name"] for c in tree] == ["Baseball", "Football"]


def test_get_store_categories_tolerates_empty_response():
    client, _ = build_client({"sell_stores_get_store_categories": None})
    assert client.get_store_categories() == []


def test_flatten_tracks_parent_and_path():
    flat = flatten_store_categories(parse_store_categories(TREE_RESPONSE["store_categories"]))

    assert [c["category_id"] for c in flat] == ["1", "2", "3"]
    assert flat[1]["parent_id"] == "1"
    assert flat[1]["path"] == ("Baseball", "Rookies")
    assert flat[2]["parent_id"] is None


def test_add_store_category_top_level_omits_parent():
    client, api = build_client()
    client.add_store_category("Vintage")

    name, _, kwargs = api.calls[-1]
    assert name == "sell_stores_add_store_category"
    assert kwargs["body"] == {"categoryName": "Vintage"}
    assert kwargs["content_type"] == "application/json"


def test_add_store_category_with_parent_and_listing_destination():
    client, api = build_client()
    client.add_store_category("Rookies", parent_category_id=1, listing_destination_category_id=9)

    body = api.calls[-1][2]["body"]
    assert body == {
        "categoryName": "Rookies",
        "destinationParentCategoryId": "1",
        "listingDestinationCategoryId": "9",
    }


def test_add_store_category_rejects_blank_name():
    client, _ = build_client()
    with pytest.raises(ValueError):
        client.add_store_category("   ")


def test_delete_store_category_sends_id_as_path_param():
    client, api = build_client()
    client.delete_store_category(11, listing_destination_category_id=99)

    name, _, kwargs = api.calls[-1]
    assert name == "sell_stores_delete_store_category"
    assert kwargs["category_id"] == "11"
    assert kwargs["body"] == {"listingDestinationCategoryId": "99"}


def test_move_store_category_passes_body_then_content_type_positionally():
    client, api = build_client()
    client.move_store_category(11, parent_category_id=5)

    name, args, _ = api.calls[-1]
    assert name == "sell_stores_move_store_category"
    # The generated signature is move_store_category(body, content_type).
    assert args[0] == {"categoryId": "11", "destinationParentCategoryId": "5"}
    assert args[1] == "application/json"


def test_move_to_top_level_omits_parent():
    client, api = build_client()
    client.move_store_category(11)

    assert api.calls[-1][1][0] == {"categoryId": "11"}


def test_rename_store_category():
    client, api = build_client()
    client.rename_store_category(11, "Graded Cards")

    name, _, kwargs = api.calls[-1]
    assert name == "sell_stores_rename_store_category"
    assert kwargs["category_id"] == "11"
    assert kwargs["body"] == {"categoryName": "Graded Cards"}


def test_invalid_category_id_rejected():
    client, _ = build_client()
    with pytest.raises(ValueError):
        client.delete_store_category("not-an-id")


def test_batch_delete_issues_one_call_per_id():
    client, api = build_client()
    results = client.delete_store_categories([11, 12, 13])

    assert len(api.calls) == 3
    assert [r["status"] for r in results] == ["ok", "ok", "ok"]
    assert [r["category_id"] for r in results] == [11, 12, 13]


def test_batch_stops_on_first_error_by_default():
    boom = RuntimeError("gone")
    client, api = build_client({"sell_stores_delete_store_category": boom})

    with pytest.raises(RuntimeError):
        client.delete_store_categories([11, 12, 13])

    # Stopped after the first failure rather than plowing through.
    assert len(api.calls) == 1


def test_batch_collects_failures_when_not_stopping():
    boom = RuntimeError("already deleted with its parent")
    client, api = build_client({"sell_stores_delete_store_category": boom})

    results = client.delete_store_categories([11, 12], stop_on_error=False)

    assert len(api.calls) == 2
    assert [r["status"] for r in results] == ["error", "error"]
    assert "already deleted" in results[0]["error"]


def test_add_store_categories_allows_per_item_parent_override():
    client, api = build_client()
    client.add_store_categories(
        ["Top", {"name": "Child", "parent_category_id": 5}], parent_category_id=None
    )

    assert api.calls[0][2]["body"] == {"categoryName": "Top"}
    assert api.calls[1][2]["body"] == {
        "categoryName": "Child",
        "destinationParentCategoryId": "5",
    }


def test_add_store_categories_rejects_spec_without_name():
    client, _ = build_client()
    with pytest.raises(ValueError):
        client.add_store_categories([{"parent_category_id": 1}])


def test_rename_store_categories_from_mapping():
    client, api = build_client()
    results = client.rename_store_categories({11: "A", 12: "B"})

    assert [c[2]["category_id"] for c in api.calls] == ["11", "12"]
    assert [c[2]["body"]["categoryName"] for c in api.calls] == ["A", "B"]
    assert all(r["status"] == "ok" for r in results)


def test_get_store_task_unwraps_task():
    client, _ = build_client(
        {"sell_stores_get_store_task": {"task": {"id": "7", "status": "COMPLETED"}}}
    )

    assert client.get_store_task("7")["status"] == "COMPLETED"


def test_get_failed_store_tasks_filters():
    client, _ = build_client(
        {
            "sell_stores_get_store_tasks": {
                "task": [
                    {"id": "1", "status": "COMPLETED"},
                    {"id": "2", "status": "FAILED", "message": "bad parent"},
                ]
            }
        }
    )

    failed = client.get_failed_store_tasks()

    assert [t["id"] for t in failed] == ["2"]
