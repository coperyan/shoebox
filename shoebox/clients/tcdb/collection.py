"""Pure parsing for the collection views on tcdb.com, including the want list.

The want list is not a page of its own: it is the member's collection view
filtered to want status, ``ViewCollectionMode.cfm?...&Filter=W``. (``Wantlists.cfm``
is a different thing entirely -- other members' want lists.) Rows come 100 to a
page with a ``PageIndex`` link set below them.

No browser here, so these are unit-testable against saved HTML.
"""

from __future__ import annotations

import math
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from shoebox.models.tcdb import TCDB_BASE_URL, WantlistCard, WantlistPage

# "4,018 record(s)" above the table.
_RECORDS_RE = re.compile(r"([\d,]+)\s+record\(s\)", re.IGNORECASE)
_PRICE_RE = re.compile(r"^\$\s*([\d,]+(?:\.\d+)?)$")
_PROFILE_RE = re.compile(r"/Profile\.cfm/(?P<member>[^/?#]+)", re.IGNORECASE)

ROWS_PER_PAGE = 100

# Collection view filters (the "All | Haves | Wants | For Sale/Trade | In-Transaction"
# row). "W" is the want list; "G" is what you actually have.
FILTER_WANT = "W"
FILTER_HAVE = "G"


def wantlist_url(
    member: str,
    *,
    category: str = "Baseball",
    collection_id: int = 1,
    page_index: int = 1,
    records: int | None = None,
    base_url: str = TCDB_BASE_URL,
) -> str:
    """URL of one page of a member's want list."""
    params = {
        "Member": member,
        "CollectionID": collection_id,
        "Type": category,
        "Filter": FILTER_WANT,
    }
    if records is not None:
        params["Records"] = records
    if page_index > 1:
        params["PageIndex"] = page_index
    return f"{base_url.rstrip('/')}/ViewCollectionMode.cfm?{urlencode(params)}"


def parse_member(html: str) -> str | None:
    """The signed-in member's username, from the ``/Profile.cfm/<member>`` nav link.

    Saves hardcoding the account: every collection URL needs the member name.
    """
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a[href]"):
        m = _PROFILE_RE.search(a["href"])
        if m:
            return m.group("member")
    return None


def parse_wantlist_page(
    html: str,
    *,
    query_url: str = "",
    category: str = "",
    page_index: int = 1,
    base_url: str = TCDB_BASE_URL,
) -> WantlistPage:
    """Parse one ``Filter=W`` collection page.

    Each card is a ``tr.collection_row``::

        <td><a href="/CollectionEdit.cfm?SetID=..&CardID=..&ItemID=..">
            <span class="badge" title="Quantity">1</span></a></td>
        ...
        <td><i class="fa-heart-circle-plus" title="Wantlist"></i></td>
        ...
        <td><a href="/ViewCard.cfm/sid/../cid/../slug">1986 Broder #28 Barry Bonds</a></td>
        <td>$3.50</td>

    Cells are found by what they contain rather than by position, so an extra
    column on TCDB's side does not break the parse.
    """
    soup = BeautifulSoup(html, "html.parser")
    page = WantlistPage(query_url=query_url, category=category, page_index=page_index)

    count_el = soup.find(string=_RECORDS_RE)
    if count_el:
        m = _RECORDS_RE.search(count_el)
        if m:
            page.total_records = int(m.group(1).replace(",", ""))

    for tr in soup.select("tr.collection_row"):
        title_a = tr.select_one('a[href*="ViewCard.cfm"]')
        if title_a is None:
            continue

        card = WantlistCard(
            title=" ".join(title_a.get_text(" ", strip=True).split()),
            url=urljoin(base_url, title_a["href"]),
            category=category,
        )

        edit_a = tr.select_one('a[href*="CollectionEdit.cfm"]')
        if edit_a is not None:
            qs = parse_qs(urlparse(edit_a["href"]).query)
            card.set_id = _int(qs.get("SetID"))
            card.card_id = _int(qs.get("CardID"))
            card.item_id = _int(qs.get("ItemID"))

        badge = tr.select_one('span.badge[title="Quantity"]')
        if badge is not None:
            try:
                card.quantity = int(badge.get_text(strip=True))
            except ValueError:
                pass

        icon = tr.select_one("i[title]")
        if icon is not None:
            card.status = icon["title"].strip()

        for td in tr.find_all("td"):
            text = td.get_text(" ", strip=True)
            m = _PRICE_RE.match(text)
            if m:
                card.price_text = text
                card.price = float(m.group(1).replace(",", ""))
                break

        page.cards.append(card)

    page.total_pages = _total_pages(soup, page.total_records)
    return page


def _int(values: list[str] | None) -> int | None:
    try:
        return int(values[0]) if values else None
    except (ValueError, TypeError):
        return None


def _total_pages(soup: BeautifulSoup, total_records: int | None) -> int | None:
    """Highest ``PageIndex`` in the pager, falling back to the record count.

    TCDB shows a windowed pager, so the highest link is not always the last
    page; the record count wins when it implies more.
    """
    highest = 0
    for a in soup.select('a[href*="PageIndex="]'):
        qs = parse_qs(urlparse(a["href"]).query)
        idx = _int(qs.get("PageIndex")) or 0
        highest = max(highest, idx)
    from_count = math.ceil(total_records / ROWS_PER_PAGE) if total_records else 0
    best = max(highest, from_count)
    return best or None
