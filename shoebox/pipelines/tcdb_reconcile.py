"""Reconcile eBay sales against the TCDB collection, one card at a time.

Every sale is already in BigQuery (``sync-orders`` runs nightly). This pulls
the non-variation line items created since the stored *reconciled-through*
date, searches TCDB for each card (year + card number + first name, the same
search ``tcdb-add`` uses), shows the candidates with the best match starred,
and asks what to do::

    [Enter]            remove the starred match from the collection
    N                  remove result N instead
    open N             open result N in the browser
    set field=value    change the search (year, card_number, name, category ...) and search again
    skip               leave it for next time
    ignore             never ask again (not tracked on TCDB)
    quit               stop; everything not yet decided comes back next run

Variation ("You Pick") listings are never shown: those cards are not tracked
on TCDB. A card sold singly is removed as soon as you pick it. A card sold
with quantity 2 or more first shows how many copies TCDB says you have and
asks how many should remain; the rest are removed.

State lives under ``<data_dir>/tcdb_reconcile/``: ``state.json`` holds the
reconciled-through date, ``ledger.jsonl`` one line per decision. Only
``removed`` and ``ignored`` are final; a skipped, failed or undecided sale
holds the date back so it is offered again. The first run needs ``--since``.

Everything but ``run_tcdb_reconcile`` is pure and unit-tested; the browser and
BigQuery are only touched from there.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from shoebox.clients.tcdb import TcdbBrowser, TcdbLoginTimeout
from shoebox.clients.tcdb.card import normalize_title
from shoebox.models.common import utc_now
from shoebox.models.tcdb import (
    AdvancedSearchQuery,
    ReconcileEntry,
    ReconcileState,
    SoldCard,
    TcdbSearchPage,
    TcdbSearchResult,
)
from shoebox.settings import get_settings
from shoebox.utils.jsonl import append_jsonl, read_jsonl

logger = logging.getLogger(__name__)

STATE_DIRNAME = "tcdb_reconcile"
STATE_FILENAME = "state.json"
LEDGER_FILENAME = "ledger.jsonl"
SOLD_CARDS_SQL = "tcdb_sold_cards.sql"

QUIT_WORDS = frozenset({"q", "quit", "exit"})

# A candidate is starred only when it beats every other result and shares at
# least this fraction of its words with the sold card (set, subset, parallel, player).
SUGGEST_MIN_SCORE = 0.5

# Sport words in a checklist set name -> TCDB category. Anything else is the default.
_CATEGORY_WORDS: tuple[tuple[str, str], ...] = (
    ("basketball", "Basketball"),
    ("football", "Football"),
    ("hockey", "Hockey"),
    ("soccer", "Soccer"),
    ("baseball", "Baseball"),
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CARD_NUMBER_RE = re.compile(r"#\S+")
# An eBay listing title, loosely: "<year> <set> - <player> #<number> <team> (RC)".
_EBAY_TITLE_RE = re.compile(r"^(?P<year>\d{4}(?:-\d{2})?)\b(?P<before>[^#]*)#(?P<number>\S+)")
_NAME_SUFFIXES = frozenset({"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"})
# Words that say nothing about which card it is.
_NOISE_TOKENS = frozenset({"base"})


# --------------------------------------------------------------------------- state


def load_state(state_dir: Path) -> ReconcileState:
    path = state_dir / STATE_FILENAME
    if not path.exists():
        return ReconcileState()
    return ReconcileState.model_validate(json.loads(path.read_text(encoding="utf-8")))


def save_state(state_dir: Path, state: ReconcileState) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    state.updated_at = utc_now()
    (state_dir / STATE_FILENAME).write_text(
        json.dumps(state.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )


def load_ledger(state_dir: Path) -> list[ReconcileEntry]:
    return [ReconcileEntry.model_validate(row) for row in read_jsonl(state_dir / LEDGER_FILENAME)]


def append_ledger(state_dir: Path, entry: ReconcileEntry) -> None:
    append_jsonl(state_dir / LEDGER_FILENAME, entry.model_dump(mode="json"))


def final_keys(ledger: Iterable[ReconcileEntry]) -> set[str]:
    """Line items with a final decision (removed or ignored), by order_line_key."""
    return {e.order_line_key for e in ledger if e.final}


def pending_cards(cards: Iterable[SoldCard], ledger: Iterable[ReconcileEntry]) -> list[SoldCard]:
    """The sold cards still waiting for a decision, in the order they were sold."""
    done = final_keys(ledger)
    return [c for c in cards if c.order_line_key not in done]


def next_reconciled_through(still_pending: Iterable[SoldCard], today: date) -> date:
    """Everything up to this date is settled: the day before the oldest sale
    still waiting, or today when nothing is."""
    dates = [c.order_created_date for c in still_pending]
    if not dates:
        return today
    return min(dates) - timedelta(days=1)


def since_for_run(since: str | date | None, state: ReconcileState) -> date | None:
    """The first sale date to pull: ``--since`` when given, else the day after
    the stored reconciled-through date, else None (first run, nothing stored)."""
    if isinstance(since, date):
        return since
    if since:
        return date.fromisoformat(since)
    if state.reconciled_through:
        return state.reconciled_through + timedelta(days=1)
    return None


# --------------------------------------------------------------------------- rows


def _clean(value: Any) -> Any:
    """Turn what pandas hands back (NaN, numpy scalars, Timestamps) into plain Python."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, TypeError):
            pass
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "date") and callable(value.date) and not isinstance(value, date):
        return value.date()
    return value


