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


# A row title is "{year} {set} #{number} {player}", e.g.
# "1986 Topps Traded - Limited Edition (Tiffany) #11T Barry Bonds".
_TITLE_RE = re.compile(r"^(?P<year>\d{4}(?:-\d{2}(?:\d{2})?)?)\s+(?P<set>.+?)\s+#(?P<rest>.+)$")

# TCDB writes a subset/parallel as "<master> - <subset>". Set names use hyphens
# without spaces ("O-Pee-Chee"), so a spaced " - " is unambiguous.
_SUBSET_SEP = " - "

BASE_SUBSET = "Base"


def split_set_name(year: str, set_part: str) -> tuple[str, str]:
    """Master set name (with year) and subset name.

    ``2026 Topps Chrome - 1991 Topps Anniversary`` -> ``2026 Topps Chrome`` and
    ``1991 Topps Anniversary``. A set with no subset gets ``Base``.
    """
    master, sep, subset = set_part.partition(_SUBSET_SEP)
    full_master = f"{year} {master.strip()}".strip()
    return full_master, (subset.strip() if sep else BASE_SUBSET)


def _is_number_token(token: str) -> bool:
    """Whether a token after the "#" belongs to the card number, not the player.

    Numbers carry a digit ("171", "U-14", "11T") or are an all-caps code:
    short ("NNO" for unnumbered, "HAMTC", the "PP" of "PP 3") or hyphenated
    ("GAA-BB", "IG-BBO"). Player names always have lowercase, and initials
    like "R.J." are excluded by the dot.
    """
    if any(ch.isdigit() for ch in token):
        return True
    if not token.isupper() or "." in token:
        return False
    return "-" in token or 2 <= len(token) <= 6


# Words skipped when abbreviating a subset name to its initials.
_INITIAL_STOPWORDS = frozenset({"a", "an", "and", "of", "the"})


def _abbreviates_subset(code: str, subset: str) -> bool:
    """Whether an all-caps code is the subset's initials, so part of the number.

    TCDB numbers a subset card "#2 DS" under "Diamond Standouts". The same shape
    with an unrelated code -- "#4 NL Batting Average Leaders" under subset
    "Gold" -- is a description of the card, and stays with the player.
    """
    if len(code) < 2 or not subset or subset == BASE_SUBSET:
        return False
    words = re.findall(r"[A-Za-z']+", subset)
    initials = "".join(w[0] for w in words if w.lower() not in _INITIAL_STOPWORDS).upper()
    return bool(initials) and initials.startswith(code.upper())


def split_number_and_player(rest: str, subset: str = "") -> tuple[str, str]:
    """Split "U-14 Barry Bonds" into its card number and player.

    The number is one token, extended only two ways:

    - an alpha-only code takes the digits after it ("PP 3"), and digits take a
      code that abbreviates the subset ("#2 DS" under "Diamond Standouts");
    - a "/" joins two numbers on a shared card ("24-A / 24-B", "131 / 292",
      which carry a player each: "Barry Bonds / Neil Allen").

    Taking no more than that keeps league-leader cards intact, where what
    follows the number is a description rather than a name: "#106 NL ERA
    Leaders (...)" is number "106".
    """
    tokens = rest.split()
    if not tokens or not _is_number_token(tokens[0]):
        return "", rest.strip()

    taken = 1
    if (
        not any(ch.isdigit() for ch in tokens[0])
        and taken < len(tokens)
        and tokens[taken].isdigit()
    ):
        taken += 1
    elif (
        taken < len(tokens)
        and tokens[taken].isupper()
        and _abbreviates_subset(tokens[taken], subset)
    ):
        taken += 1
    while taken + 1 < len(tokens) and tokens[taken] == "/" and _is_number_token(tokens[taken + 1]):
        taken += 2

    return " ".join(tokens[:taken]), " ".join(tokens[taken:]).strip()


def parse_title_fields(title: str) -> dict[str, str]:
    """Pull year, set, subset, card number and player out of a TCDB row title.

    Returns blanks rather than raising: a title TCDB writes unusually should
    cost one row's detail, not the export.
    """
    blank = {
        "set_year": "",
        "set_name": "",
        "subset_name": "",
        "card_number": "",
        "player": "",
    }
    m = _TITLE_RE.match(" ".join((title or "").split()))
    if not m:
        return blank
    set_name, subset_name = split_set_name(m.group("year"), m.group("set"))
    card_number, player = split_number_and_player(m.group("rest"), subset_name)
    return {
        "set_year": m.group("year"),
        "set_name": set_name,
        "subset_name": subset_name,
        "card_number": card_number,
        "player": player,
    }


def parse_card_team(html: str) -> str:
    """Team from a ViewCard.cfm page's heading (``h4.site`` -> ``Team.cfm`` link)."""
    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one('h4.site a[href*="Team.cfm"]') or soup.select_one('a[href*="Team.cfm"]')
    return " ".join(link.get_text(" ", strip=True).split()) if link else ""


def _row_notes(title_cell) -> tuple[str, str]:
    """Note codes and their explanation from a row's title cell.

    TCDB puts the codes as bare text after the card link ("RC, VAR") and the
    long form in a ``<figcaption>`` under it.
    """
    detail = " ".join(
        f.get_text(" ", strip=True) for f in title_cell.find_all("figcaption")
    ).strip()
    parts = []
    for child in title_cell.children:
        name = getattr(child, "name", None)
        if name in ("a", "figcaption", "br"):
            continue
        text = child.get_text(" ", strip=True) if name else str(child).strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip(" ,"), detail


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

        title = " ".join(title_a.get_text(" ", strip=True).split())
        notes, note_detail = _row_notes(title_a.find_parent("td"))
        card = WantlistCard(
            title=title,
            url=urljoin(base_url, title_a["href"]),
            category=category,
            notes=notes,
            note_detail=note_detail,
            **parse_title_fields(title),
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
