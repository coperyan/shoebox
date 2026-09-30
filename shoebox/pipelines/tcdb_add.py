"""Add cards to your TCDB collection from a list of card titles.

Each input line is a card written the way TCDB titles it, e.g.::

    2009 Bowman Chrome - X-Fractors #171 Matt Cain

or a ViewCard.cfm link. For a title, the card is found with an advanced search
on year + card number + name (the set name is left out of the search because
TCDB reads a leading "-" as an exclusion) and the result whose title matches
exactly is opened. The card is then added with the Quick Add button on its
page. Zero or several exact matches are reported and skipped, never guessed.

Cards already in the collection are skipped unless ``allow_duplicates``.
``dry_run`` finds every card without adding anything.

Line reading, spec parsing and title matching are pure and unit-tested; the
browser is only touched from ``run_tcdb_add``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from shoebox.clients.tcdb import (
    TcdbBrowser,
    TcdbLoginTimeout,
    is_card_url,
    match_card,
    parse_card_spec,
)
from shoebox.models.tcdb import AdvancedSearchQuery, CardSpec, TcdbSearchResult
from shoebox.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class CardOutcome:
    """One input line and what happened to it (one row of the summary table)."""

    line: str
    status: str  # added | already_owned | found | not_found | ambiguous | invalid | failed | ...
    title: str = ""
    url: str = ""
    detail: str = ""
    candidates: list[TcdbSearchResult] = field(default_factory=list)


def read_card_lines(cards: Iterable[str] = (), file: str | Path | None = None) -> list[str]:
    """Card lines from the command line and/or a text file.

    Blank lines and ``#`` comments in the file are skipped (a card's ``#171``
    never starts a line, so this is unambiguous).
    """
    lines = [c.strip() for c in cards if c and c.strip()]
    if file:
        for raw in Path(file).read_text(encoding="utf-8").splitlines():
            text = raw.strip()
            if text and not text.startswith("#"):
                lines.append(text)
    return lines


def search_query_for(spec: CardSpec, category: str) -> AdvancedSearchQuery:
    """The advanced search that finds a spec: year + card number + (first) name."""
    return AdvancedSearchQuery(
        category=category,
        year=spec.year,
        card_number=spec.card_number,
        name=spec.search_name,
    )


def outcomes_table(outcomes: list[CardOutcome]) -> Table:
    table = Table(title="TCDB add", expand=True)
    table.add_column("Card", overflow="fold")
    table.add_column("Result", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    styles = {"added": "green", "found": "cyan", "already_owned": "yellow"}
    for o in outcomes:
        style = styles.get(o.status, "red")
        table.add_row(o.title or o.line, f"[{style}]{o.status}[/{style}]", o.detail)
    return table


def run_tcdb_add(
    *,
    cards: Iterable[str] = (),
    file: str | Path | None = None,
    category: str | None = None,
    dry_run: bool = False,
    allow_duplicates: bool = False,
    headless: bool = False,
    profile_dir: str | Path | None = None,
    console: Console | None = None,
) -> int:
    """Add each card to the TCDB collection. Returns a process exit code
    (0 when every card was added, already owned, or found in a dry run)."""
    console = console or Console()
    settings = get_settings()
    tcdb_cfg = settings.tcdb
    category = category or tcdb_cfg.search_defaults.get("category") or "Baseball"

    try:
        lines = read_card_lines(cards, file)
    except OSError as e:
        console.print(f"[red]Could not read {file}: {e}[/red]")
        return 2
    if not lines:
        console.print("[yellow]No cards given. Pass card titles or --file.[/yellow]")
        return 2

    # Validate everything before opening a browser.
    try:
        AdvancedSearchQuery(category=category)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return 2
    outcomes: list[CardOutcome] = []
    specs: dict[int, CardSpec] = {}
    for i, line in enumerate(lines):
        if is_card_url(line):
            continue
        try:
            specs[i] = parse_card_spec(line)
        except ValueError as e:
            outcomes.append(CardOutcome(line=line, status="invalid", detail=str(e)))
    if outcomes:
        console.print(outcomes_table(outcomes))
        return 2

    debug_dir = Path(settings.paths.data_dir) / "tcdb_debug"

    with TcdbBrowser(profile_dir or tcdb_cfg.profile_dir, headless=headless) as browser:
        try:
            browser.ensure_logged_in(
                timeout_s=tcdb_cfg.login_timeout_s,
                prompt=lambda msg: console.print(f"[bold cyan]{msg}[/bold cyan]"),
            )
        except TcdbLoginTimeout as e:
            console.print(f"[red]{e}[/red]")
            return 1

        for i, line in enumerate(lines):
            outcome = CardOutcome(line=line, status="failed")
            outcomes.append(outcome)
            try:
                if i in specs:
                    spec = specs[i]
                    outcome.title = spec.title
                    page = browser.advanced_search(search_query_for(spec, category))
                    matches = match_card(page, spec)
                    if len(matches) != 1:
                        outcome.status = "not_found" if not matches else "ambiguous"
                        outcome.candidates = matches or page.results
                        outcome.detail = _candidates_detail(
                            outcome.candidates, page.truncated, with_urls=bool(matches)
                        )
                        continue
                    outcome.url = matches[0].url
                    outcome.title = matches[0].title
                else:
                    outcome.url = line

                if dry_run:
                    outcome.status = "found"
                    outcome.detail = outcome.url
                    continue

                res = browser.add_to_collection(outcome.url, allow_duplicate=allow_duplicates)
                outcome.status = res.status
                outcome.title = res.card.title or outcome.title
                outcome.detail = res.detail
                if not res.ok and res.status != "already_owned" and res.widget_html:
                    path = _save_debug(debug_dir, res.card.card_id, res.widget_html)
                    outcome.detail += f" (collection box saved to {path})"
            except Exception as e:  # browser hiccup on one card: report and carry on
                logger.exception("TCDB add failed for %r", line)
                outcome.status = "failed"
                outcome.detail = str(e)
            finally:
                console.print(f"{outcome.status:>13}  {outcome.title or line}")

    console.print(outcomes_table(outcomes))
    good = {"added", "already_owned", "found"}
    return 0 if all(o.status in good for o in outcomes) else 1


def _candidates_detail(
    candidates: list[TcdbSearchResult], truncated: bool, *, with_urls: bool = False
) -> str:
    """Summarize what the search did find. ``with_urls`` for same-title matches,
    so one can be picked and passed back as a ViewCard link."""
    if not candidates:
        return "no search results"
    shown = "; ".join(
        f"{c.title}{f' ({c.note})' if c.note else ''}{f' <{c.url}>' if with_urls else ''}"
        for c in candidates[:5]
    )
    more = f" (+{len(candidates) - 5} more)" if len(candidates) > 5 else ""
    tail = " — search results were truncated" if truncated else ""
    return f"TCDB has: {shown}{more}{tail}"


def _save_debug(debug_dir: Path, card_id: int | None, html: str) -> Path:
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"collection_box_{card_id or 'unknown'}.html"
    path.write_text(html, encoding="utf-8")
    return path
