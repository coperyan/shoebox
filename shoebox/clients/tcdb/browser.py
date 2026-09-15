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

from shoebox.clients.tcdb.search import is_challenge_page, is_logged_in, parse_results
from shoebox.models.tcdb import TCDB_BASE_URL, AdvancedSearchQuery, TcdbSearchPage

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