def _text(value: Any) -> str:
    value = _clean(value)
    return "" if value is None else str(value)


def sold_cards_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[SoldCard]:
    """``SoldCard`` per query row (``tcdb_sold_cards.sql``), tolerant of pandas NaNs."""
    cards: list[SoldCard] = []
    for row in rows:
        quantity = _clean(row.get("quantity"))
        print_run = _clean(row.get("print_run"))
        cards.append(
            SoldCard(
                order_line_key=_text(row.get("order_line_key")),
                order_id=_text(row.get("order_id")),
                line_item_id=_text(row.get("line_item_id")),
                order_created_date=_clean(row.get("order_created_date")),
                ebay_title=_text(row.get("ebay_title")),
                sku=_text(row.get("sku")),
                quantity=int(quantity) if quantity else 1,
                legacy_item_id=_text(row.get("legacy_item_id")),
                matched=bool(_clean(row.get("is_matched_to_meta"))),
                set_year=_text(row.get("set_year")),
                set_name=_text(row.get("set_name")),
                subset_name=_text(row.get("subset_name")),
                subset_type=_text(row.get("subset_type")),
                parallel_variety=_text(row.get("parallel_variety")),
                card_number=_text(row.get("card_number")),
                player=_text(row.get("player")),
                team=_text(row.get("team")),
                print_run=int(print_run) if print_run else None,
            )
        )
    return cards


def fetch_sold_cards(bq, since: date) -> list[SoldCard]:
    """Run ``tcdb_sold_cards.sql`` for sales on/after ``since``."""
    df = bq.run_query(sql=SOLD_CARDS_SQL, params={"since": since.isoformat()})
    rows = df.to_dict("records") if df is not None else []
    return sold_cards_from_rows(rows)


# --------------------------------------------------------------------------- search


def guess_category(set_name: str, default: str = "Baseball") -> str:
    """TCDB category from the sport named in a checklist set name, else ``default``."""
    lowered = (set_name or "").casefold()
    for word, category in _CATEGORY_WORDS:
        if word in lowered:
            return category
    return default


