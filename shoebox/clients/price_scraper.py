from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

import chrome_version
import pandas as pd
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

parallel_terms = [
    "Blue Refractor",
    "Blue Border",
    "Magenta",
    "Pink",
    "Raywave",
    "Sepia",
    "Prism",
    "Purple",
    "Yellow",
    "Mojo",
    "Gold",
    "Orange",
    "Black",
    "Canvas",
    "Acetate",
    "X-Fractor",
    "Negative",
    "Reptilian",
    "Pulsar",
    "Geometric",
]
sp_terms = ["Golden Mirror", "Image Variation", "SSP"]
auto_terms = ["Auto"]
relic_terms = ["MEM", "Relic"]
numbered = [
    "/5",
    "/10",
    "/25",
    "/50",
    "/75",
    "/99",
    "/125",
    "/150",
    "/175",
    "/199",
    "/249",
    "/299",
]


def search_helper(
    set_name: str,
    subset_type: str,
    subset_name: str,
    player: str,
    parallel_variety: str = None,
    print_run: str = None,
) -> list:
    title = ""
    set_name_clean = (
        set_name.replace("Bowman ", "")
        .replace("Topps ", "")
        .replace("Series 1 ", "")
        .replace("Series 2 ", "")
        .replace("Baseball ", "")
    )
    title += f"{set_name_clean}"
    if subset_type == "Insert":
        subset_name_clean = (
            subset_name.replace("Bowman ", "")
            .replace("Topps ", "")
            .replace("Series 1 ", "")
            .replace("Series 2 ", "")
            .replace("Baseball ", "")
            .replace("Football ", "")
            .replace("Holo Foil", "Foil")
            .replace("Holofoil", "Foil")
        )
        title += f" {subset_name_clean}"
    if parallel_variety:
        title += f" {parallel_variety}"
    title += f" {player}"
    if print_run:
        title += f" /{int(str(print_run).replace('.0', ''))}"
    ex_terms = ["PSA", "BGS", "SGC"]
    if not parallel_variety:
        ex_terms.extend([x for x in parallel_terms if x.lower() not in title.lower()])
    else:
        ex_terms.extend([x for x in parallel_terms if x.lower() not in parallel_variety.lower()])
    if "Auto".lower() not in subset_name.lower():
        ex_terms.extend([x for x in auto_terms])
    if "Relic".lower() not in subset_name.lower():
        ex_terms.extend([x for x in relic_terms])
    if not print_run:
        ex_terms.extend([x for x in numbered])
    return title, ex_terms


