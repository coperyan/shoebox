from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from urllib.parse import urljoin

import chrome_version
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from shoebox.models.topps_release import ToppsRelease

logger = logging.getLogger(__name__)


# Year is optional — many calendar pages omit it (e.g. "May 11" vs "May 11, 2026")
_MONTH_RE = re.compile(
    r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(?P<year>\d{4}))?\b",
    re.IGNORECASE,
)

# Matches numeric dates: 5/11/2026, 05/11/2026, 5/11
_NUMERIC_DATE_RE = re.compile(r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>\d{2,4}))?\b")

_PRE_ORDER_RE = re.compile(r"\bpre[-\s]?order\b", re.IGNORECASE)

# "Drops in 3 days" / "Drops in 2 hours" — countdown text, not a real date
_DROPS_IN_RE = re.compile(
    r"\bdrops?\s+in\s+\d[\w\s]*",
    re.IGNORECASE,
)

# "at 10:00 AM PDT" suffix that sometimes accompanies dates
_TIME_SUFFIX_RE = re.compile(
    r"\bat\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?\s*[A-Z]{0,5}\b",
    re.IGNORECASE,
)

_DATE_FMTS = [
    "%b %d %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%Y-%m-%d",
]


def _parse_date(text: str) -> date | None:
    if not text:
        return None
    cleaned = text.strip()
    cleaned = _DROPS_IN_RE.sub("", cleaned)
    cleaned = _TIME_SUFFIX_RE.sub("", cleaned)
    today = date.today()

    m = _MONTH_RE.search(cleaned)
    if m:
        year_str = m.group("year")
        year = int(year_str) if year_str else today.year
        cand = f"{m.group('month')} {int(m.group('day')):02d} {year}"
        for fmt in _DATE_FMTS:
            try:
                result = datetime.strptime(cand, fmt).date()
                # No explicit year: roll forward if the date has already passed
                if not year_str and result < today:
                    result = result.replace(year=result.year + 1)
                return result
            except ValueError:
                continue

    m2 = _NUMERIC_DATE_RE.search(cleaned)
    if m2:
        year_str = m2.group("year")
        year = int(year_str) if year_str else today.year
        if year < 100:
            year += 2000
        try:
            result = date(year, int(m2.group("month")), int(m2.group("day")))
            if not year_str and result < today:
                result = result.replace(year=result.year + 1)
            return result
        except ValueError:
            pass

    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