def parse_ebay_title(title: str) -> tuple[str, str, str]:
    """Year, card number and the player's last name from a listing title such as
    ``2024 Topps Heritage - Jackson Chourio #632 Brewers (RC)``: the number is
    what follows ``#``, the last name the word before it (skipping Jr./II).
    Empty strings for whatever cannot be read."""
    m = _EBAY_TITLE_RE.match(" ".join((title or "").split()))
    if not m:
        return "", "", ""
    words = [w for w in m.group("before").replace(" - ", " ").split() if w != "-"]
    while words and words[-1].casefold() in _NAME_SUFFIXES:
        words.pop()
    last_name = words[-1].strip(",") if words else ""
    return m.group("year"), m.group("number"), last_name


def search_query_for(card: SoldCard, category: str) -> AdvancedSearchQuery:
    """Year + card number + first name, like ``tcdb-add`` (the set is left out:
    TCDB reads a leading ``-`` as an exclusion and set names rarely agree).
    A card whose SKU matched no checklist row is searched from its eBay title
    instead (year, number, last name), as a starting point to refine."""
    if not card.matched:
        year, number, last_name = parse_ebay_title(card.ebay_title)
        return AdvancedSearchQuery(category=category, year=year, card_number=number, name=last_name)
    return AdvancedSearchQuery(
        category=category,
        year=card.set_year,
        card_number=card.card_number,
        name=card.search_name,
    )


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(normalize_title(text)) if t not in _NOISE_TOKENS}


def card_tokens(card: SoldCard) -> set[str]:
    """Words that identify the sold card: set, subset, parallel and player
    (the eBay title, minus its ``#number``, when the SKU matched nothing)."""
    if not card.matched:
        return _tokens(_CARD_NUMBER_RE.sub(" ", card.ebay_title))
    return _tokens(" ".join([card.set_name, card.subset_name, card.parallel_variety, card.player]))


def result_tokens(result: TcdbSearchResult) -> set[str]:
    """Words of a TCDB title, minus the ``#number`` (the search already fixed it)."""
    return _tokens(_CARD_NUMBER_RE.sub(" ", result.title))


@dataclass(frozen=True)
class Candidate:
    result: TcdbSearchResult
    score: float  # Jaccard overlap of words with the sold card, 0..1


def rank_candidates(card: SoldCard, results: Iterable[TcdbSearchResult]) -> list[Candidate]:
    """Results scored by how many words they share with the sold card, best first.
    A base card and its parallels all contain the base words, so the extra
    parallel words are what separate them."""
    wanted = card_tokens(card)
    ranked = []
    for r in results:
        have = result_tokens(r)
        union = wanted | have
        score = len(wanted & have) / len(union) if union else 0.0
        ranked.append(Candidate(result=r, score=score))
    ranked.sort(key=lambda c: (-c.score, c.result.index))
    return ranked


def suggest(ranked: list[Candidate]) -> TcdbSearchResult | None:
    """The one result to star: a clear best (strictly ahead of the runner-up)
    scoring at least ``SUGGEST_MIN_SCORE``. None when it is anyone's guess."""
    if not ranked or ranked[0].score < SUGGEST_MIN_SCORE:
        return None
    if len(ranked) > 1 and ranked[1].score >= ranked[0].score:
        return None
    return ranked[0].result


# --------------------------------------------------------------------------- commands


@dataclass(frozen=True)
class ReconcileCommand:
    """One parsed line of session input."""

    # accept | remove | open | set | skip | ignore | help | quit
    kind: str
    index: int | None = None
    updates: dict[str, str] = field(default_factory=dict)


