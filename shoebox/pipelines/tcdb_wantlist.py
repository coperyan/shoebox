"""Export your TCDB want list to a file.

The want list is the member's collection filtered to want status
(``ViewCollectionMode.cfm?...&Filter=W``), 100 rows to a page. This walks every
page for one sport (or all of them) and writes the rows to CSV or JSONL.

Nothing is changed on TCDB: every request is a page read.

Parsing and row shaping are pure and unit-tested; the browser is only touched
from ``run_tcdb_wantlist``.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from shoebox.clients.tcdb import TcdbBrowser, TcdbLoginTimeout
from shoebox.models.tcdb import CATEGORIES, WantlistCard
from shoebox.settings import get_settings

logger = logging.getLogger(__name__)

FIELDS = (
    "category",
    "set_year",
    "set_name",
    "subset_name",
    "card_number",
    "player",
    "team",
    "notes",
    "note_detail",
    "title",
    "url",
    "set_id",
    "card_id",
    "item_id",
    "quantity",
    "price",
    "status",
)


def card_row(card: WantlistCard) -> dict:
    """One card as a flat row, in ``FIELDS`` order."""
    return {f: getattr(card, f) for f in FIELDS}


def default_out_path(exports_dir: Path, fmt: str, stamp: str | None = None) -> Path:
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    return exports_dir / fmt / f"tcdb_wantlist_{stamp}.{fmt}"


def write_wantlist(cards: list[WantlistCard], out_path: Path, fmt: str) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "jsonl":
        with out_path.open("w", encoding="utf-8") as fh:
            for card in cards:
                fh.write(json.dumps(card_row(card), ensure_ascii=False) + "\n")
    else:
        with out_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(FIELDS))
            writer.writeheader()
            for card in cards:
                writer.writerow(card_row(card))
    return out_path


def summary_table(counts: dict[str, int], total: int) -> Table:
    table = Table(title="TCDB want list", expand=False)
    table.add_column("Category")
    table.add_column("Cards", justify="right")
    for category, n in counts.items():
        table.add_row(category, f"{n:,}")
    table.add_row("[bold]Total[/bold]", f"[bold]{total:,}[/bold]")
    return table


def run_tcdb_wantlist(
    *,
    member: str | None = None,
    category: str | None = None,
    all_categories: bool = False,
    with_team: bool = False,
    limit: int | None = None,
    out: str | Path | None = None,
    fmt: str = "csv",
    max_pages: int | None = None,
    headless: bool = False,
    profile_dir: str | Path | None = None,
    console: Console | None = None,
) -> int:
    """Scrape the want list and write it out. Returns a process exit code."""
    console = console or Console()
    settings = get_settings()
    tcdb_cfg = settings.tcdb

    if fmt not in ("csv", "jsonl"):
        console.print(f"[red]Unknown format {fmt!r}. Use csv or jsonl.[/red]")
        return 2

    categories = (
        list(CATEGORIES)
        if all_categories
        else [category or tcdb_cfg.search_defaults.get("category") or "Baseball"]
    )
    bad = [c for c in categories if c not in CATEGORIES]
    if bad:
        console.print(f"[red]Unknown category {bad[0]!r}. One of: {', '.join(CATEGORIES)}[/red]")
        return 2

    cards: list[WantlistCard] = []
    counts: dict[str, int] = {}

    with TcdbBrowser(profile_dir or tcdb_cfg.profile_dir, headless=headless) as browser:
        try:
            browser.ensure_logged_in(
                timeout_s=tcdb_cfg.login_timeout_s,
                prompt=lambda msg: console.print(f"[bold cyan]{msg}[/bold cyan]"),
            )
        except TcdbLoginTimeout as e:
            console.print(f"[red]{e}[/red]")
            return 1

        who = member or browser.current_member()
        if not who:
            console.print(
                "[red]Could not work out which TCDB account is signed in. "
                "Pass --member <username>.[/red]"
            )
            return 1
        console.print(f"Signed in as [bold]{who}[/bold]")

        for cat in categories:
            before = len(cards)
            try:
                for page in browser.iter_wantlist(who, category=cat, max_pages=max_pages):
                    cards.extend(page.cards)
                    if page.total_records:
                        console.print(
                            f"  {cat}: page {page.page_index}/{page.total_pages or '?'} "
                            f"({len(cards) - before:,} of {page.total_records:,})"
                        )
                    # Stop mid-category too, so --limit really does cost one page.
                    if limit is not None and len(cards) >= limit:
                        del cards[limit:]
                        break
            except Exception as e:  # one sport failing should not lose the rest
                logger.exception("TCDB want list failed for %s", cat)
                console.print(f"[red]  {cat}: {e}[/red]")
            n = len(cards) - before
            if n or not all_categories:
                counts[cat] = n
            if limit is not None and len(cards) >= limit:
                break

        if with_team and cards:
            # One page load per card, so this is opt-in and worth a progress line.
            console.print(f"Fetching teams for {len(cards):,} card(s)...")
            for i, card in enumerate(cards, 1):
                try:
                    card.team = browser.card_team(card.url)
                except Exception as e:  # one bad card should not lose the export
                    logger.warning("team lookup failed for %s: %s", card.url, e)
                if i % 25 == 0 or i == len(cards):
                    console.print(f"  teams: {i:,}/{len(cards):,}")

    if not cards:
        console.print("[yellow]No want-list cards found.[/yellow]")
        return 1

    out_path = Path(out) if out else default_out_path(Path(settings.paths.exports_dir), fmt)
    write_wantlist(cards, out_path, fmt)

    console.print(summary_table(counts, len(cards)))
    console.print(f"Wrote [bold]{out_path}[/bold]")
    return 0
