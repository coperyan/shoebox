"""Pure parsing helpers for TCDB pages. No browser here, so they are unit-testable
against saved HTML.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from shoebox.models.tcdb import TCDB_BASE_URL, TcdbSearchPage, TcdbSearchResult

_VIEWCARD_RE = re.compile(r"/ViewCard\.cfm/sid/(?P<sid>\d+)/cid/(?P<cid>\d+)", re.IGNORECASE)
_RESULT_COUNT_RE = re.compile(r"([\d,]+)\s+results?", re.IGNORECASE)
_LOGOUT_RE = re.compile(r"log(out|off)", re.IGNORECASE)
_LOGIN_RE = re.compile(r"^/?Login\.cfm$", re.IGNORECASE)


def is_challenge_page(title: str | None, html: str | None = None) -> bool:
    """Cloudflare's interstitial ("Just a moment...") before the real page loads."""
    if title and "just a moment" in title.casefold():
        return True
    if html and "Performing security verification" in html:
        return True
    return False


def is_logged_in(html: str) -> bool:
    """Decide from the page header whether TCDB considers this browser logged in.

    Logged-out pages carry a ``Login`` nav link to ``/Login.cfm``; logged-in pages
    replace it with account links (and a logout link). A logout link wins if both
    somehow appear.
    """
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        if _LOGOUT_RE.search(a["href"]) or _LOGOUT_RE.search(a.get_text(" ", strip=True)):
            return True
    for a in soup.select("a.nav-link[href]"):
        if _LOGIN_RE.match(a["href"].strip()):
            return False
    # No login link in the nav and nothing that says logout: treat as logged in only
    # if the page really is a TCDB page (has the nav at all).
    return soup.select_one("ul.navbar-nav") is not None


def parse_results(html: str, query_url: str = "", base_url: str = TCDB_BASE_URL) -> TcdbSearchPage:
    """Parse a ViewResults.cfm page into structured rows.

    Row layout (one <tr> per card)::

        <td>1.</td>
        <td><a href="/ViewCard.cfm/sid/…/cid/…/slug"><img src="/Images/Thumbs/…"></a></td>
        <td><a href="/ViewCard.cfm/…">1994 Upper Deck Fun Pack #25 Barry Bonds</a>
            <br><span class="text-muted">optional note</span></td>
    """
    soup = BeautifulSoup(html, "html.parser")
    page = TcdbSearchPage(query_url=query_url)

    count_el = soup.find("strong", string=_RESULT_COUNT_RE)
    if count_el:
        m = _RESULT_COUNT_RE.search(count_el.get_text())
        if m:
            page.total_results = int(m.group(1).replace(",", ""))

    table = soup.select_one("table.table.table-hover") or soup.select_one("table.table")
    if table is None:
        # "0 results" pages render the count but no table.
        if page.total_results is None and soup.find(string=re.compile(r"no (cards|results)", re.I)):
            page.total_results = 0
        return page

    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        links = [a for a in tr.find_all("a", href=True) if _VIEWCARD_RE.search(a["href"])]
        if not links:
            continue
        title_link = next((a for a in links if a.get_text(strip=True)), links[-1])
        href = title_link["href"]
        m = _VIEWCARD_RE.search(href)

        index_text = cells[0].get_text(strip=True).rstrip(".")
        try:
            index = int(index_text)
        except ValueError:
            index = len(page.results) + 1

        img = tr.find("img")
        thumb = img.get("src") if img else None
        if thumb and "AddImage" in thumb:  # TCDB's "no image yet" placeholder
            thumb = None

        note_el = title_link.parent.find("span", class_="text-muted")
        note = note_el.get_text(" ", strip=True) if note_el else None

        page.results.append(
            TcdbSearchResult(
                index=index,
                title=title_link.get_text(" ", strip=True),
                url=urljoin(base_url, href),
                set_id=int(m.group("sid")) if m else None,
                card_id=int(m.group("cid")) if m else None,
                thumb_url=urljoin(base_url, thumb) if thumb else None,
                note=note or None,
            )
        )

    if page.total_results is None:
        page.total_results = len(page.results)
    return page