def parse_reconcile_command(line: str) -> ReconcileCommand:
    """Turn a line of input into a ``ReconcileCommand``. Raises ``ValueError``
    for anything it cannot read."""
    text = (line or "").strip()
    if not text:
        return ReconcileCommand("accept")
    lowered = text.casefold()
    if lowered in QUIT_WORDS:
        return ReconcileCommand("quit")
    if lowered in ("help", "?"):
        return ReconcileCommand("help")
    if lowered in ("s", "skip"):
        return ReconcileCommand("skip")
    if lowered in ("i", "ignore"):
        return ReconcileCommand("ignore")
    if text.isdigit():
        return ReconcileCommand("remove", index=int(text))

    head, _, rest = text.partition(" ")
    head = head.casefold()
    if head in ("open", "o", "remove", "rm", "r"):
        kind = "open" if head in ("open", "o") else "remove"
        try:
            return ReconcileCommand(kind, index=int(rest.strip()))
        except ValueError as e:
            raise ValueError(f"Usage: {kind} N   (N = result number from the search)") from e
    if head == "set":
        try:
            tokens = shlex.split(rest)
        except ValueError as e:
            raise ValueError(f"Could not parse: {e}") from e
        if not tokens:
            raise ValueError("Usage: set field=value [field=value ...]")
        updates: dict[str, str] = {}
        for tok in tokens:
            key, sep, val = tok.partition("=")
            if not sep or not key:
                raise ValueError(f"Expected field=value, got {tok!r}")
            updates[key.strip().casefold().replace("-", "_")] = val.strip()
        return ReconcileCommand("set", updates=updates)
    raise ValueError(f"Unknown command {text!r}. Type help for the list.")


HELP_TEXT = """\
Commands:
  [Enter]                  remove the starred match from your collection
  N                        remove result N instead
                           (quantity 2+: you are then asked how many copies should remain)
  open N                   open result N in the browser
  set field=value [...]    change the search and run it again, e.g.  set name=Bonds year=1993
  skip                     leave this sale for next time
  ignore                   never ask about this sale again (not tracked on TCDB)
  help                     this text
  quit                     stop; undecided sales come back next run
Fields: {fields}
"""


def parse_count(line: str, default: int) -> int | str:
    """Answer to a "how many" prompt: a whole number, ``default`` on Enter,
    or the words ``skip`` / ``quit``. Raises ``ValueError`` otherwise."""
    text = (line or "").strip().casefold()
    if not text:
        return default
    if text in QUIT_WORDS:
        return "quit"
    if text in ("s", "skip"):
        return "skip"
    if text.isdigit():
        return int(text)
    raise ValueError(f"Enter a number, skip or quit (got {line.strip()!r}).")


# --------------------------------------------------------------------------- display


def pending_table(cards: list[SoldCard], title: str = "Sales to reconcile") -> Table:
    table = Table(title=title, expand=True)
    table.add_column("Sold", no_wrap=True)
    table.add_column("Qty", justify="right", no_wrap=True)
    table.add_column("Card", overflow="fold")
    table.add_column("eBay title", overflow="fold", style="dim")
    for c in cards:
        card = c.description if c.matched else "[yellow](no checklist match)[/yellow]"
        table.add_row(c.order_created_date.isoformat(), str(c.quantity), card, c.ebay_title)
    return table


def candidates_table(
    page: TcdbSearchPage,
    query: AdvancedSearchQuery,
    ranked: list[Candidate],
    suggestion: TcdbSearchResult | None,
) -> Table:
    title = f"TCDB: {query.describe()} — {page.total_results} result(s)"
    if page.truncated:
        title += f" (page shows {len(page.results)})"
    table = Table(title=title, show_lines=False, expand=True)
    table.add_column("", no_wrap=True)
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("Card", overflow="fold")
    table.add_column("Note", overflow="fold")
    table.add_column("Match", justify="right", no_wrap=True)
    for c in sorted(ranked, key=lambda c: c.result.index):
        starred = suggestion is not None and c.result.index == suggestion.index
        style = "bold green" if starred else ""
        table.add_row(
            "★" if starred else "",
            str(c.result.index),
            f"[{style}]{c.result.title}[/{style}]" if style else c.result.title,
            c.result.note or "",
            f"{c.score:.0%}",
        )
    return table


