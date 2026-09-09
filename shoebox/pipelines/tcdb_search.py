"""Interactive TCDB advanced-search session.

Opens a real Chrome window on tcdb.com, makes sure you are logged in (once per
session; the profile remembers you afterwards), then loops: you type a card
number, it runs the advanced search with your defaults (e.g. ``name=Bonds``),
prints the matches, and leaves the results page open in the browser so you can
click through and add the card to your collection.

Session commands (also shown by ``help``)::

    <card number>          search with the current defaults
    set field=value ...    change a default, e.g. set year=1993 set_name="Upper Deck"
    set field=             clear a default
    show                   print the current defaults
    open N                 open result N of the last search in the browser
    help                   list commands
    quit / q / exit        end the session

Command parsing (``parse_command``) is pure and unit-tested; the browser is only
touched from ``run_tcdb_search``.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from shoebox.clients.tcdb import TcdbBrowser, TcdbLoginTimeout
from shoebox.models.tcdb import AdvancedSearchQuery, TcdbSearchPage
from shoebox.settings import get_settings

logger = logging.getLogger(__name__)

QUIT_WORDS = frozenset({"q", "quit", "exit"})


@dataclass(frozen=True)
class Command:
    """One parsed line of session input."""

    kind: str  # "search" | "set" | "show" | "open" | "help" | "quit" | "noop"
    value: str = ""
    updates: dict[str, str] = field(default_factory=dict)
    index: int | None = None


def parse_command(line: str) -> Command:
    """Turn a line of user input into a ``Command``.

    Anything that is not a known keyword is treated as a card number to search.
    Raises ``ValueError`` for a malformed ``set``/``open``.
    """
    text = (line or "").strip()
    if not text:
        return Command("noop")
    lowered = text.casefold()
    if lowered in QUIT_WORDS:
        return Command("quit")
    if lowered in ("help", "?"):
        return Command("help")
    if lowered == "show":
        return Command("show")

    head, _, rest = text.partition(" ")
    if head.casefold() == "set":
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
        return Command("set", updates=updates)
    if head.casefold() == "open":
        try:
            return Command("open", index=int(rest.strip()))
        except ValueError as e:
            raise ValueError("Usage: open N   (N = result number from the last search)") from e

    return Command("search", value=text)


HELP_TEXT = """\
Commands:
  <card number>            search with the current defaults
  set field=value [...]    change defaults, e.g.  set year=1993 set_name="Upper Deck"
  set field=               clear a default
  show                     print the current defaults
  open N                   open result N from the last search in the browser
  help                     this text
  quit                     end the session
Fields: {fields}
"""


def results_table(page: TcdbSearchPage, query: AdvancedSearchQuery, *, max_rows: int = 0) -> Table:
    """Render a results page as a rich table (``max_rows`` 0 = all)."""
    shown = page.results if not max_rows else page.results[:max_rows]
    title = f"TCDB: {query.describe()} — {page.total_results} result(s)"
    if len(shown) < len(page.results):
        title += f" (showing {len(shown)})"
    elif page.truncated:
        title += f" (page shows {len(page.results)})"
    table = Table(title=title, show_lines=False, expand=True)
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("Card", overflow="fold")
    table.add_column("Note", overflow="fold")
    table.add_column("URL", overflow="fold", style="dim")
    for r in shown:
        table.add_row(str(r.index), r.title, r.note or "", r.url)
    return table


def build_defaults(
    overrides: dict[str, str] | None = None, *, settings_defaults: dict[str, str] | None = None
) -> AdvancedSearchQuery:
    """Merge config ``tcdb.search_defaults`` with command-line overrides."""
    merged = {**(settings_defaults or {}), **{k: v for k, v in (overrides or {}).items() if v}}
    return AdvancedSearchQuery().with_updates(**merged)


def run_tcdb_search(
    *,
    overrides: dict[str, str] | None = None,
    card_numbers: Iterable[str] | None = None,
    login: bool = True,
    max_rows: int = 0,
    headless: bool = False,
    profile_dir: str | Path | None = None,
    input_fn: Callable[[str], str] = input,
    console: Console | None = None,
) -> int:
    """Run the interactive session. Returns a process exit code.

    ``card_numbers`` given up front are searched first (non-interactively); the
    interactive prompt follows unless stdin is exhausted.
    """
    console = console or Console()
    settings = get_settings()
    tcdb_cfg = settings.tcdb

    try:
        query = build_defaults(overrides, settings_defaults=tcdb_cfg.search_defaults)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return 2

    last_page: TcdbSearchPage | None = None
    fields = ", ".join(AdvancedSearchQuery.field_names())

    def do_search(card_number: str) -> None:
        nonlocal last_page, query
        q = query.with_updates(card_number=card_number)
        try:
            last_page = browser.advanced_search(q)
        except Exception as e:  # browser hiccup: report and keep the session alive
            logger.exception("TCDB search failed")
            console.print(f"[red]Search failed: {e}[/red]")
            return
        if not last_page.results:
            console.print(f"[yellow]No results for {q.describe()}[/yellow]")
            return
        console.print(results_table(last_page, q, max_rows=max_rows))
        console.print("[dim]Results are open in the browser; add to your collection there.[/dim]")

    with TcdbBrowser(profile_dir or tcdb_cfg.profile_dir, headless=headless) as browser:
        if login:
            try:
                browser.ensure_logged_in(
                    timeout_s=tcdb_cfg.login_timeout_s,
                    prompt=lambda msg: console.print(f"[bold cyan]{msg}[/bold cyan]"),
                )
            except TcdbLoginTimeout as e:
                console.print(f"[red]{e}[/red]")
                return 1

        console.print(f"Defaults: {query.describe()}")
        for number in card_numbers or ():
            do_search(str(number).strip())

        while True:
            try:
                line = input_fn("card #> ")
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            try:
                cmd = parse_command(line)
            except ValueError as e:
                console.print(f"[red]{e}[/red]")
                continue

            if cmd.kind == "noop":
                continue
            if cmd.kind == "quit":
                break
            if cmd.kind == "help":
                console.print(HELP_TEXT.format(fields=fields))
            elif cmd.kind == "show":
                console.print(f"Defaults: {query.describe()}")
            elif cmd.kind == "set":
                try:
                    query = query.with_updates(**cmd.updates)
                except ValueError as e:
                    console.print(f"[red]{e}[/red]")
                    continue
                console.print(f"Defaults: {query.describe()}")
            elif cmd.kind == "open":
                if last_page is None:
                    console.print("[yellow]Run a search first.[/yellow]")
                    continue
                match = next((r for r in last_page.results if r.index == cmd.index), None)
                if match is None:
                    console.print(f"[yellow]No result #{cmd.index} in the last search.[/yellow]")
                    continue
                browser.open(match.url)
                console.print(f"Opened {match.title}")
            elif cmd.kind == "search":
                do_search(cmd.value)

    return 0
