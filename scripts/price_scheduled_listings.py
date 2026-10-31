"""Ad hoc: price scheduled listings from 130point sold comps, then push the prices.

Picks the scheduled listings (GetMyeBaySelling ``ScheduledList``) whose title
contains ``--title-contains``, and runs one deliberately broad 130point search
per player: the search text plus the player's last name, e.g.
``2026 topps holiday kurtz``. Every comp that comes back is then matched to
each listing locally -- same player, same year, same card (its card number, or
failing that the insert's name), same parallel words -- so one search prices
every card of that player and nothing is lost to a seller's word order.

    python scripts/price_scheduled_listings.py --title-contains "2026 Topps Holiday"
    python scripts/price_scheduled_listings.py --from-plan exports/xlsx/price_scheduled_listings_working.xlsx
    python scripts/price_scheduled_listings.py --from-plan exports/xlsx/price_scheduled_listings_working.xlsx --apply

The scrape writes ``exports/xlsx/price_scheduled_listings_<stamp>.xlsx`` and
refreshes ``price_scheduled_listings_working.xlsx``. Its ``plan`` sheet has one
row per listing (current price, comp count, median, suggested price) and a
``new_price`` column, pre-filled with the suggestion, to hand-edit; ``comps``
has every sold row with the listings it counted toward, or why it was left
out. A blank ``new_price`` leaves that listing alone.

Nothing is sent to eBay without ``--apply``, which only ever reads a saved plan.
Each listing is an updateOffer PUT with the new price and its existing
``listingStartDate``, so it stays scheduled for the same time.
"""

from __future__ import annotations

import argparse
import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pandas as pd

from shoebox.clients.ebay.client import EbayClient
from shoebox.clients.price_scraper import PriceScraper
from shoebox.settings import get_settings
from shoebox.transforms.listing_builder import rebuild_offer_body
from shoebox.utils.logging_setup import setup_logging
from shoebox.utils.pricing import round_up_to_nine

logger = logging.getLogger(__name__)

WORKING_PLAN = "xlsx/price_scheduled_listings_working.xlsx"

# GetItem is one call per listing and pure network wait; overlap them.
_DEFAULT_WORKERS = 8

_MIN_PRICE = 0.99

# Below this share of titles naming the searched player, a result set is stale.
_MIN_NAME_SHARE = 0.3

_NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}

# Comps that aren't a raw single of the card.
_EXCLUDE_TERMS = [
    "psa", "bgs", "sgc", "cgc", "beckett", "graded", "lot", "auto", "signed", "relic",
    "patch", "you pick", "pick your", "choose", "complete", "team set", "break", "pyp",
]  # fmt: skip

# Words that name a parallel or finish. A comp counts toward a listing only when
# it carries exactly the listing's set of these (after team names are removed).
_FINISH_WORDS = {
    "holo", "rainbow", "gold", "orange", "black", "pink", "yellow", "silver", "purple",
    "red", "blue", "green", "light", "glitter", "snow", "snowflake", "metallic", "sparkle",
    "wave", "refractor", "prizm", "mojo", "sepia", "xfractor", "chrome", "variation",
    "backs", "plate", "finest", "heritage", "bowman", "mosaic", "inception", "prism",
    "raywave", "aqua", "magenta", "lava", "geometric", "negative", "speckle", "diamond",
    "teal", "crackle", "canvas", "acetate", "pulsar", "reptilian", "atomic", "shimmer",
    "logofractor", "fuchsia", "rose", "leather", "lightboard", "logo", "apple", "hongbao",
    "mini", "white", "holiday", "golden", "mirror", "statue", "liberty", "hotdog", "hotdogs",
    "dog", "dogs", "color", "photo", "boxing", "gloves", "fireworks", "umbrella", "foilboard",
    "lunar", "glow",
}  # fmt: skip

# Used too loosely by sellers to decide on either way.
_WEAK_WORDS = {"foil", "sp", "ssp", "short", "print", "parallel", "image", "printing"}

_SYNONYMS = {"back": "backs"}

# Releases that share a year and a card-number scheme (Series 1's holiday
# variations, Chrome vs Chrome Update). A comp must name the same ones the
# listing's title does.
_PRODUCT_WORDS = {"series", "series1", "series2", "update", "sapphire", "platinum", "cosmic", "high"}