def summary_table(entries: list[ReconcileEntry]) -> Table:
    table = Table(title="TCDB reconcile", expand=True)
    table.add_column("Sold", no_wrap=True)
    table.add_column("Card", overflow="fold")
    table.add_column("Result", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    styles = {"removed": "green", "ignored": "cyan", "skipped": "yellow"}
    for e in entries:
        style = styles.get(e.status, "red")
        detail = e.tcdb_title or e.detail
        if e.tcdb_title and e.detail:
            detail = f"{e.tcdb_title} ({e.detail})"
        table.add_row(
            e.order_created_date.isoformat(), e.card, f"[{style}]{e.status}[/{style}]", detail
        )
    return table


# --------------------------------------------------------------------------- run


def _entry(card: SoldCard, status: str, **kw: Any) -> ReconcileEntry:
    return ReconcileEntry(
        order_line_key=card.order_line_key,
        order_id=card.order_id,
        line_item_id=card.line_item_id,
        order_created_date=card.order_created_date,
        status=status,
        card=card.description,
        quantity=card.quantity,
        **kw,
    )


def reconcile_one(
    browser,
    card: SoldCard,
    *,
    category: str,
    position: str = "",
    input_fn: Callable[[str], str] = input,
    console: Console,
) -> ReconcileEntry | None:
    """Search TCDB for one sold card and act on the user's decision.
    Returns the ledger entry, or None when the user quits."""
    console.rule(
        f"[bold]{position} Sold {card.order_created_date} · qty {card.quantity} · order {card.order_id}"
    )
    if card.matched:
        console.print(f"  [bold]{card.description}[/bold]")
        console.print(f"  [dim]eBay: {card.ebay_title}[/dim]")
    else:
        console.print(f"  [bold]{card.ebay_title}[/bold]")
        console.print(
            "  [yellow]The SKU matched no checklist card; searching from the eBay title. "
            "Refine with  set year=... card_number=... name=...  if needed.[/yellow]"
        )

    query = search_query_for(card, guess_category(card.set_name, category))
    page: TcdbSearchPage | None = None
    suggestion: TcdbSearchResult | None = None
    fields = ", ".join(AdvancedSearchQuery.field_names())

    def do_search() -> None:
        nonlocal page, suggestion
        page, suggestion = None, None
        if not (query.card_number or query.name):
            return
        try:
            found = browser.advanced_search(query)
        except Exception as e:  # browser hiccup: report and keep the session alive
            logger.exception("TCDB search failed")
            console.print(f"[red]Search failed: {e}[/red]")
            return
        page = found
        if not page.results:
            console.print(f"[yellow]No results for {query.describe()}[/yellow]")
            return
        ranked = rank_candidates(card, page.results)
        suggestion = suggest(ranked)
        console.print(candidates_table(page, query, ranked, suggestion))
        hint = f"[Enter] remove #{suggestion.index}" if suggestion else "no clear match: pick N"
        console.print(
            f"[dim]{hint} · N remove result N · open N · set field=value · skip · ignore · quit[/dim]"
        )

    do_search()
    while True:
        try:
            line = input_fn("reconcile> ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return None
        try:
            cmd = parse_reconcile_command(line)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            continue

        if cmd.kind == "quit":
            return None
        if cmd.kind == "help":
            console.print(HELP_TEXT.format(fields=fields))
            continue
        if cmd.kind == "skip":
            return _entry(card, "skipped")
        if cmd.kind == "ignore":
            return _entry(card, "ignored", detail="not tracked on TCDB")
        if cmd.kind == "set":
            try:
                query = query.with_updates(**cmd.updates)
            except ValueError as e:
                console.print(f"[red]{e}[/red]")
                continue
            do_search()
            continue

        # accept / remove / open all need a result to point at
        if cmd.kind == "accept":
            if suggestion is None:
                console.print(
                    "[yellow]No starred match. Type the result number, skip or ignore.[/yellow]"
                )
                continue
            target = suggestion
        else:
            if page is None:
                console.print(
                    "[yellow]No search results yet. Use set field=value to search.[/yellow]"
                )
                continue
            target = next((r for r in page.results if r.index == cmd.index), None)
            if target is None:
                console.print(f"[yellow]No result #{cmd.index} in the search.[/yellow]")
                continue
        if cmd.kind == "open":
            browser.open(target.url)
            console.print(f"Opened {target.title}")
            continue

        copies = card.quantity
        if card.quantity > 1:
            decided = _copies_to_remove(browser, card, target, input_fn=input_fn, console=console)
            if decided is None:
                return None
            if isinstance(decided, ReconcileEntry):
                return decided
            copies = decided

        try:
            res = browser.remove_from_collection(target.url, copies=copies)
        except Exception as e:  # browser hiccup on one card: report, come back to it next run
            logger.exception("TCDB remove failed for %s", target.url)
            return _entry(
                card, "failed", tcdb_title=target.title, tcdb_url=target.url, detail=str(e)
            )
        status = "removed" if res.copies_removed else "failed"
        detail = res.detail
        if res.status == "not_owned" and not detail:
            detail = "not in the collection"
        style = "green" if res.ok else "yellow" if res.copies_removed else "red"
        copies_note = f" ({res.copies_removed}/{res.copies_requested} copies)" if copies > 1 else ""
        console.print(
            f"[{style}]{status}[/{style}]: {res.card.title or target.title}{copies_note}"
            + (f" — {detail}" if detail else "")
        )
        return _entry(
            card,
            status,
            tcdb_title=res.card.title or target.title,
            tcdb_url=target.url,
            copies_removed=res.copies_removed,
            detail=detail,
        )


def _copies_to_remove(
    browser,
    card: SoldCard,
    target: TcdbSearchResult,
    *,
    input_fn: Callable[[str], str],
    console: Console,
) -> int | ReconcileEntry | None:
    """For a sale of 2+ copies: read how many copies TCDB says you have and ask
    how many should remain. Returns the number to remove, a ledger entry when
    the answer settles the sale without removing (skip, keep them all, or the
    card is not owned), or None to quit."""
    try:
        info = browser.inspect_collection(target.url)
    except Exception as e:  # browser hiccup: come back to it next run
        logger.exception("TCDB inspect failed for %s", target.url)
        return _entry(card, "failed", tcdb_title=target.title, tcdb_url=target.url, detail=str(e))
    title = info.card.title or target.title
    if not info.widget.loaded:
        return _entry(
            card, "failed", tcdb_title=title, tcdb_url=target.url,
            detail="collection box did not load",
        )  # fmt: skip
    if not info.widget.owned:
        console.print(f"[red]not in the collection[/red]: {title}")
        return _entry(
            card, "failed", tcdb_title=title, tcdb_url=target.url, detail="not in the collection"
        )

    owned = info.widget.quantity
    if owned is not None:
        default = max(owned - card.quantity, 0)
        console.print(
            f"TCDB shows [bold]{owned}[/bold] cop{'y' if owned == 1 else 'ies'} of {title}; "
            f"{card.quantity} sold."
        )
        prompt = f"copies to keep [{default}]> "
    else:
        default = card.quantity
        console.print(
            f"[yellow]TCDB does not say how many copies of {title} you have.[/yellow] "
            f"{card.quantity} sold."
        )
        prompt = f"copies to remove [{default}]> "

    while True:
        try:
            line = input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            console.print()
            return None
        try:
            answer = parse_count(line, default)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            continue
        if answer == "quit":
            return None
        if answer == "skip":
            return _entry(card, "skipped", tcdb_title=title, tcdb_url=target.url)
        if owned is None:
            copies = int(answer)
        else:
            if answer > owned:
                console.print(f"[red]You only have {owned}; enter 0..{owned}.[/red]")
                continue
            copies = owned - int(answer)
        if copies == 0:
            console.print(f"[cyan]kept[/cyan]: {title} (nothing removed)")
            return _entry(
                card, "ignored", tcdb_title=title, tcdb_url=target.url,
                detail=f"kept all {owned} copies" if owned is not None else "nothing removed",
            )  # fmt: skip
        return copies


def run_tcdb_reconcile(
    *,
    since: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    category: str | None = None,
    headless: bool = False,
    profile_dir: str | Path | None = None,
    input_fn: Callable[[str], str] = input,
    console: Console | None = None,
    bq=None,
    today: date | None = None,
) -> int:
    """Reconcile sales since the stored date (or ``since``) against TCDB.
    Returns a process exit code."""
    console = console or Console()
    settings = get_settings()
    tcdb_cfg = settings.tcdb
    default_category = category or tcdb_cfg.search_defaults.get("category") or "Baseball"
    try:
        AdvancedSearchQuery(category=default_category)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return 2

    state_dir = Path(settings.paths.data_dir) / STATE_DIRNAME
    state = load_state(state_dir)
    ledger = load_ledger(state_dir)
    today = today or date.today()

    try:
        since_date = since_for_run(since, state)
    except ValueError:
        console.print(f"[red]--since must be a date like 2026-10-01, got {since!r}[/red]")
        return 2
    if since_date is None:
        console.print(
            "[yellow]Nothing reconciled yet, so there is no stored date. "
            "Pass --since YYYY-MM-DD for the first run.[/yellow]"
        )
        return 2

    if bq is None:
        from shoebox.clients.bigquery import BigQueryClient

        bq = BigQueryClient()
    cards = fetch_sold_cards(bq, since_date)
    pending = pending_cards(cards, ledger)
    console.print(
        f"{len(cards)} sale(s) since {since_date} (variations left out); "
        f"{len(pending)} to reconcile, {len(cards) - len(pending)} already decided."
    )
    if state.reconciled_through:
        console.print(f"[dim]Reconciled through {state.reconciled_through}.[/dim]")

    if dry_run or not pending:
        if pending:
            console.print(pending_table(pending))
        if not pending and not dry_run:
            state.reconciled_through = next_reconciled_through([], today)
            save_state(state_dir, state)
            console.print(f"Nothing to do. Reconciled through {state.reconciled_through}.")
        return 0

    todo = pending[:limit] if limit else pending
    decided: dict[str, ReconcileEntry] = {}

    with TcdbBrowser(profile_dir or tcdb_cfg.profile_dir, headless=headless) as browser:
        try:
            browser.ensure_logged_in(
                timeout_s=tcdb_cfg.login_timeout_s,
                prompt=lambda msg: console.print(f"[bold cyan]{msg}[/bold cyan]"),
            )
        except TcdbLoginTimeout as e:
            console.print(f"[red]{e}[/red]")
            return 1

        for i, card in enumerate(todo, 1):
            entry = reconcile_one(
                browser,
                card,
                category=default_category,
                position=f"[{i}/{len(todo)}]",
                input_fn=input_fn,
                console=console,
            )
            if entry is None:
                console.print("[dim]Stopping; undecided sales come back next run.[/dim]")
                break
            append_ledger(state_dir, entry)
            decided[card.order_line_key] = entry

    still_pending = [
        c for c in pending if c.order_line_key not in decided or not decided[c.order_line_key].final
    ]
    state.reconciled_through = next_reconciled_through(still_pending, today)
    save_state(state_dir, state)

    if decided:
        console.print(summary_table(list(decided.values())))
    console.print(
        f"Reconciled through [bold]{state.reconciled_through}[/bold]; "
        f"{len(still_pending)} sale(s) still open. Ledger: {state_dir / LEDGER_FILENAME}"
    )
    return 0 if not any(e.status == "failed" for e in decided.values()) else 1
