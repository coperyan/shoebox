"""Sell Stores API: the eBay Store category tree.

Replaces the Trading-API (XML) implementation that previously lived in the
Trading client. This uses the same OAuth token as the rest of the
REST pipelines, so no Auth'n'Auth token is involved.

Requires the ``sell.stores`` OAuth scope on a token from the authorization
code grant flow. A token without it fails with HTTP 403 / errorId 1100 /
domain ACCESS; scopes are fixed at consent time, so the user token has to be
re-minted after adding the scope (see docs/setup.md and
scripts/refresh_ebay_token.py).

Note the calls here bypass the generated ``sell_stores_*`` wrappers: in
ebay_rest 1.1.4 those send an application token, which eBay always rejects for
this API with the same 403/1100 — see :meth:`StoresClient._invoke`.

Two shape differences from the Trading API are worth knowing:

* eBay's REST endpoints act on **one category per call**, so the batch helpers
  here loop. That makes partial failure possible — see ``stop_on_error``.
* The mutating calls are asynchronous. eBay returns the task URI in the
  ``Location`` response header rather than the body, and the swagger-generated
  client in ``ebay_rest`` surfaces neither, so there is no taskId to hold onto.
  Use :meth:`StoresClient.get_store_tasks` to see recent task outcomes, and
  re-read :meth:`StoresClient.get_store_categories` to pick up newly assigned
  IDs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .session import EbaySession

logger = logging.getLogger(__name__)

# eBay supports three levels of store categories.
MAX_CATEGORY_DEPTH = 3

# Terminal states reported by getStoreTask / getStoreTasks.
TASK_DONE_STATUSES = ("COMPLETED", "FAILED")


def _category_id_str(category_id: Any) -> str:
    """
    Normalize a store category id to its string form.

    Store category IDs are numeric strings; anything not integer-like is a
    caller mistake worth catching before it reaches the wire.
    """
    try:
        return str(int(str(category_id).strip()))
    except (TypeError, ValueError):
        raise ValueError(f"Invalid store category id: {category_id!r}") from None


def _clean_name(name: Any) -> str:
    text = str(name).strip()
    if not text:
        raise ValueError("store category name must not be blank")
    return text


def parse_store_categories(nodes: Any) -> list[dict[str, Any]]:
    """
    Convert StoreCategoryType nodes into a nested list of plain dicts.

    Shape: {"category_id", "name", "order", "level", "children"}. eBay nests
    subcategories under ``children_categories``.
    """
    out: list[dict[str, Any]] = []
    for cat in nodes or []:
        if not isinstance(cat, dict):
            continue
        out.append(
            {
                "category_id": cat.get("category_id"),
                "name": cat.get("category_name"),
                "order": cat.get("order"),
                "level": cat.get("level"),
                "children": parse_store_categories(cat.get("children_categories")),
            }
        )
    return out


def flatten_store_categories(
    categories: Iterable[dict[str, Any]],
    *,
    parent_id: str | None = None,
    path: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """
    Flatten the nested tree from ``get_store_categories`` depth-first.

    Each entry gains ``parent_id`` and ``path`` (names from the root). Useful
    when reorganizing: deleting a parent takes its children with it, so reverse
    this list to process deepest-first.
    """
    out: list[dict[str, Any]] = []
    for cat in categories:
        cat_path = (*path, cat.get("name") or "")
        out.append(
            {
                "category_id": cat.get("category_id"),
                "name": cat.get("name"),
                "order": cat.get("order"),
                "level": cat.get("level"),
                "parent_id": parent_id,
                "path": cat_path,
            }
        )
        out.extend(
            flatten_store_categories(
                cat.get("children") or [],
                parent_id=cat.get("category_id"),
                path=cat_path,
            )
        )
    return out


class StoresClient:
    def __init__(self, session: EbaySession):
        self.session = session
        self.api = session.api

    def _invoke(self, method: str, params: Any = None, **kwargs: Any) -> Any:
        """
        Call a Stores API method with the *user* access token.

        Workaround for ebay_rest 1.1.4 (latest at the time of writing): its
        generated ``sell_stores_*`` wrappers pass ``user_access_token=False``,
        sending an application token to an API that only accepts tokens from
        the authorization code grant flow. Application tokens can never carry
        ``sell.stores``, so every call through those wrappers fails with
        HTTP 403 / errorId 1100 regardless of the config. This re-issues the
        same call with the flag set correctly. Drop it (and go back to
        ``self.api.sell_stores_*``) once upstream fixes the flag.
        """
        from ebay_rest.api import sell_stores
        from ebay_rest.api.sell_stores.rest import ApiException as SellStoresException

        return self.api._method_single(
            sell_stores.Configuration,
            "/sell/stores/v1",
            sell_stores.StoreApi,
            sell_stores.ApiClient,
            method,
            SellStoresException,
            True,  # user access token — the fix
            ["sell.stores", "store"],
            params,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_store(self) -> dict[str, Any]:
        """Return store profile information (name, URL, description)."""
        return self._invoke("get_store")

    def get_store_categories(self) -> list[dict[str, Any]]:
        """
        Return the store's category tree, nested.

        Each node is {"category_id", "name", "order", "level", "children"}.
        Pass the result to `flatten_store_categories` for a flat view.
        """
        resp = self._invoke("get_store_categories") or {}
        return parse_store_categories(resp.get("store_categories"))

    def get_store_task(self, task_id: str) -> dict[str, Any]:
        """Return the status of a single async store task."""
        resp = self._invoke("get_store_task", str(task_id)) or {}
        return resp.get("task") or {}

    def get_store_tasks(self) -> list[dict[str, Any]]:
        """
        Return the status of recent async store tasks.

        eBay marks every task COMPLETED or FAILED once it reaches 24 hours.
        Since the client discards the taskId from mutating calls, this is how
        you confirm a restructure actually landed.
        """
        resp = self._invoke("get_store_tasks") or {}
        return resp.get("task") or []

    def get_failed_store_tasks(self) -> list[dict[str, Any]]:
        """Return only the recent store tasks that failed."""
        return [t for t in self.get_store_tasks() if t.get("status") == "FAILED"]

    # ------------------------------------------------------------------
    # Single-category writes
    # ------------------------------------------------------------------

    def add_store_category(
        self,
        name: str,
        *,
        parent_category_id: str | int | None = None,
        listing_destination_category_id: str | int | None = None,
    ) -> None:
        """
        Add one store category.

        Omit ``parent_category_id`` to add at the top level. eBay assigns the
        new category ID asynchronously — re-read `get_store_categories` to pick
        it up. ``listing_destination_category_id`` receives any listings the
        change displaces.
        """
        body: dict[str, Any] = {"categoryName": _clean_name(name)}
        if parent_category_id is not None:
            body["destinationParentCategoryId"] = _category_id_str(parent_category_id)
        if listing_destination_category_id is not None:
            body["listingDestinationCategoryId"] = _category_id_str(listing_destination_category_id)

        # Generated signature: add_store_category(content_type, body=...)
        self._invoke("add_store_category", "application/json", body=body)
        logger.info("Added store category name=%r parent=%s", name, parent_category_id)

    def delete_store_category(
        self,
        category_id: str | int,
        *,
        listing_destination_category_id: str | int | None = None,
    ) -> None:
        """
        Delete one store category, and its subcategories with it.

        Pass ``listing_destination_category_id`` to say where displaced listings
        should go.
        """
        body: dict[str, Any] = {}
        if listing_destination_category_id is not None:
            body["listingDestinationCategoryId"] = _category_id_str(listing_destination_category_id)

        # Generated signature: delete_store_category(category_id, body=...)
        self._invoke("delete_store_category", _category_id_str(category_id), body=body)
        logger.info("Deleted store category category_id=%s", category_id)

    def move_store_category(
        self,
        category_id: str | int,
        *,
        parent_category_id: str | int | None = None,
        listing_destination_category_id: str | int | None = None,
    ) -> None:
        """
        Move a category under a new parent. Omit the parent to promote it to the
        top level.
        """
        body: dict[str, Any] = {"categoryId": _category_id_str(category_id)}
        if parent_category_id is not None:
            body["destinationParentCategoryId"] = _category_id_str(parent_category_id)
        if listing_destination_category_id is not None:
            body["listingDestinationCategoryId"] = _category_id_str(listing_destination_category_id)

        # Generated signature: move_store_category(body, content_type) — positional tuple.
        self._invoke("move_store_category", (body, "application/json"))
        logger.info(
            "Moved store category category_id=%s parent=%s", category_id, parent_category_id
        )

    def rename_store_category(self, category_id: str | int, name: str) -> None:
        """Rename one store category."""
        # Generated signature: rename_store_category(content_type, category_id, body=...)
        self._invoke(
            "rename_store_category",
            ("application/json", _category_id_str(category_id)),
            body={"categoryName": _clean_name(name)},
        )
        logger.info("Renamed store category category_id=%s to %r", category_id, name)

    # ------------------------------------------------------------------
    # Batch helpers
    # ------------------------------------------------------------------

    def _run_batch(
        self, items: Sequence[Any], fn, *, key: str, stop_on_error: bool
    ) -> list[dict[str, Any]]:
        """
        Apply ``fn`` to each item, recording per-item outcomes.

        eBay has no batch endpoint here, so a run can fail partway. With
        ``stop_on_error`` the exception propagates on the first failure and the
        caller is left to inspect the store; otherwise every item is attempted
        and the failures come back in the results.
        """
        results: list[dict[str, Any]] = []
        for item in items:
            try:
                fn(item)
            except Exception as e:
                if stop_on_error:
                    raise
                logger.warning("Store category operation failed for %s=%r: %s", key, item, e)
                results.append({key: item, "status": "error", "error": str(e)})
            else:
                results.append({key: item, "status": "ok", "error": None})
        return results

    def add_store_categories(
        self,
        categories: Sequence[str | Mapping[str, Any]],
        *,
        parent_category_id: str | int | None = None,
        listing_destination_category_id: str | int | None = None,
        stop_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Add several categories under the same parent, one call each.

        Accepts plain names, or dicts with "name" and an optional
        "parent_category_id" that overrides the shared parent.
        """
        specs: list[dict[str, Any]] = []
        for cat in categories:
            if isinstance(cat, str):
                specs.append({"name": cat, "parent_category_id": parent_category_id})
                continue
            if "name" not in cat:
                raise ValueError(f"category spec is missing 'name': {cat!r}")
            specs.append(
                {
                    "name": cat["name"],
                    "parent_category_id": cat.get("parent_category_id", parent_category_id),
                }
            )

        return self._run_batch(
            specs,
            lambda spec: self.add_store_category(
                spec["name"],
                parent_category_id=spec["parent_category_id"],
                listing_destination_category_id=listing_destination_category_id,
            ),
            key="category",
            stop_on_error=stop_on_error,
        )

    def delete_store_categories(
        self,
        category_ids: Sequence[str | int],
        *,
        listing_destination_category_id: str | int | None = None,
        stop_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Delete several categories, one call each.

        Deleting a parent removes its children, so a later ID in the list may
        already be gone by the time it is reached. Pass ``stop_on_error=False``
        to push through those and collect the failures.
        """
        return self._run_batch(
            list(category_ids),
            lambda cid: self.delete_store_category(
                cid, listing_destination_category_id=listing_destination_category_id
            ),
            key="category_id",
            stop_on_error=stop_on_error,
        )

    def move_store_categories(
        self,
        category_ids: Sequence[str | int],
        *,
        parent_category_id: str | int | None = None,
        listing_destination_category_id: str | int | None = None,
        stop_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        """Move several categories under the same parent, one call each."""
        return self._run_batch(
            list(category_ids),
            lambda cid: self.move_store_category(
                cid,
                parent_category_id=parent_category_id,
                listing_destination_category_id=listing_destination_category_id,
            ),
            key="category_id",
            stop_on_error=stop_on_error,
        )

    def rename_store_categories(
        self,
        renames: Mapping[str | int, str],
        *,
        stop_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        """Rename categories from a {category_id: new_name} mapping."""
        return self._run_batch(
            list(renames.items()),
            lambda pair: self.rename_store_category(pair[0], pair[1]),
            key="rename",
            stop_on_error=stop_on_error,
        )
