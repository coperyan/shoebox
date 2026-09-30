"""Real-browser session against tcdb.com.

TCDB sits behind Cloudflare, so plain HTTP gets an interstitial. We drive a
visible Chrome via undetected-chromedriver, which clears the managed challenge
on its own, and keep a persistent Chrome profile so the Cloudflare clearance
and the TCDB login cookie survive between runs.

Credentials are never handled here: ``ensure_logged_in`` opens the login page
and waits for you to sign in by hand in the browser window.

Each site action is a small method on ``TcdbBrowser`` (``advanced_search``,
``open`` ...). To automate a new TCDB page, add a parser to ``search.py`` (or a
sibling module) and a one-method wrapper here that navigates and hands the HTML
to the parser.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from shoebox.clients.tcdb.card import parse_card_page, parse_collection_widget
from shoebox.clients.tcdb.search import is_challenge_page, is_logged_in, parse_results
from shoebox.models.tcdb import (
    TCDB_BASE_URL,
    AdvancedSearchQuery,
    CollectionAddResult,
    TcdbSearchPage,
)

logger = logging.getLogger(__name__)

LOGIN_URL = f"{TCDB_BASE_URL}/Login.cfm"


class TcdbLoginTimeout(TimeoutError):
    """Raised when the user does not finish logging in within the allowed time."""


class TcdbBrowser:
    """undetected-chromedriver session with a persistent profile.

    Usable as a context manager::

        with TcdbBrowser(profile_dir="data/tcdb_chrome_profile") as tcdb:
            tcdb.ensure_logged_in()
            page = tcdb.advanced_search(AdvancedSearchQuery(name="Bonds", card_number="25"))
    """

    def __init__(
        self,
        profile_dir: str | Path,
        *,
        headless: bool = False,
        base_url: str = TCDB_BASE_URL,
        challenge_timeout_s: float = 90,
        settle_delay_s: float = 0.5,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.base_url = base_url.rstrip("/")
        self.challenge_timeout_s = challenge_timeout_s
        self.settle_delay_s = settle_delay_s
        self.driver = None

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        if self.driver is not None:
            return
        import chrome_version
        import undetected_chromedriver as uc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        chrome_v = chrome_version.get_chrome_version()
        self.driver = uc.Chrome(
            headless=self.headless,
            use_subprocess=True,
            version_main=int(chrome_v.split(".")[0]),
            user_data_dir=str(self.profile_dir.resolve()),
        )
        logger.info("TCDB browser started (profile %s)", self.profile_dir)

    def close(self) -> None:
        if self.driver is None:
            return
        try:
            self.driver.quit()
        except Exception:  # driver already gone (window closed by hand)
            logger.debug("driver.quit() failed; ignoring", exc_info=True)
        finally:
            self.driver = None

    def __enter__(self) -> TcdbBrowser:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_started(self) -> None:
        if self.driver is None:
            raise RuntimeError("TcdbBrowser not started. Call start() first.")

    # -- navigation ----------------------------------------------------------------

    @property
    def page_source(self) -> str:
        self._ensure_started()
        return self.driver.page_source

    @property
    def current_url(self) -> str:
        self._ensure_started()
        return self.driver.current_url

    def get(self, url: str) -> str:
        """Navigate, wait out any Cloudflare interstitial, return the page HTML."""
        self._ensure_started()
        self.driver.get(url)
        self._wait_for_challenge()
        if self.settle_delay_s:
            time.sleep(self.settle_delay_s)
        return self.driver.page_source

    def _wait_for_challenge(self) -> None:
        deadline = time.monotonic() + self.challenge_timeout_s
        warned = False
        while is_challenge_page(self.driver.title):
            if not warned:
                logger.info("Waiting for the Cloudflare check on tcdb.com to clear...")
                warned = True
            if time.monotonic() > deadline:
                raise TimeoutError(
                    "tcdb.com is still showing the Cloudflare check. If it wants a click, "
                    "do it in the browser window and re-run."
                )
            time.sleep(1)

    def open(self, url: str) -> None:
        """Show a page in the browser window without parsing it (e.g. a card page)."""
        self.get(url)

    # -- auth ----------------------------------------------------------------------

    def is_logged_in(self) -> bool:
        return is_logged_in(self.page_source)

    def ensure_logged_in(
        self,
        *,
        timeout_s: float = 300,
        prompt: Callable[[str], None] = print,
        poll_s: float = 1.0,
    ) -> bool:
        """Make sure this browser session is signed in to TCDB.

        Returns True if already signed in (persistent profile). Otherwise shows the
        login page, asks the user via ``prompt`` to sign in by hand, and polls until
        the header shows a signed-in state. Raises ``TcdbLoginTimeout`` if that does
        not happen within ``timeout_s``.
        """
        self.get(LOGIN_URL)
        if self.is_logged_in():
            logger.info("TCDB: already logged in")
            return True

        prompt(
            "Please log in to TCDB in the Chrome window that just opened "
            f"(waiting up to {int(timeout_s)}s; tick 'Remember' to skip this next time)."
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            try:
                if self.is_logged_in():
                    logger.info("TCDB: login detected")
                    return False
            except Exception:  # page mid-navigation
                continue
        raise TcdbLoginTimeout(f"No TCDB login detected within {int(timeout_s)}s")

    # -- actions -------------------------------------------------------------------

    def advanced_search(self, query: AdvancedSearchQuery) -> TcdbSearchPage:
        """Run one advanced search and leave the results showing in the browser."""
        url = query.to_url(self.base_url)
        logger.info("TCDB advanced search: %s", query.describe())
        html = self.get(url)
        page = parse_results(html, query_url=url, base_url=self.base_url)
        logger.info("TCDB returned %s result(s)", page.total_results)
        return page

    def add_to_collection(
        self,
        card_url: str,
        *,
        allow_duplicate: bool = False,
        timeout_s: float = 20,
        poll_s: float = 0.25,
    ) -> CollectionAddResult:
        """Open a ViewCard.cfm page and add the card to the collection it shows.

        Uses the page's own controls rather than calling TCDB's endpoints
        directly: waits for the ``#colDiv`` collection box to load, clicks
        ``#quickAddBtn`` (or ``.addAnotherBtn`` for an extra copy), and watches
        the ``CollectionAdd*_ajax.cfm`` request the click makes. A card that is
        already in the collection is skipped unless ``allow_duplicate``.
        """
        card = parse_card_page(self.get(card_url), url=card_url, base_url=self.base_url)
        widget_html = self._wait_for_collection_widget(timeout_s, poll_s)
        widget = parse_collection_widget(widget_html)

        def result(status: str, detail: str = "", **kw) -> CollectionAddResult:
            return CollectionAddResult(
                status=status, card=card, detail=detail, widget_html=widget_html, **kw
            )

        if not widget.loaded:
            return result("failed", f"collection box did not load within {int(timeout_s)}s")
        if widget.owned and not allow_duplicate:
            return result("already_owned", "already in the collection")

        if widget.owned and widget.add_another:
            selector = ".addAnotherBtn"
        elif widget.quick_add:
            selector = "#quickAddBtn"
        else:
            return result("no_add_button", "no Quick Add button in the collection box")

        self.driver.execute_script(_WATCH_COLLECTION_REQUESTS_JS)
        clicked = self.driver.execute_script(
            "const el = document.querySelector('#colDiv ' + arguments[0]);"
            "if (!el) return false; el.click(); return true;",
            selector,
        )
        if not clicked:
            return result("no_add_button", f"{selector} disappeared before it could be clicked")

        request = self._wait_for_add_request(timeout_s, poll_s)
        if request is None:
            return result("failed", f"no add request seen within {int(timeout_s)}s")
        if not request.get("ok"):
            return result(
                "failed",
                f"TCDB answered HTTP {request.get('status')} {request.get('error') or ''}".strip(),
                request_url=request.get("url"),
            )

        # The page reloads the collection box after a successful add.
        deadline = time.monotonic() + timeout_s
        after = widget_html
        while time.monotonic() < deadline:
            after = self._collection_widget_html()
            if after.strip() and after != widget_html:
                break
            time.sleep(poll_s)
        verified = parse_collection_widget(after).owned
        logger.info("TCDB: added %s (verified=%s)", card.title, verified)
        return CollectionAddResult(
            status="added",
            card=card,
            request_url=request.get("url"),
            verified=verified,
            detail="" if verified else "collection box did not refresh to show it",
        )

    def _collection_widget_html(self) -> str:
        return (
            self.driver.execute_script(
                "const d = document.getElementById('colDiv'); return d ? d.innerHTML : '';"
            )
            or ""
        )

    def _wait_for_collection_widget(self, timeout_s: float, poll_s: float) -> str:
        deadline = time.monotonic() + timeout_s
        while True:
            html = self._collection_widget_html()
            if html.strip() or time.monotonic() > deadline:
                return html
            time.sleep(poll_s)

    def _wait_for_add_request(self, timeout_s: float, poll_s: float) -> dict | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            requests = self.driver.execute_script(
                "return (window.__shoebox && window.__shoebox.requests) || [];"
            )
            for req in requests or []:
                if "CollectionAdd" in (req.get("url") or ""):
                    return req
            time.sleep(poll_s)
        return None


# Wraps window.fetch (which the ViewCard page's collection buttons call) to record
# every Collection*_ajax.cfm request and its HTTP status in window.__shoebox.requests.
# Idempotent: the wrapper is installed once per page; the log is reset each call.
_WATCH_COLLECTION_REQUESTS_JS = """
if (!window.__shoebox) {
  window.__shoebox = {requests: []};
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = String((input && input.url) || input);
    const p = origFetch.apply(this, arguments);
    if (/Collection\\w*_ajax\\.cfm/i.test(url)) {
      p.then(
        r => window.__shoebox.requests.push({url: url, ok: r.ok, status: r.status}),
        e => window.__shoebox.requests.push({url: url, ok: false, status: 0, error: String(e)})
      );
    }
    return p;
  };
}
window.__shoebox.requests = [];
"""
