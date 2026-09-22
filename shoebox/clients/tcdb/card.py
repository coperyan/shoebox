"""Pure helpers for adding cards to a TCDB collection: parse a card written as
a TCDB title, pick its row out of a search, read a ViewCard.cfm page and the
collection box it loads. No browser here, so they are unit-testable.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from shoebox.models.tcdb import (
    TCDB_BASE_URL,
    CardSpec,
    CollectionWidget,
    TcdbCardPage,
    TcdbSearchPage,
    TcdbSearchResult,
)

# "{year} {set} #{number} {name}". Year is "2009" or a season like "2009-10";
# the set is everything up to the "#" (TCDB set names never contain one).
_CARD_SPEC_RE = re.compile(
    r"^(?P<year>\d{4}(?:-\d{2}(?:\d{2})?)?)\s+(?P<set>[^#]+?)\s+#(?P<number>\S+)(?:\s+(?P<name>.+))?$"
)
_VIEWCARD_URL_RE = re.compile(
    r"^https?://(?:www\.)?tcdb\.com/ViewCard\.cfm/sid/(?P<sid>\d+)/cid/(?P<cid>\d+)", re.IGNORECASE
)
_TITLE_SUFFIX = " | Trading Card Database"


def parse_card_spec(text: str) -> CardSpec:
    """Split ``2009 Bowman Chrome - X-Fractors #171 Matt Cain`` into its parts."""
    raw = " ".join((text or "").split())
    m = _CARD_SPEC_RE.match(raw)
    if not m:
        raise ValueError(
            f"Could not read {text!r}. Write it as TCDB titles it: "
            "'<year> <set> #<number> <name>', e.g. '2009 Bowman Chrome - X-Fractors #171 Matt Cain'"
        )
    return CardSpec(
        raw=raw,
        year=m.group("year"),
        set_name=m.group("set"),
        card_number=m.group("number"),
        name=m.group("name") or "",
    )


def is_card_url(text: str) -> bool:
    """True for a tcdb.com ViewCard.cfm link (added directly, no search)."""
    return bool(_VIEWCARD_URL_RE.match((text or "").strip()))


def normalize_title(title: str) -> str:
    """Case-, accent-, apostrophe- and whitespace-insensitive form for comparing titles."""
    text = unicodedata.normalize("NFKD", title or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("’", "'").replace("‘", "'")
    return " ".join(text.split()).casefold()


def match_card(page: TcdbSearchPage, spec: CardSpec) -> list[TcdbSearchResult]:
    """Results whose title is exactly the spec's title (normalized).

    With no name in the spec, any player on that year/set/number matches.
    """
    wanted = normalize_title(spec.title)
    if spec.name:
        return [r for r in page.results if normalize_title(r.title) == wanted]
    return [r for r in page.results if normalize_title(r.title).startswith(wanted + " ")]


def parse_card_page(html: str, url: str = "", base_url: str = TCDB_BASE_URL) -> TcdbCardPage:
    """Title and ids of a ViewCard.cfm page (from its canonical link and og:title)."""
    soup = BeautifulSoup(html, "html.parser")

    canonical = soup.find("link", rel="canonical")
    page_url = urljoin(base_url, canonical["href"]) if canonical and canonical.get("href") else url

    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"]
    else:
        title = soup.title.get_text() if soup.title else ""
        title = title.removesuffix(_TITLE_SUFFIX)

    m = _VIEWCARD_URL_RE.match(page_url or "")
    return TcdbCardPage(
        url=page_url,
        title=" ".join(title.split()),
        set_id=int(m.group("sid")) if m else None,
        card_id=int(m.group("cid")) if m else None,
    )


def parse_collection_widget(html: str) -> CollectionWidget:
    """Read the controls in the ``#colDiv`` box (pass its inner HTML)."""
    if not (html or "").strip():
        return CollectionWidget(loaded=False)
    soup = BeautifulSoup(html, "html.parser")
    return CollectionWidget(
        loaded=True,
        quick_add=soup.select_one("#quickAddBtn") is not None,
        add_another=soup.select_one(".addAnotherBtn") is not None,
        remove=soup.select_one(".removeBtn") is not None,
    )
