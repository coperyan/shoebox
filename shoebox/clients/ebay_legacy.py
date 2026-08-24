import json
import logging
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from itertools import islice
from typing import Any

import requests
import xmltodict
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

# Default namespace used by eBay Trading API responses/requests
EBAY_NS = "urn:ebay:apis:eBLBaseComponents"

# Trading error code for "application has exceeded usage limit on this call".
# The limit is a daily call allowance, so once it trips, every later call in
# the same sweep is doomed -- worth recognizing rather than retrying into.
_QUOTA_ERROR_CODE = "518"


class TradingQuotaExceeded(RuntimeError):
    """eBay's daily call allowance for this Trading call is used up.

    Distinct from an ordinary failure because waiting is the only remedy: no
    amount of retrying, backing off, or reducing concurrency helps until the
    allowance resets.
    """


def _xml_escape(value: str) -> str:
    """
    Minimal XML escaping for values embedded into request XML bodies.
    """
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _xml_to_dict(xml_text: str) -> dict[str, Any]:
    """
    Parse eBay Trading API XML -> dict.

    Trading responses typically use a *default namespace*:
      <GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"> ...

    We strip that namespace so keys are clean (e.g. "GetItemResponse", "Item", "Ack")
    instead of including namespace prefixes.
    """
    return xmltodict.parse(
        xml_text,
        process_namespaces=True,
        namespaces={EBAY_NS: None},
    )