class ToppsReleaseScraper:
    """Scrape upcoming product releases from topps.com/release-calendar.

    The page is JS-rendered behind Cloudflare, so we use undetected-chromedriver
    to load the rendered DOM, then BeautifulSoup to parse release entries.
    """

    BASE_URL = "https://www.topps.com/release-calendar"

    def __init__(
        self,
        base_url: str = BASE_URL,
        headless: bool = False,
        use_subprocess: bool = False,
        load_timeout_s: int = 30,
        polite_delay_s: float = 3.0,
    ) -> None:
        self.base_url = base_url
        self.headless = headless
        self.use_subprocess = use_subprocess
        self.load_timeout_s = load_timeout_s
        self.polite_delay_s = polite_delay_s
        self.driver: uc.Chrome | None = None

    def start(self) -> None:
        if self.driver is not None:
            return
        chrome_v = chrome_version.get_chrome_version()
        self.driver = uc.Chrome(
            headless=self.headless,
            use_subprocess=self.use_subprocess,
            version_main=int(chrome_v.split(".")[0]),
        )

    def close(self) -> None:
        if self.driver is None:
            return
        try:
            self.driver.quit()
        finally:
            self.driver = None

    def __enter__(self) -> ToppsReleaseScraper:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_started(self) -> None:
        if self.driver is None:
            raise RuntimeError("ToppsReleaseScraper not started. Call start() first.")

    def fetch_releases(self) -> list[ToppsRelease]:
        self._ensure_started()
        assert self.driver is not None

        self.driver.get(self.base_url)

        _DATE_PRESENT_RE = re.compile(
            r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+\d{1,2}"
            r"|\b\d{1,2}/\d{1,2}",
            re.IGNORECASE,
        )

        wait = WebDriverWait(self.driver, self.load_timeout_s)
        try:
            wait.until(
                lambda d: bool(
                    _DATE_PRESENT_RE.search(d.find_element(By.TAG_NAME, "body").text or "")
                )
            )
        except Exception:
            logger.warning("Topps release calendar did not render a recognizable date")

        try:
            wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "main, [class*='calendar']"))
            )
        except Exception:
            pass

        time.sleep(self.polite_delay_s)
        self._scroll_to_load_all()
        soup = BeautifulSoup(self.driver.page_source, "html.parser")
        return self._parse(soup)

    def _scroll_to_load_all(self) -> None:
        """Scroll incrementally to trigger IntersectionObserver-based lazy loading."""
        assert self.driver is not None
        viewport_h: int = self.driver.execute_script("return window.innerHeight") or 800
        total_h: int = self.driver.execute_script("return document.body.scrollHeight") or viewport_h
        y = 0
        while y < total_h:
            self.driver.execute_script(f"window.scrollTo(0, {y});")
            time.sleep(0.4)
            y += viewport_h
            total_h = self.driver.execute_script("return document.body.scrollHeight")
        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.0)

    # Slugs that are nav/footer pages, not product releases
    _NAV_SLUG_RE = re.compile(
        r"^(accessibility|privacy[-_]policy|terms[-_](of[-_])?(?:use|service|conditions?)|"
        r"fancash|contact[\w-]*|about[\w-]*|help[\w-]*|faq|shipping|returns|"
        r"careers|sitemap|cookie|legal)$",
        re.IGNORECASE,
    )

    # Anchor text that is a CTA rather than a product name
    _CTA_RE = re.compile(
        r"^(pre[-\s]?order|buy\s+now|shop\s+now|learn\s+more|order\s+now|"
        r"get\s+yours?|view\s+(all|details?)|available\s+now|sign\s+up|"
        r"notify\s+me|coming\s+soon|see\s+(details?|more))$",
        re.IGNORECASE,
    )

    def _parse(self, soup: BeautifulSoup) -> list[ToppsRelease]:
        results: list[ToppsRelease] = []
        seen: set[str] = set()

        self._collect_from_cards(soup, results, seen)
        # Fall back to generic approaches only if the targeted card pass found nothing
        # (e.g. the site redesigned away from the a.contents/heading-1 structure).
        if not results:
            self._collect_from_anchors(soup, results, seen)
            self._collect_from_headings(soup, results, seen)

        results.sort(key=lambda r: (r.release_date, r.name.lower()))
        return results

    def _collect_from_cards(
        self,
        soup: BeautifulSoup,
        results: list[ToppsRelease],
        seen: set[str],
    ) -> None:
        """Pass 0: target the Next.js SSR card structure directly.

        Each product card is an <a class="contents"> anchor that wraps:
          - a date label in a .pt-grid-gutter > .ui-2 element
          - a product title in an element with class matching heading-1

        This avoids the container-walk approach, which climbs too far and picks
        up dates from neighbouring cards when a card only shows "Drops in X".
        """
        today = date.today()
        for anchor in soup.find_all("a", class_="contents", href=True):
            href = anchor["href"]
            if not re.search(r"/(products?|releases?|collections?|pages?)/", href, re.IGNORECASE):
                continue

            heading = anchor.find(class_=re.compile(r"\bheading-1\b"))
            if not heading:
                continue
            name = heading.get_text(" ", strip=True)
            if not name or len(name) < 5 or self._CTA_RE.match(name):
                continue

            date_text = ""
            date_container = anchor.find(class_=re.compile(r"\bpt-grid-gutter\b"))
            if date_container:
                date_el = date_container.find(class_=re.compile(r"\bui-2\b"))
                if date_el:
                    date_text = date_el.get_text(" ", strip=True)

            if re.search(r"\bdrops?\s+in\b", date_text, re.IGNORECASE):
                release_date = today
            else:
                release_date = _parse_date(date_text)
                if release_date is None:
                    continue

            url = urljoin(self.base_url, href)
            slug_match = re.search(
                r"/(products?|releases?|collections?|pages?)/([^/?#]+)", url, re.IGNORECASE
            )
            source_id = slug_match.group(2) if slug_match else None
            is_pre_order = bool(_PRE_ORDER_RE.search(anchor.get_text(" ", strip=True)))

            dedup_key = f"{name.lower()}|{release_date.isoformat()}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            try:
                results.append(
                    ToppsRelease(
                        name=name,
                        release_date=release_date,
                        url=url,
                        source_id=source_id,
                        is_pre_order=is_pre_order,
                    )
                )
            except Exception as e:
                logger.warning("skipping unparseable release %r (card pass): %s", name, e)

    def _collect_from_anchors(
        self,
        soup: BeautifulSoup,
        results: list[ToppsRelease],
        seen: set[str],
    ) -> None:
        """Pass 1: anchor → product page (items with buy/pre-order links)."""
        anchors = soup.find_all("a", href=True)
        for anchor in anchors:
            href = anchor["href"]
            if not href:
                continue
            if not re.search(r"/(products?|releases?|collections?|pages?)/", href, re.IGNORECASE):
                continue

            slug_for_filter = href.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
            if self._NAV_SLUG_RE.match(slug_for_filter):
                continue

            container = anchor
            for _ in range(6):
                if container.parent is None:
                    break
                container = container.parent
                container_text = container.get_text(" ", strip=True)
                if _MONTH_RE.search(container_text):
                    break
            else:
                continue

            container_text = container.get_text(" ", strip=True)
            release_date = _parse_date(container_text)
            if release_date is None:
                continue

            is_pre_order = bool(_PRE_ORDER_RE.search(container_text))

            name = (anchor.get_text(" ", strip=True) or anchor.get("title") or "").strip()
            if not name:
                img = anchor.find("img")
                if img and img.get("alt"):
                    name = img["alt"].strip()

            if not name or self._CTA_RE.match(name):
                for search_root in (container, container.parent):
                    if search_root is None:
                        continue
                    for tag in ("h2", "h3", "h4", "h5", "h1", "strong"):
                        el = search_root.find(tag)
                        if el:
                            el_text = el.get_text(" ", strip=True)
                            if el_text and len(el_text) > 8 and not self._CTA_RE.match(el_text):
                                name = el_text
                                break
                    if name and not self._CTA_RE.match(name):
                        break
                    for img in search_root.find_all("img", alt=True):
                        alt = img.get("alt", "").strip()
                        if alt and len(alt) > 8 and not self._CTA_RE.match(alt):
                            name = alt
                            break
                    if name and not self._CTA_RE.match(name):
                        break

            if not name or self._CTA_RE.match(name):
                continue

            url = urljoin(self.base_url, href)
            slug_match = re.search(
                r"/(products?|releases?|collections?|pages?)/([^/?#]+)", url, re.IGNORECASE
            )
            source_id = slug_match.group(2) if slug_match else None

            dedup_key = f"{name.lower()}|{release_date.isoformat()}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            try:
                results.append(
                    ToppsRelease(
                        name=name,
                        release_date=release_date,
                        url=url,
                        source_id=source_id,
                        is_pre_order=is_pre_order,
                    )
                )
            except Exception as e:
                logger.warning("skipping unparseable release %r: %s", name, e)

    def _collect_from_headings(
        self,
        soup: BeautifulSoup,
        results: list[ToppsRelease],
        seen: set[str],
    ) -> None:
        """Pass 2: heading → date container (items with no product link yet).

        Catches upcoming releases that only show a "Notify me" button and have
        no /products/ or /collections/ anchor — invisible to pass 1.
        """
        for heading_tag in ("h2", "h3", "h4"):
            for heading in soup.find_all(heading_tag):
                name = heading.get_text(" ", strip=True)
                if not name or len(name) < 8 or self._CTA_RE.match(name):
                    continue

                container = heading
                for _ in range(6):
                    if container.parent is None:
                        break
                    container = container.parent
                    container_text = container.get_text(" ", strip=True)
                    if _MONTH_RE.search(container_text):
                        break
                else:
                    continue

                container_text = container.get_text(" ", strip=True)
                release_date = _parse_date(container_text)
                if release_date is None:
                    continue

                dedup_key = f"{name.lower()}|{release_date.isoformat()}"
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                is_pre_order = bool(_PRE_ORDER_RE.search(container_text))

                # Best-effort: pick up a product URL if one exists in the card
                url: str | None = None
                source_id: str | None = None
                for a in container.find_all("a", href=True):
                    href = a["href"]
                    if re.search(r"/(products?|releases?|collections?)/", href, re.IGNORECASE):
                        slug = href.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
                        if not self._NAV_SLUG_RE.match(slug):
                            url = urljoin(self.base_url, href)
                            sm = re.search(
                                r"/(products?|releases?|collections?)/([^/?#]+)",
                                url,
                                re.IGNORECASE,
                            )
                            source_id = sm.group(2) if sm else None
                            break

                try:
                    results.append(
                        ToppsRelease(
                            name=name,
                            release_date=release_date,
                            url=url,
                            source_id=source_id,
                            is_pre_order=is_pre_order,
                        )
                    )
                except Exception as e:
                    logger.warning("skipping unparseable release %r (heading pass): %s", name, e)