class PriceScraper:
    def __init__(
        self,
        base_url: str = "https://www.130point.com/search",
        headless: bool = False,
        use_subprocess: bool = False,
        polite_delay_s: float = 5,
    ) -> None:
        self.base_url = base_url
        self.headless = headless
        self.use_subprocess = use_subprocess
        self.polite_delay_s = polite_delay_s
        self.driver = None

    def start(self) -> None:
        if self.driver is not None:
            return
        chrome_v = chrome_version.get_chrome_version()
        self.driver = uc.Chrome(
            headless=self.headless,
            use_subprocess=self.use_subprocess,
            version_main=int(chrome_v.split(".")[0]),
        )
        self.driver.get(self.base_url)

    def close(self) -> None:
        if self.driver is None:
            return
        try:
            self.driver.quit()
        finally:
            self.driver = None

    @staticmethod
    def price_averages_with_outliers(
        df: pd.DataFrame,
        price_col: str = "sold_price",
        method: str = "mad",
        mad_z: float = 3.5,
        iqr_k: float = 1.5,
        min_n: int = 8,
    ) -> dict[str, float | None]:
        d = df.copy()
        if price_col not in d.columns:
            return {
                "raw_mean": None,
                "raw_median": None,
                "trimmed_mean": None,
                "trimmed_median": None,
                "n_total": 0,
                "n_used": 0,
                "n_outliers": 0,
            }
        d[price_col] = pd.to_numeric(d[price_col], errors="coerce")
        d = d.dropna(subset=[price_col])
        d = d[d[price_col] > 0]

        n_total = len(d)
        if n_total == 0:
            return {
                "raw_mean": None,
                "raw_median": None,
                "trimmed_mean": None,
                "trimmed_median": None,
                "n_total": 0,
                "n_used": 0,
                "n_outliers": 0,
            }

        raw_mean = float(d[price_col].mean())
        raw_median = float(d[price_col].median())

        if n_total < min_n:
            return {
                "raw_mean": raw_mean,
                "raw_median": raw_median,
                "trimmed_mean": raw_mean,
                "trimmed_median": raw_median,
                "n_total": n_total,
                "n_used": n_total,
                "n_outliers": 0,
            }

        if method.lower() == "iqr":
            q1 = d[price_col].quantile(0.25)
            q3 = d[price_col].quantile(0.75)
            iqr = q3 - q1
            if iqr == 0:
                mask = pd.Series(True, index=d.index)
            else:
                mask = (d[price_col] >= q1 - iqr_k * iqr) & (d[price_col] <= q3 + iqr_k * iqr)
        else:
            med = float(d[price_col].median())
            abs_dev = (d[price_col] - med).abs()
            mad = float(abs_dev.median())
            if mad == 0:
                mask = pd.Series(True, index=d.index)
            else:
                mz = 0.6745 * (d[price_col] - med) / mad
                mask = mz.abs() <= mad_z

        trimmed = d.loc[mask]
        return {
            "raw_mean": raw_mean,
            "raw_median": raw_median,
            "trimmed_mean": float(trimmed[price_col].mean()) if len(trimmed) else None,
            "trimmed_median": (float(trimmed[price_col].median()) if len(trimmed) else None),
            "n_total": n_total,
            "n_used": len(trimmed),
            "n_outliers": n_total - len(trimmed),
        }

    def _ensure_started(self) -> None:
        if self.driver is None:
            raise RuntimeError("PriceScraper not started. Call start() first.")

    def exclude_str_check(self, t: str, excludes: list[str]) -> bool:
        if not t or not excludes:
            return False
        return any(x.lower() in t.lower() for x in excludes)

    @staticmethod
    def _parse_price(text: str) -> float | None:
        """
        FIX: requires '$' so years like '2025' in card titles are never matched.
        """
        if not text or "$" not in text:
            return None
        m = re.search(r"\$\s*([\d,]+\.?\d*)", text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
        return None

    _DATE_FMTS = [
        "%a %d %b %Y %H:%M:%S",  # old format: 'Sun 28 Dec 2025 19:08:30' (timezone stripped)
        "%b %d, %Y %I:%M %p",  # 'Apr 23, 2025 3:45 PM'
        "%b %d, %Y",  # 'Apr 23, 2025'
        "%B %d, %Y",  # 'April 23, 2025'
        "%d %b %Y",  # '23 Apr 2025'
        "%Y-%m-%d",  # '2025-04-23'
    ]

    @classmethod
    def _parse_date(cls, s: str) -> dict[str, Any]:
        out: dict[str, Any] = {"sold_date": None, "days_ago": None, "sold_date_str": s}
        if not s:
            return out
        cleaned = re.sub(r"\s+[A-Z]{2,4}$", "", s.strip())
        for fmt in cls._DATE_FMTS:
            try:
                dt = datetime.strptime(cleaned, fmt)
                out["sold_date"] = dt
                out["days_ago"] = (datetime.now() - dt).days
                return out
            except ValueError:
                continue
        return out

    def _do_search(self, query: str) -> None:
        wait = WebDriverWait(self.driver, 20)
        selectors = [
            'nav[data-nav="desktop"] input[type="text"]',
            'input[placeholder*="Search"]',
            'input[type="text"]',
        ]
        search_el = None
        for sel in selectors:
            try:
                search_el = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
                break
            except Exception:
                continue
        if search_el is None:
            raise RuntimeError("Could not locate search input on 130point.")

        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", search_el)
        time.sleep(0.2)
        try:
            search_el.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", search_el)

        try:
            search_el.send_keys(Keys.CONTROL + "a")
            search_el.send_keys(Keys.DELETE)
            search_el.clear()
        except Exception:
            pass

        search_el.send_keys(query)
        search_el.send_keys(Keys.RETURN)

    def _parse_card(self, card) -> dict[str, Any] | None:
        p: dict[str, Any] = {}

        anchor = card if card.name == "a" else card.find("a", href=True)
        if not anchor:
            return None
        p["url"] = anchor.get("href")

        img = card.find("img")
        title = img.get("alt", "").strip() if img else card.get_text(" ", strip=True)[:200]
        p["title"] = title or None

        # Price: data-price attr → child element attr → $ text scan (never bare numbers)
        raw_price = card.get("data-price") or anchor.get("data-price")
        if raw_price is not None:
            try:
                p["sale_price"] = float(raw_price)
            except (TypeError, ValueError):
                p["sale_price"] = None
        else:
            price_el = card.find(attrs={"data-price": True})
            if price_el:
                try:
                    p["sale_price"] = float(price_el["data-price"])
                except (TypeError, ValueError):
                    p["sale_price"] = None
            else:
                p["sale_price"] = None
                for text in card.stripped_strings:
                    if "$" not in text:  # ← key guard: skip any text without $
                        continue
                    candidate = self._parse_price(text)
                    if candidate and 0.01 < candidate < 100_000:
                        p["sale_price"] = candidate
                        break

        # Sale type
        p["type"] = None
        for keyword in ("Auction", "Buy now", "Best offer", "Fixed"):
            if card.find(string=re.compile(keyword, re.IGNORECASE)):
                p["type"] = keyword
                break

        # Bid count
        p["bid_count"] = None
        if p["type"] == "Auction":
            bid_el = card.find(string=re.compile(r"\d+\s*bid", re.IGNORECASE))
            if bid_el:
                m = re.search(r"(\d+)", bid_el)
                p["bid_count"] = m.group(1) if m else None

        # Date: find an element whose text contains a month name AND a 4-digit year
        date_text = None
        month_re = re.compile(
            r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?)\b",
            re.IGNORECASE,
        )
        for el in card.find_all(["span", "div", "p", "time", "small"]):
            txt = el.get_text(strip=True)
            if month_re.search(txt) and re.search(r"\d{4}", txt):
                date_text = txt
                break

        p.update(self._parse_date(date_text))
        return p

    def search(
        self,
        query: str,
        exclude_strs: list = None,
        rows_per_page: int | None = None,
    ) -> pd.DataFrame:
        self._ensure_started()
        wait = WebDriverWait(self.driver, 20)

        self.driver.get(self.base_url)
        self._do_search(query)

        wait.until(EC.presence_of_element_located((By.ID, "sold-results-panel")))
        try:
            wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#sold-results-panel a[href]"))
            )
        except Exception:
            pass
        time.sleep(self.polite_delay_s)

        soup = BeautifulSoup(self.driver.page_source, "html.parser")
        panel = soup.find(id="sold-results-panel")
        if not panel:
            return pd.DataFrame()

        seen_urls: set = set()
        parsed: list[dict[str, Any]] = []

        candidates = panel.find_all("a", href=True) or panel.find_all(
            ["div", "article"], recursive=False
        )

        for candidate in candidates:
            try:
                record = self._parse_card(candidate)
            except Exception:
                continue
            if not record:
                continue
            url = record.get("url", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            title = record.get("title", "") or ""
            if exclude_strs and self.exclude_str_check(t=title, excludes=exclude_strs):
                continue
            parsed.append(record)

        return pd.json_normalize(parsed) if parsed else pd.DataFrame()

    def search_with_averages(
        self,
        query: str = None,
        excluded_terms: list = None,
        rows_per_page: int | None = None,
        exclude_strs: list[str] = None,
        price_col: str = "sale_price",
        method: str = "mad",
        days: int | None = None,
    ) -> dict[str, Any]:
        df = self.search(query, exclude_strs=exclude_strs, rows_per_page=rows_per_page)
        averages_all = self.price_averages_with_outliers(df, price_col=price_col, method=method)

        df_filtered = df
        if days is not None and "days_ago" in df.columns:
            df_filtered = df[df["days_ago"].notna() & (df["days_ago"] <= days)]
        averages_filtered = self.price_averages_with_outliers(
            df_filtered, price_col=price_col, method=method
        )

        df_filtered_2 = df.copy()
        if excluded_terms:
            df_filtered_2["excluded"] = df_filtered_2.apply(
                lambda x: (
                    1
                    if any(exc.lower() in (x.get("title") or "").lower() for exc in excluded_terms)
                    else 0
                ),
                axis=1,
            )
            df_filtered_2 = df_filtered_2[df_filtered_2["excluded"] == 0].reset_index(drop=True)
        else:
            df_filtered_2["excluded"] = 0

        averages_filtered_terms = self.price_averages_with_outliers(
            df_filtered_2, price_col=price_col, method=method
        )

        return {
            "query": query,
            "df": df,
            "df_filtered_2": df_filtered_2,
            "averages_all": averages_all,
            "averages_filtered": averages_filtered,
            "averages_filtered_terms": averages_filtered_terms,
        }