# Team names with a finish word in them, removed before finish words are read.
_TEAM_NAMES = ["red sox", "white sox", "blue jays", "green bay"]

# Words in an insert's name that don't tell one insert from another.
_INSERT_STOP_WORDS = {
    "topps", "the", "of", "and", "football", "baseball", "basketball", "hockey",
    "set", "insert", "card", "cards",
}  # fmt: skip


def last_name(player: str) -> str:
    """``"Jazz Chisholm Jr."`` -> ``"Chisholm"``."""
    tokens = [t for t in player.split() if t.lower() not in _NAME_SUFFIXES]
    return tokens[-1] if tokens else player


def tokens(text: str) -> set[str]:
    """Lowercased words and print runs (``/299``) in ``text``, minus team names.

    Accents are dropped (``Acuña`` -> ``acuna``) and ``X-Fractor`` is one word.
    """
    t = unicodedata.normalize("NFKD", f" {text.lower()} ").encode("ascii", "ignore").decode()
    t = re.sub(r"\bx[\s-]?fractor", "xfractor", t)
    # "Series 1", "Series One" and "S1" are one product word, so Series 1 and
    # Series 2 comps don't price each other's cards.
    t = re.sub(r"\b(?:series\s*(?:1|one)|s1)\b", "series1", t)
    t = re.sub(r"\b(?:series\s*(?:2|two)|s2)\b", "series2", t)
    for team in _TEAM_NAMES:
        t = t.replace(team, " ")
    return {_SYNONYMS.get(w, w) for w in re.findall(r"/\d+|[a-z0-9]+", t)}


def finish(words: set[str]) -> frozenset[str]:
    """The parallel-naming words in ``words``; print runs count."""
    return frozenset(w for w in words if w in _FINISH_WORDS or w.startswith("/"))


def card_number_pattern(number: str) -> re.Pattern | None:
    """Matches ``GOTG-7`` as ``#GOTG-7``, ``GOTG 7`` or ``gotg7``, but not ``GOTG-70``.

    A lettered number also matches its digits after a ``#`` (``H166`` as
    ``#166``), since sellers often drop the prefix.
    """
    compact = re.sub(r"[^a-z0-9]", "", number.lower())
    if not compact:
        return None
    pattern = r"(?<![a-z0-9])" + r"[\s#\-]*".join(map(re.escape, compact)) + r"(?![a-z0-9])"
    digits = re.search(r"\d+$", compact)
    if digits and digits.group() != compact:
        pattern += rf"|#\s*{digits.group()}(?![0-9])"
    return re.compile(pattern)


def insert_tokens(insert: str, search_words: set[str]) -> frozenset[str]:
    """Words that identify an insert: ``Greats Of The Game`` -> ``{greats, game}``."""
    words = tokens(insert) - _INSERT_STOP_WORDS - _FINISH_WORDS - _WEAK_WORDS - search_words
    return frozenset(w for w in words if not (w.isdigit() and len(w) == 4 and w >= "2000"))