def _ensure_list(x: Any) -> list[Any]:
    """Normalize a value that may be list/dict/str/None into a list."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


PathElem = str | tuple[str, Any]


def _dig(d: Any, path: Iterable[PathElem], default: Any = None) -> Any:
    """
    Safe dict traversal helper.

    - If element is a string: move into d[element]
    - If element is (key, default): move into d.get(key, default)

    Returns `default` if traversal fails.
    """
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        if isinstance(p, tuple):
            k, df = p
            cur = cur.get(k, df)
        else:
            cur = cur.get(p)
        if cur is None:
            return default
    return cur


def _money_to_parts(m: Any) -> tuple[str | None, str | None]:
    """
    Convert an xmltodict money node to (amount, currency).

    e.g. {"@currencyID": "USD", "#text": "12.34"} -> ("12.34", "USD")
    """
    if isinstance(m, dict):
        return m.get("#text"), m.get("@currencyID")
    if isinstance(m, str):
        return m, None
    return None, None


class eBayLegacyClient:
    """
    Lightweight eBay Trading API client for a few legacy workflows.

    Uses xmltodict for response parsing so downstream code works with dictionaries,
    not ElementTree nodes.
    """

    auth_path = "configs/ebay_legacy.json"
    trading_endpoint = "https://api.ebay.com/ws/api.dll"

    # Connections held open for reuse. Sized for the widest concurrent fan-out
    # in the codebase (get_item_details_bulk); a pool smaller than the worker
    # count silently discards connections and gives the handshake back.
    pool_size = 32

    def __init__(self) -> None:
        self.token: str | None = None
        self._authenticate()
        self._session = self._build_session()

    def _build_session(self) -> requests.Session:
        """A session that keeps its TCP/TLS connections open between calls.

        Every Trading call used to be a bare ``requests.post``: a fresh TCP
        connection and TLS handshake each time, which is most of the latency on
        a small XML request. Pooling removes that per-call cost.

        Deliberately no automatic retries -- this session also carries mutating
        calls (ReviseFixedPriceItem, EndItem), and a silent replay of one of
        those is worse than an error the caller can see.
        """
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=self.pool_size,
            pool_maxsize=self.pool_size,
            max_retries=0,
        )
        session.mount("https://", adapter)
        return session

    def _authenticate(self) -> None:
        with open(self.auth_path) as f:
            d = json.load(f)
        self.token = d.get("token")
        if not self.token:
            raise RuntimeError(f"Missing 'token' in {self.auth_path}")

    def _trading_call(
        self,
        *,
        call_name: str,
        body: str,
        site_id: str,
        compatibility_level: str,
        timeout: int = 60,
    ) -> dict[str, Any]:
        """
        Make a Trading API call and return the *payload dict* under <CallNameResponse>.

        Raises RuntimeError on non-success Ack with a structured error payload.
        """
        headers = {
            "X-EBAY-API-CALL-NAME": call_name,
            "X-EBAY-API-SITEID": site_id,
            "X-EBAY-API-COMPATIBILITY-LEVEL": compatibility_level,
            "Content-Type": "text/xml",
        }

        resp = self._session.post(
            self.trading_endpoint,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()

        doc = _xml_to_dict(resp.text)

        # Responses are usually a single top-level key: e.g. {"GetItemResponse": {...}}
        if not isinstance(doc, dict) or not doc:
            raise RuntimeError("Empty / invalid XML response")

        top_key = next(iter(doc.keys()))
        payload = doc.get(top_key)

        if not isinstance(payload, dict):
            raise RuntimeError({"message": "Unexpected response shape", "top_key": top_key})

        ack = payload.get("Ack", "")
        if ack not in ("Success", "Warning"):
            errs_out: list[dict[str, Any]] = []
            for err in _ensure_list(payload.get("Errors")):
                if not isinstance(err, dict):
                    continue
                errs_out.append(
                    {
                        "code": _dig(err, ["ErrorCode"], ""),
                        "severity": _dig(err, ["SeverityCode"], ""),
                        "message": _dig(err, ["LongMessage"], None)
                        or _dig(err, ["ShortMessage"], ""),
                    }
                )
            if any(str(e.get("code")) == _QUOTA_ERROR_CODE for e in errs_out):
                raise TradingQuotaExceeded(
                    f"eBay daily call allowance exhausted for {call_name}; "
                    "it resets at midnight Pacific."
                )
            raise RuntimeError({"ack": ack, "errors": errs_out, "call_name": call_name})

        return payload

    def get_item_details(
        self,
        item_id: str,
        *,
        site_id: str = "0",  # 0 = US
        compatibility_level: str = "1259",
    ) -> dict[str, Any]:
        """
        Fetch full listing details + item specifics using Trading API GetItem.
        Requires a Trading API user token (Auth'n'Auth) with access to the seller account.
        """

        body = f"""<?xml version="1.0" encoding="utf-8"?>
                <GetItemRequest xmlns="{EBAY_NS}">
                <RequesterCredentials>
                    <eBayAuthToken>{self.token}</eBayAuthToken>
                </RequesterCredentials>
                <ItemID>{item_id}</ItemID>
                <DetailLevel>ReturnAll</DetailLevel>
                <IncludeItemSpecifics>true</IncludeItemSpecifics>
                </GetItemRequest>"""

        payload = self._trading_call(
            call_name="GetItem",
            body=body,
            site_id=site_id,
            compatibility_level=compatibility_level,
        )

        item = payload.get("Item")
        if not isinstance(item, dict):
            raise RuntimeError("GetItem response missing Item node")

        price_node = _dig(item, ["SellingStatus", "CurrentPrice"], None)
        price, currency = _money_to_parts(price_node)

        # Parse specifics: ItemSpecifics.NameValueList -> dict[str, list[str]]
        specifics: dict[str, list[str]] = {}
        for nvl in _ensure_list(_dig(item, ["ItemSpecifics", "NameValueList"], None)):
            if not isinstance(nvl, dict):
                continue
            name = (nvl.get("Name") or "").strip()
            values = [
                v.strip()
                for v in _ensure_list(nvl.get("Value"))
                if isinstance(v, str) and v.strip()
            ]
            if name:
                specifics[name] = values

        out: dict[str, Any] = {
            "item_id": item_id,
            "title": item.get("Title"),
            "sku": item.get("SKU"),
            "listing_status": _dig(item, ["SellingStatus", "ListingStatus"]),
            "quantity": item.get("Quantity"),
            "quantity_sold": _dig(item, ["SellingStatus", "QuantitySold"]),
            "price": price,
            "currency": currency,
            "category_id": _dig(item, ["PrimaryCategory", "CategoryID"]),
            "category_name": _dig(item, ["PrimaryCategory", "CategoryName"]),
            "condition_id": item.get("ConditionID"),
            "condition_display_name": item.get("ConditionDisplayName"),
            "start_time": _dig(item, ["ListingDetails", "StartTime"]),
            "end_time": _dig(item, ["ListingDetails", "EndTime"]),
            "view_item_url": _dig(item, ["ListingDetails", "ViewItemURL"]),
            "item_specifics": specifics,
        }

        # Optional: picture URLs
        pics = [
            p
            for p in _ensure_list(_dig(item, ["PictureDetails", "PictureURL"], None))
            if isinstance(p, str) and p
        ]
        if pics:
            out["picture_urls"] = pics

        return out

    def get_item_details_bulk(
        self,
        item_ids: list[str],
        *,
        max_workers: int = 8,
        site_id: str = "0",  # 0 = US
        compatibility_level: str = "1259",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """
        Fetch details for many listings at once, concurrently.

        There is no batch equivalent of GetItem for this data: GetSellerList
        pages 200 listings per call but returns no ItemSpecifics at all, and the
        Browse API's bulk `getItems` needs partner-level access. So the only
        lever is overlapping the calls -- they are almost entirely network wait.

        Results come back in the order the item IDs were given. A listing that
        fails is collected rather than raised, so one bad item cannot throw away
        a sweep of hundreds; callers get ``(details, {item_id: error})`` and
        decide what an acceptable failure count is.

        The exception is running out of daily call allowance: every remaining
        call would fail the same way, so the sweep stops there and raises
        :class:`TradingQuotaExceeded` instead of hammering the wall.

        ``max_workers`` is capped at the connection-pool size. eBay's Trading
        limits are a daily allowance rather than a rate, so concurrency buys
        wall-clock but never additional calls -- a wide fan-out is still worth
        keeping modest.
        """
        if not item_ids:
            return [], {}

        workers = max(1, min(max_workers, self.pool_size, len(item_ids)))
        details: dict[str, dict[str, Any]] = {}
        failures: dict[str, str] = {}
        total = len(item_ids)

        def fetch(item_id: str) -> dict[str, Any]:
            return self.get_item_details(
                item_id,
                site_id=site_id,
                compatibility_level=compatibility_level,
            )

        quota_error: TradingQuotaExceeded | None = None
        remaining = iter(item_ids)
        completed = 0

        # Work is fed in a sliding window rather than submitted all at once.
        # Queued-but-unstarted futures are the only ones cancel() can stop, so
        # submitting everything up front would keep firing doomed calls for a
        # long time after the allowance runs out.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {
                pool.submit(fetch, item_id): item_id for item_id in islice(remaining, workers * 2)
            }
            while pending:
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    item_id = pending.pop(future)
                    try:
                        details[item_id] = future.result()
                    except TradingQuotaExceeded as e:
                        quota_error = quota_error or e
                    except Exception as e:
                        logger.warning("GetItem failed for item_id=%s: %s", item_id, e)
                        failures[item_id] = str(e)
                    completed += 1
                    if on_progress:
                        on_progress(completed, total)

                if quota_error:
                    for future in pending:
                        future.cancel()
                    break

                for item_id in islice(remaining, len(finished)):
                    pending[pool.submit(fetch, item_id)] = item_id

        if quota_error:
            raise TradingQuotaExceeded(
                f"{quota_error} Fetched {len(details)} of {total} listing(s) first."
            ) from quota_error

        ordered = [details[item_id] for item_id in item_ids if item_id in details]
        return ordered, failures

    def get_active_listings(
        self,
        *,
        entries_per_page: int = 200,
        page_number: int = 1,
        max_pages: int | None = None,
        site_id: str = "0",  # 0 = US
        compatibility_level: str = "1259",
    ) -> list[dict[str, Any]]:
        """
        Retrieve *all* active listings for the authenticated seller using the Trading API.

        Uses Trading API GetMyeBaySelling with ActiveList pagination.
        Returns a list of lightweight listing dicts (ItemID, Title, SKU, Quantity, Price, etc.).
        """
        if entries_per_page < 1:
            raise ValueError("entries_per_page must be >= 1")

        results: list[dict[str, Any]] = []
        current_page = page_number

        while True:
            body = f"""<?xml version="1.0" encoding="utf-8"?>
                    <GetMyeBaySellingRequest xmlns="{EBAY_NS}">
                    <RequesterCredentials>
                        <eBayAuthToken>{self.token}</eBayAuthToken>
                    </RequesterCredentials>
                    <ErrorLanguage>en_US</ErrorLanguage>
                    <WarningLevel>High</WarningLevel>
                    <ActiveList>
                        <Include>true</Include>
                        <Pagination>
                        <EntriesPerPage>{entries_per_page}</EntriesPerPage>
                        <PageNumber>{current_page}</PageNumber>
                        </Pagination>
                        <Sort>TimeLeft</Sort>
                    </ActiveList>
                    </GetMyeBaySellingRequest>"""

            payload = self._trading_call(
                call_name="GetMyeBaySelling",
                body=body,
                site_id=site_id,
                compatibility_level=compatibility_level,
            )

            items = _ensure_list(_dig(payload, ["ActiveList", "ItemArray", "Item"], None))

            for item in items:
                if not isinstance(item, dict):
                    continue

                price_node = _dig(item, ["SellingStatus", "CurrentPrice"], None)
                price, currency = _money_to_parts(price_node)

                results.append(
                    {
                        "item_id": item.get("ItemID"),
                        "title": item.get("Title"),
                        "sku": item.get("SKU"),
                        "listing_status": _dig(item, ["SellingStatus", "ListingStatus"]),
                        "quantity": item.get("Quantity"),
                        "quantity_sold": _dig(item, ["SellingStatus", "QuantitySold"]),
                        "price": price,
                        "currency": currency,
                        "start_time": _dig(item, ["ListingDetails", "StartTime"]),
                        "end_time": _dig(item, ["ListingDetails", "EndTime"]),
                        "watchers": item.get("WatchCount"),
                        "view_item_url": _dig(item, ["ListingDetails", "ViewItemURL"]),
                    }
                )

            total_pages_text = _dig(
                payload, ["ActiveList", "PaginationResult", "TotalNumberOfPages"], None
            )
            try:
                total_pages = int(total_pages_text) if total_pages_text else None
            except (TypeError, ValueError):
                total_pages = None

            # Stop conditions:
            # 1) no items returned (empty page)
            # 2) we reached total pages (if provided)
            # 3) max_pages reached (caller limit)
            if not items:
                break
            if total_pages is not None and current_page >= total_pages:
                break
            if max_pages is not None and (current_page - page_number + 1) >= max_pages:
                break

            current_page += 1

        return results

    def get_scheduled_listings(
        self,
        *,
        entries_per_page: int = 200,
        page_number: int = 1,
        max_pages: int | None = None,
        site_id: str = "0",  # 0 = US
        compatibility_level: str = "1259",
    ) -> list[dict[str, Any]]:
        """
        Retrieve *all* active listings for the authenticated seller using the Trading API.

        Uses Trading API GetMyeBaySelling with ActiveList pagination.
        Returns a list of lightweight listing dicts (ItemID, Title, SKU, Quantity, Price, etc.).
        """
        if entries_per_page < 1:
            raise ValueError("entries_per_page must be >= 1")

        results: list[dict[str, Any]] = []
        current_page = page_number

        while True:
            body = f"""<?xml version="1.0" encoding="utf-8"?>
                    <GetMyeBaySellingRequest xmlns="{EBAY_NS}">
                    <RequesterCredentials>
                        <eBayAuthToken>{self.token}</eBayAuthToken>
                    </RequesterCredentials>
                    <ErrorLanguage>en_US</ErrorLanguage>
                    <WarningLevel>High</WarningLevel>
                    <ScheduledList>
                        <Include>true</Include>
                        <Pagination>
                        <EntriesPerPage>{entries_per_page}</EntriesPerPage>
                        <PageNumber>{current_page}</PageNumber>
                        </Pagination>
                        <Sort>StartTime</Sort>
                    </ScheduledList>
                    </GetMyeBaySellingRequest>"""

            payload = self._trading_call(
                call_name="GetMyeBaySelling",
                body=body,
                site_id=site_id,
                compatibility_level=compatibility_level,
            )

            items = _ensure_list(_dig(payload, ["ScheduledList", "ItemArray", "Item"], None))

            for item in items:
                if not isinstance(item, dict):
                    continue

                price_node = _dig(item, ["SellingStatus", "CurrentPrice"], None)
                price, currency = _money_to_parts(price_node)

                results.append(
                    {
                        "item_id": item.get("ItemID"),
                        "title": item.get("Title"),
                        "sku": item.get("SKU"),
                        "listing_status": _dig(item, ["SellingStatus", "ListingStatus"]),
                        "quantity": item.get("Quantity"),
                        "quantity_sold": _dig(item, ["SellingStatus", "QuantitySold"]),
                        "price": price,
                        "currency": currency,
                        "start_time": _dig(item, ["ListingDetails", "StartTime"]),
                        "end_time": _dig(item, ["ListingDetails", "EndTime"]),
                        "watchers": item.get("WatchCount"),
                        "view_item_url": _dig(item, ["ListingDetails", "ViewItemURL"]),
                    }
                )

            total_pages_text = _dig(
                payload,
                ["ScheduledList", "PaginationResult", "TotalNumberOfPages"],
                None,
            )
            try:
                total_pages = int(total_pages_text) if total_pages_text else None
            except (TypeError, ValueError):
                total_pages = None

            # Stop conditions:
            # 1) no items returned (empty page)
            # 2) we reached total pages (if provided)
            # 3) max_pages reached (caller limit)
            if not items:
                break
            if total_pages is not None and current_page >= total_pages:
                break
            if max_pages is not None and (current_page - page_number + 1) >= max_pages:
                break

            current_page += 1

        return results

    def get_out_of_stock_listings(
        self,
        *,
        entries_per_page: int = 200,
        page_number: int = 1,
        max_pages: int | None = None,
        site_id: str = "0",
        compatibility_level: str = "1259",
    ) -> list[dict[str, Any]]:
        """
        Retrieve active listings where all quantity has sold (remaining = 0).

        Queries GetMyeBaySelling ActiveList and filters to items where
        Quantity - QuantitySold == 0. These are Out-of-Stock Control listings
        that remain active but are hidden from eBay search.
        Returns the same shape dicts as get_active_listings, plus `quantity_remaining`.
        """
        if entries_per_page < 1:
            raise ValueError("entries_per_page must be >= 1")

        results: list[dict[str, Any]] = []
        current_page = page_number

        while True:
            body = f"""<?xml version="1.0" encoding="utf-8"?>
                    <GetMyeBaySellingRequest xmlns="{EBAY_NS}">
                    <RequesterCredentials>
                        <eBayAuthToken>{self.token}</eBayAuthToken>
                    </RequesterCredentials>
                    <ErrorLanguage>en_US</ErrorLanguage>
                    <WarningLevel>High</WarningLevel>
                    <ActiveList>
                        <Include>true</Include>
                        <Pagination>
                        <EntriesPerPage>{entries_per_page}</EntriesPerPage>
                        <PageNumber>{current_page}</PageNumber>
                        </Pagination>
                        <Sort>TimeLeft</Sort>
                    </ActiveList>
                    </GetMyeBaySellingRequest>"""

            payload = self._trading_call(
                call_name="GetMyeBaySelling",
                body=body,
                site_id=site_id,
                compatibility_level=compatibility_level,
            )

            items = _ensure_list(_dig(payload, ["ActiveList", "ItemArray", "Item"], None))

            for item in items:
                if not isinstance(item, dict):
                    continue

                try:
                    quantity = int(item.get("Quantity") or 0)
                    quantity_sold = int(_dig(item, ["SellingStatus", "QuantitySold"], 0) or 0)
                except (TypeError, ValueError):
                    continue

                if quantity - quantity_sold != 0:
                    continue

                price_node = _dig(item, ["SellingStatus", "CurrentPrice"], None)
                price, currency = _money_to_parts(price_node)

                results.append(
                    {
                        "item_id": item.get("ItemID"),
                        "title": item.get("Title"),
                        "sku": item.get("SKU"),
                        "listing_status": _dig(item, ["SellingStatus", "ListingStatus"]),
                        "quantity": quantity,
                        "quantity_sold": quantity_sold,
                        "quantity_remaining": quantity - quantity_sold,
                        "price": price,
                        "currency": currency,
                        "start_time": _dig(item, ["ListingDetails", "StartTime"]),
                        "end_time": _dig(item, ["ListingDetails", "EndTime"]),
                        "watchers": item.get("WatchCount"),
                        "view_item_url": _dig(item, ["ListingDetails", "ViewItemURL"]),
                    }
                )

            total_pages_text = _dig(
                payload, ["ActiveList", "PaginationResult", "TotalNumberOfPages"], None
            )
            try:
                total_pages = int(total_pages_text) if total_pages_text else None
            except (TypeError, ValueError):
                total_pages = None

            if not items:
                break
            if total_pages is not None and current_page >= total_pages:
                break
            if max_pages is not None and (current_page - page_number + 1) >= max_pages:
                break

            current_page += 1

        return results

    def end_listing(
        self,
        item_id: str,
        *,
        ending_reason: str = "NotAvailable",
        site_id: str = "0",
        compatibility_level: str = "1259",
    ) -> dict[str, Any]:
        """
        End a single active listing using the Trading API EndItem call.

        Valid ending_reason values: NotAvailable, LostOrBroken, Incorrect,
        OtherListingError, Sold, SellToHighBidder.
        Returns a dict with item_id, end_time, and ack.
        """
        body = f"""<?xml version="1.0" encoding="utf-8"?>
                <EndItemRequest xmlns="{EBAY_NS}">
                <RequesterCredentials>
                    <eBayAuthToken>{self.token}</eBayAuthToken>
                </RequesterCredentials>
                <ErrorLanguage>en_US</ErrorLanguage>
                <WarningLevel>High</WarningLevel>
                <ItemID>{item_id}</ItemID>
                <EndingReason>{_xml_escape(ending_reason)}</EndingReason>
                </EndItemRequest>"""

        payload = self._trading_call(
            call_name="EndItem",
            body=body,
            site_id=site_id,
            compatibility_level=compatibility_level,
        )

        return {
            "item_id": item_id,
            "end_time": payload.get("EndTime"),
            "ack": payload.get("Ack", "Success"),
        }

    def end_listings(
        self,
        item_ids: list[str],
        *,
        ending_reason: str = "NotAvailable",
        site_id: str = "0",
        compatibility_level: str = "1259",
    ) -> list[dict[str, Any]]:
        """
        End multiple active listings, one EndItem call per item.

        Returns a list of result dicts (item_id, end_time, ack) in the same
        order as item_ids. A failure on any individual item raises RuntimeError
        and stops processing; catch per-item if partial success is needed.
        """
        return [
            self.end_listing(
                item_id,
                ending_reason=ending_reason,
                site_id=site_id,
                compatibility_level=compatibility_level,
            )
            for item_id in item_ids
        ]

    def revise_listing_title(
        self,
        item_id: str,
        title: str,
        *,
        site_id: str = "0",  # 0 = US
        compatibility_level: str = "1259",
    ) -> dict[str, Any]:
        """
        Change the title of a fixed-price listing via Trading `ReviseFixedPriceItem`.

        This is the path for listings that have no SKU -- created outside the
        Sell Inventory API, which is the only way to retitle the ones that do.
        eBay rejects Trading revisions on Inventory-API listings, so callers
        should reach for this only when the inventory path doesn't apply or has
        already failed.
        """
        if not title or not title.strip():
            raise ValueError("title must be a non-empty string")
        if len(title) > 80:
            raise ValueError(f"eBay titles are capped at 80 characters; got {len(title)}")

        body = f"""<?xml version="1.0" encoding="utf-8"?>
                <ReviseFixedPriceItemRequest xmlns="{EBAY_NS}">
                <RequesterCredentials>
                    <eBayAuthToken>{self.token}</eBayAuthToken>
                </RequesterCredentials>
                <ErrorLanguage>en_US</ErrorLanguage>
                <WarningLevel>High</WarningLevel>
                <Item>
                    <ItemID>{item_id}</ItemID>
                    <Title>{_xml_escape(title)}</Title>
                </Item>
                </ReviseFixedPriceItemRequest>"""

        payload = self._trading_call(
            call_name="ReviseFixedPriceItem",
            body=body,
            site_id=site_id,
            compatibility_level=compatibility_level,
        )

        return {
            "ack": payload.get("Ack", "Success"),
            "item_id": payload.get("ItemID", item_id),
            "title": title,
        }

    def revise_store_category(
        self,
        item_id: str,
        category_id: str | int,
        secondary_category_id: str | int | None = None,
        *,
        site_id: str = "0",  # 0 = US
        compatibility_level: str = "1259",
    ) -> dict[str, Any]:
        """
        Move a listing into store categories via Trading `ReviseFixedPriceItem`.

        The counterpart to the Sell Inventory route, which addresses categories
        by name: Trading wants the numeric store category IDs, so callers
        resolve names to IDs from ``StoresClient.get_store_categories`` first.

        This is the path for listings with no SKU. eBay rejects Trading
        revisions on Inventory-API listings, so reach for it only when the
        inventory path doesn't apply.
        """
        primary = str(int(str(category_id).strip()))
        secondary = (
            str(int(str(secondary_category_id).strip()))
            if secondary_category_id is not None
            else None
        )
        if secondary == primary:
            # eBay rejects a listing filed twice in the same category.
            secondary = None

        second_xml = f"<StoreCategory2ID>{secondary}</StoreCategory2ID>" if secondary else ""
        body = f"""<?xml version="1.0" encoding="utf-8"?>
                <ReviseFixedPriceItemRequest xmlns="{EBAY_NS}">
                <RequesterCredentials>
                    <eBayAuthToken>{self.token}</eBayAuthToken>
                </RequesterCredentials>
                <ErrorLanguage>en_US</ErrorLanguage>
                <WarningLevel>High</WarningLevel>
                <Item>
                    <ItemID>{item_id}</ItemID>
                    <Storefront>
                        <StoreCategoryID>{primary}</StoreCategoryID>
                        {second_xml}
                    </Storefront>
                </Item>
                </ReviseFixedPriceItemRequest>"""

        payload = self._trading_call(
            call_name="ReviseFixedPriceItem",
            body=body,
            site_id=site_id,
            compatibility_level=compatibility_level,
        )

        return {
            "ack": payload.get("Ack", "Success"),
            "item_id": payload.get("ItemID", item_id),
            "store_category_id": primary,
            "store_category_2_id": secondary,
        }

    def add_sku_to_listing(
        self,
        item_id: str,
        sku: str,
        *,
        site_id: str = "0",  # 0 = US
        compatibility_level: str = "1259",
    ) -> dict[str, Any]:
        """
        Add or update the SKU on a listing via the Trading API.

        Uses Trading API `ReviseFixedPriceItem`, which supports revising SKU on fixed-price listings.
        Returns a small response dict (Ack, ItemID, SKU, Fees if present).
        """
        if not sku or not isinstance(sku, str):
            raise ValueError("sku must be a non-empty string")

        body = f"""<?xml version="1.0" encoding="utf-8"?>
                <ReviseFixedPriceItemRequest xmlns="{EBAY_NS}">
                <RequesterCredentials>
                    <eBayAuthToken>{self.token}</eBayAuthToken>
                </RequesterCredentials>
                <ErrorLanguage>en_US</ErrorLanguage>
                <WarningLevel>High</WarningLevel>
                <Item>
                    <ItemID>{item_id}</ItemID>
                    <SKU>{_xml_escape(sku)}</SKU>
                </Item>
                </ReviseFixedPriceItemRequest>"""

        payload = self._trading_call(
            call_name="ReviseFixedPriceItem",
            body=body,
            site_id=site_id,
            compatibility_level=compatibility_level,
        )

        # Fees are optional; collect a simple list if present
        fees: list[dict[str, Any]] = []
        for fee in _ensure_list(_dig(payload, ["Fees", "Fee"], None)):
            if not isinstance(fee, dict):
                continue
            amount, currency = _money_to_parts(fee.get("Fee"))
            fees.append({"name": fee.get("Name"), "amount": amount, "currency": currency})

        return {
            "ack": payload.get("Ack", "Success"),
            "item_id": payload.get("ItemID", item_id),
            "sku": sku,
            "fees": fees,
        }


# test = eBayLegacyClient()

# body = f"""<?xml version="1.0" encoding="utf-8"?>
#         <GetItemRequest xmlns="{EBAY_NS}">
#         <RequesterCredentials>
#             <eBayAuthToken>{test.token}</eBayAuthToken>
#         </RequesterCredentials>
#         <ItemID>317247766017</ItemID>
#         <DetailLevel>ReturnAll</DetailLevel>
#         <IncludeItemSpecifics>true</IncludeItemSpecifics>
#         </GetItemRequest>"""

# payload = test._trading_call(
#     call_name="GetItem",
#     body=body,
#     site_id="0",
#     compatibility_level="1259",
# )

# item = payload.get("Item")
# if not isinstance(item, dict):
#     raise RuntimeError("GetItem response missing Item node")

# price_node = _dig(item, ["SellingStatus", "CurrentPrice"], None)
# price, currency = _money_to_parts(price_node)

# # Parse specifics: ItemSpecifics.NameValueList -> dict[str, list[str]]
# specifics: Dict[str, List[str]] = {}
# for nvl in _ensure_list(_dig(item, ["ItemSpecifics", "NameValueList"], None)):
#     if not isinstance(nvl, dict):
#         continue
#     name = (nvl.get("Name") or "").strip()
#     values = [
#         v.strip()
#         for v in _ensure_list(nvl.get("Value"))
#         if isinstance(v, str) and v.strip()
#     ]
#     if name:
#         specifics[name] = values