def load_scheduled(
    ebay_api: EbayClient,
    title_contains: str,
    workers: int,
    title_excludes: list[str] | None = None,
    search_prefix: str | None = None,
) -> pd.DataFrame:
    """Matching scheduled listings with player, card, finish and the search to run.

    ``title_excludes`` drops listings whose title also has any of those strings
    ("High Number" when pricing plain Heritage). ``search_prefix`` replaces
    ``title_contains`` as the 130point search text ("2024 Allen & Ginter" for
    sellers who leave out "Topps"); matching still uses ``title_contains``.
    """
    needle = title_contains.casefold()
    excludes = [x.casefold() for x in title_excludes or []]
    rows = [
        r
        for r in ebay_api.trading.get_scheduled_listings()
        if needle in (r.get("title") or "").casefold()
        and not any(x in (r.get("title") or "").casefold() for x in excludes)
        and r.get("sku")
    ]
    logger.info("%d scheduled listing(s) match %r", len(rows), title_contains)
    prefix = search_prefix or title_contains

    def specifics(item_id: str) -> dict:
        return ebay_api.trading.get_item_details(item_id=item_id).get("item_specifics") or {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        all_specifics = list(pool.map(specifics, [r["item_id"] for r in rows]))

    def first(spec: dict, key: str) -> str:
        value = (spec.get(key) or [""])[0]
        return "" if value in ("Base", "[Base]") else value

    search_words = tokens(title_contains)
    out = []
    for row, spec in zip(rows, all_specifics, strict=True):
        player = " ".join(spec.get("Player/Athlete") or [])
        insert, parallel = first(spec, "Insert Set"), first(spec, "Parallel/Variety")
        print_run = first(spec, "Print Run")
        finish_text = f"{insert} {parallel} {'/' + print_run if print_run else ''}"
        out.append(
            {
                "item_id": row["item_id"],
                "sku": row["sku"],
                "title": row["title"],
                "start_time": row["start_time"],
                "current_price": float(row["price"]),
                "player": player,
                "last_name": last_name(player) if player else None,
                "team_card": bool(player) and player == first(spec, "Team"),
                "card_number": first(spec, "Card Number"),
                "variant": " ".join(x for x in (insert, parallel) if x) or "base",
                "insert": insert,
                "finish": " ".join(sorted(finish(tokens(finish_text) - search_words))),
                "query": f"{prefix} {last_name(player)}".lower() if player else None,
            }
        )
    return pd.DataFrame(out)


def _fresh_scraper() -> PriceScraper:
    scraper = PriceScraper()
    scraper.start()
    # A hung 130point load otherwise blocks for selenium's full 120s read timeout.
    scraper.driver.set_page_load_timeout(45)
    return scraper


def _looks_stale(df: pd.DataFrame, query: str) -> bool:
    """True when most rows don't name the searched player.

    When 130point is slow, a new search can leave the previous one's grid on
    the page, and the scraper reads it back as this query's results.
    """
    name = tokens(query.split()[-1])
    if df.empty or "title" not in df.columns:
        return False
    hits = df["title"].fillna("").map(lambda t: name <= tokens(t)).mean()
    return hits < _MIN_NAME_SHARE


def scrape(queries: list[str], attempts: int = 3) -> pd.DataFrame:
    """Every sold row for each query, tagged with the query that found it.

    A failed or stale search restarts the browser and is retried; a query that
    never comes back clean is left out rather than priced from someone else's
    sales.
    """
    scraper = _fresh_scraper()
    frames = []
    try:
        for n, query in enumerate(queries, 1):
            logger.info("Scraping %d of %d: %r", n, len(queries), query)
            for attempt in range(1, attempts + 1):
                try:
                    df = scraper.search(query)
                    if not _looks_stale(df, query):
                        break
                    problem = "results are for another search"
                except Exception as e:
                    problem = str(e).splitlines()[0]
                logger.warning("%r attempt %d/%d: %s", query, attempt, attempts, problem)
                scraper.close()
                time.sleep(15 * attempt)
                scraper = _fresh_scraper()
            else:
                logger.warning("Giving up on %r", query)
                continue
            if not df.empty:
                frames.append(df.assign(query=query))
    finally:
        scraper.close()
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def exclusion_reason(title: str, year: str) -> str | None:
    """Why a comp can't price any listing: wrong year, or not a raw single."""
    t = title.lower()
    if year and year not in t:
        return f"no {year}"
    hit = next((x for x in _EXCLUDE_TERMS if re.search(rf"\b{re.escape(x)}\b", t)), None)
    return f"'{hit}'" if hit else None


class _Card:
    """A listing, reduced to what a comp title has to agree with."""

    def __init__(self, row: pd.Series, search_words: set[str]):
        self.last = tokens(row["last_name"])
        # A bare last name picks up other players (Julien Duranville for Edouard
        # Julien), so a first-name word is required too -- or the words run
        # together, for J.P. -> jp.
        plain = unicodedata.normalize("NFKD", row["player"].lower()).encode("ascii", "ignore")
        given = [
            w
            for w in re.findall(r"[a-z0-9]+", plain.decode())
            if w not in self.last and w not in _NAME_SUFFIXES
        ]
        self.first = set(given) | ({"".join(given)} if given else set())
        self.team_card = bool(row["team_card"])
        self.number = card_number_pattern(row["card_number"] or "")
        self.insert = insert_tokens(row["insert"], search_words)
        self.finish = frozenset(row["finish"].split())
        self.search_words = search_words
        self.products = _PRODUCT_WORDS & (tokens(row["title"]) - search_words)
        # A lettered number (USC43, 89CB-13) names one product's card on its own.
        self.lettered = bool(re.search(r"[a-z]", (row["card_number"] or "").lower()))

    def mismatch(self, title: str, words: set[str], other_inserts: list[frozenset]) -> str | None:
        if not self.last <= words or (self.first and not self.first & words):
            return "other player"
        number_matched = bool(self.number and self.number.search(title.lower()))
        products = _PRODUCT_WORDS & (words - self.search_words)
        if products != self.products and not (number_matched and self.lettered):
            return "other product"
        if number_matched:
            pass
        elif self.number and re.search(r"#\s*[a-z0-9]", title.lower()):
            return "other card"
        elif self.team_card:
            return "other card"
        elif self.insert and not self.insert <= words:
            return "other card"
        elif not self.insert and any(s <= words for s in other_inserts):
            return "other card"
        if finish(words - self.search_words) != self.finish:
            return "other parallel"
        return None


def build_plan(listings: pd.DataFrame, comps: pd.DataFrame, title_contains: str):
    """``(plan, comps)``: per-listing price stats, and every comp with how it was read."""
    year = (re.search(r"\b(19|20)\d{2}\b", title_contains) or [""])[0]
    search_words = tokens(title_contains)
    if comps.empty:
        comps = pd.DataFrame(columns=["query", "title", "sale_price", "sold_date", "url"])
    comps = comps.copy().reset_index(drop=True)
    comps["title"] = comps["title"].fillna("")
    comps["sold_date"] = pd.to_datetime(comps["sold_date"], utc=True, errors="coerce")
    comps["excluded"] = comps["title"].map(lambda t: exclusion_reason(t, year))
    comps["finish"] = comps["title"].map(
        lambda t: " ".join(sorted(finish(tokens(t) - search_words)))
    )
    words = comps["title"].map(tokens)
    inserts = [s for s in {insert_tokens(i, search_words) for i in listings["insert"]} if s]

    counted_for: dict[int, list[str]] = {}
    stats = []
    for _, row in listings.iterrows():
        card = _Card(row, search_words)
        pool = comps[(comps["query"] == row["query"]) & comps["excluded"].isna()]
        player_rows, mine_rows = [], []
        for idx, title in pool["title"].items():
            reason = card.mismatch(title, words[idx], [s for s in inserts if s != card.insert])
            if reason != "other player":
                player_rows.append(idx)
            if reason is None:
                mine_rows.append(idx)
                counted_for.setdefault(idx, []).append(f"{row['player']} {row['variant']}")
        mine = comps.loc[mine_rows]
        avg = PriceScraper.price_averages_with_outliers(mine, price_col="sale_price")
        median = avg["trimmed_median"]
        suggested = max(round_up_to_nine(median), _MIN_PRICE) if median else None
        prices = pd.to_numeric(mine["sale_price"], errors="coerce")
        stats.append(
            {
                "n_comps": avg["n_total"],
                "n_outliers": avg["n_outliers"],
                "median": median,
                "mean": avg["trimmed_mean"],
                "low": prices.min() if len(prices) else None,
                "high": prices.max() if len(prices) else None,
                "latest_sale": mine["sold_date"].max() if len(mine) else None,
                "player_median_all_cards": PriceScraper.price_averages_with_outliers(
                    comps.loc[player_rows], price_col="sale_price"
                )["trimmed_median"],
                "suggested_price": suggested,
                "new_price": suggested,
            }
        )
    plan = pd.concat([listings, pd.DataFrame(stats)], axis=1)
    plan["latest_sale"] = pd.to_datetime(plan["latest_sale"], utc=True)
    plan = plan.sort_values(["player", "variant"]).reset_index(drop=True)

    comps["counted_for"] = [" | ".join(counted_for.get(i, [])) or None for i in comps.index]
    comps = comps.sort_values(["query", "counted_for", "sold_date"], ascending=[True, True, False])
    keep = [
        "query", "counted_for", "excluded", "finish", "title", "sale_price", "type",
        "sold_date", "url",
    ]  # fmt: skip
    return plan, comps[[c for c in keep if c in comps.columns]]


def _excel_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Excel can't store tz-aware datetimes."""
    out = df.copy()
    for col in out.columns:
        if isinstance(out[col].dtype, pd.DatetimeTZDtype):
            out[col] = out[col].dt.tz_localize(None)
    return out


def write_plan(plan: pd.DataFrame, comps: pd.DataFrame) -> Path:
    exports = Path(get_settings().paths.exports_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = exports / f"xlsx/price_scheduled_listings_{stamp}.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for path in (out_path, exports / WORKING_PLAN):
        with pd.ExcelWriter(path) as writer:
            _excel_safe(plan).to_excel(writer, sheet_name="plan", index=False)
            _excel_safe(comps).to_excel(writer, sheet_name="comps", index=False)
    logger.info("Written to %s", out_path)
    logger.info("Working plan refreshed at %s (apply with --from-plan)", exports / WORKING_PLAN)
    return out_path


def apply_plan(plan_path: Path, *, apply: bool, limit: int | None) -> pd.DataFrame:
    """Push each row's ``new_price``; without ``apply``, only report what would change."""
    plan = pd.read_excel(plan_path, sheet_name="plan", dtype={"item_id": str, "sku": str})
    plan["new_price"] = pd.to_numeric(plan["new_price"], errors="coerce")
    todo = plan[plan["new_price"].notna() & (plan["new_price"] != plan["current_price"])]
    if limit:
        todo = todo.head(limit)
    logger.info("%d listing(s) to reprice from %s", len(todo), plan_path)

    ebay_api = EbayClient() if apply else None
    results = []
    for _, row in todo.iterrows():
        change = f"{row['title']}: ${row['current_price']:.2f} -> ${row['new_price']:.2f}"
        if not apply:
            logger.info("[preview] %s", change)
            results.append({"item_id": row["item_id"], "status": "preview"})
            continue
        try:
            offer = ebay_api.inventory.find_offer(row["sku"])
            if offer is None:
                raise ValueError(f"no offer for sku={row['sku']}")
            body = rebuild_offer_body(
                offer.raw, float(row["new_price"]), offer.listing_start_date or row["start_time"]
            )
            try:
                ebay_api.inventory.update_offer(offer.offer_id, body)
            except Exception as e:
                # eBay answers some of these PUTs with a transient 99500; a resend goes through.
                logger.info("Retrying %s after: %s", row["sku"], str(e).splitlines()[0])
                time.sleep(2)
                ebay_api.inventory.update_offer(offer.offer_id, body)
            logger.info("Repriced %s", change)
            results.append({"item_id": row["item_id"], "status": "updated"})
        except Exception as e:
            logger.warning("Failed %s: %s", change, e)
            results.append({"item_id": row["item_id"], "status": f"failed: {e}"})

    out = pd.DataFrame(results)
    if apply and len(out):
        logger.info("%s", out["status"].str.split(":").str[0].value_counts().to_dict())
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--title-contains", help="Scrape listings whose title has this text")
    parser.add_argument(
        "--title-excludes", action="append", default=[],
        help="Skip listings whose title also has this text (repeatable)",
    )  # fmt: skip
    parser.add_argument(
        "--search-prefix", help="130point search text, in place of --title-contains"
    )
    parser.add_argument("--from-plan", help="Apply a saved plan workbook's new_price column")
    parser.add_argument("--apply", action="store_true", help="Push the prices to eBay")
    parser.add_argument("--limit", type=int, help="Cap how many listings are repriced")
    parser.add_argument("--workers", type=int, default=_DEFAULT_WORKERS)
    args = parser.parse_args()
    if bool(args.title_contains) == bool(args.from_plan):
        parser.error("pass exactly one of --title-contains or --from-plan")

    setup_logging()
    if args.from_plan:
        apply_plan(Path(args.from_plan), apply=args.apply, limit=args.limit)
        return

    listings = load_scheduled(
        EbayClient(), args.title_contains, args.workers, args.title_excludes, args.search_prefix
    )
    if listings.empty:
        return
    comps = scrape(sorted(listings["query"].dropna().unique()))
    plan, comps = build_plan(listings, comps, args.title_contains)
    write_plan(plan, comps)


if __name__ == "__main__":
    main()
