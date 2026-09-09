"""Inspect what a saved search actually returns, without alerting anyone.

``watch-searches --dry-run`` prints a short sample, which is enough to sanity
check a query but not to *tune* one. This module returns the full result set as
a DataFrame — including the listings your post-filters rejected and the reason
each was rejected — so "are my parameters right?" becomes a question you can
answer by looking at data rather than by guessing.

Nothing here touches Slack, the seen-cache, or BigQuery. It is safe to run as
often as you like, subject only to the eBay Browse API quota.

Typical use from a REPL or notebook::

    from shoebox.pipelines.preview_search import preview_search

    df = preview_search("matt_cain_autos")
    df[df.passed]                          # what would alert
    df[~df.passed].dropped_by.value_counts()   # what your filters are costing
    df.sort_values("total_price").head(20)
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

import pandas as pd

from ..models.ebay.item_summary import ItemSummary
from ..models.saved_search import ResolvedSearch, load_searches_file
from ..settings import get_settings
from ..transforms.search_filters import (
    build_aspect_filter,
    build_browse_filter,
    cheapest_shipping,
    rejection_reason,
)

logger = logging.getLogger(__name__)

# Column order chosen for reading left-to-right in a terminal: identity, money,
# then the verdict, then the long fields.
COLUMNS = [
    "passed",
    "dropped_by",
    "title",
    "price",
    "shipping",
    "total_price",
    "buying",
    "condition",
    "seller",
    "feedback",
    "country",
    "listed",
    "item_id",
    "url",
]

# Terminal view is deliberately narrower than the DataFrame: the title is what
# you actually read when judging a filter, so it flexes and everything else is
# fixed and narrow. The full column set is always in the frame and the CSV.
#
# Not using ``utils.render_table`` here -- its expand/show_lines settings suit
# the narrow order tables and would wrap every listing title into an unreadable
# vertical stack.
_FIXED_COLUMNS = [
    ("price", "Price", 9, "right"),
    ("total_price", "Total", 9, "right"),
    ("seller", "Seller", 15, "left"),
]


def resolve_search(name: str, *, config_path: str | Path | None = None) -> ResolvedSearch:
    """Look up one search by name, with defaults merged in."""
    path = config_path or get_settings().paths.searches_file
    searches = {s.name: s for s in load_searches_file(path).resolved()}
    if name not in searches:
        known = ", ".join(sorted(searches)) or "(none defined)"
        raise KeyError(f"No search named {name!r} in {path}. Defined: {known}")
    return searches[name]


def describe_request(search: ResolvedSearch) -> dict[str, str | None]:
    """Exactly what would be sent to eBay, for printing next to the results."""
    return {
        "query": search.query,
        "category_ids": ",".join(search.category_ids) or None,
        "sort": search.sort,
        "filter": build_browse_filter(search),
        "aspect_filter": build_aspect_filter(search),
    }


def _row(item: ItemSummary, search: ResolvedSearch) -> dict:
    reason = rejection_reason(item, search)
    price = item.price_decimal
    ship = cheapest_shipping(item)
    return {
        "passed": reason is None,
        "dropped_by": reason,
        "title": item.title,
        "price": float(price) if price is not None else None,
        "shipping": float(ship) if ship is not None else None,
        "total_price": float(price + (ship or Decimal(0))) if price is not None else None,
        "buying": "/".join(item.buying_options),
        "condition": item.condition,
        "seller": item.seller.username if item.seller else None,
        "feedback": item.seller.feedback_score if item.seller else None,
        "country": item.item_location.country if item.item_location else None,
        "listed": item.item_origin_date,
        "item_id": item.item_id,
        "url": item.item_web_url,
    }


def _fetch(search: ResolvedSearch, max_results: int) -> list[ItemSummary]:
    from ..clients.ebay.client import get_client

    return get_client().browse.search(
        q=search.query,
        category_ids=",".join(search.category_ids) or None,
        filter=build_browse_filter(search),
        aspect_filter=build_aspect_filter(search),
        sort=search.sort,
        max_results=max_results,
    )


def preview_search(
    name: str,
    *,
    config_path: str | Path | None = None,
    max_results: int | None = None,
    passed_only: bool = False,
    fetch=None,
) -> pd.DataFrame:
    """Return every listing a saved search matches, with a pass/fail verdict.

    Args:
        name: Search name from searches.yaml.
        config_path: Override ``paths.searches_file``.
        max_results: Cap the fetch. Defaults to the search's ``seed_max_results``
            so you see the same breadth a seed would, not just one poll window.
        passed_only: Drop the rejected rows. Off by default — the rejected rows
            are usually the interesting ones when tuning.
        fetch: Injected fetcher, for tests.

    Returns:
        A DataFrame with one row per listing. ``passed`` is the verdict and
        ``dropped_by`` names the config key that rejected it. Empty results
        still return a correctly-shaped (zero-row) frame.
    """
    search = resolve_search(name, config_path=config_path)
    limit = max_results or search.seed_max_results

    items = (fetch or _fetch)(search, limit)
    rows = [_row(item, search) for item in items]

    df = pd.DataFrame(rows, columns=COLUMNS)
    if passed_only:
        df = df[df["passed"]].reset_index(drop=True)

    kept = int(df["passed"].sum()) if len(df) else 0
    logger.info(
        "%s: %d listing(s) returned by eBay, %d pass post-filters, %d rejected",
        name,
        len(rows),
        kept,
        len(rows) - kept,
    )
    return df


def aspect_options(
    name: str,
    *,
    config_path: str | Path | None = None,
    fetch=None,
) -> pd.DataFrame:
    """List the eBay aspects this search could filter on, with match counts.

    Aspects are eBay's structured item attributes (Player, Season, Grade,
    Parallel/Variety...). They vary by category and by what the current result
    set actually contains, so this is scoped to one saved search rather than
    being a static list.

    Returns a frame of ``aspect``, ``value``, ``count``, ``values_in_aspect`` —
    one row per selectable value, ordered by aspect then descending count. Copy
    an ``aspect`` / ``value`` pair straight into the search's ``aspects:`` block.
    """
    search = resolve_search(name, config_path=config_path)

    if fetch is None:

        def fetch(s: ResolvedSearch):
            from ..clients.ebay.client import get_client

            return get_client().browse.aspect_refinements(
                q=s.query,
                category_ids=",".join(s.category_ids) or None,
                filter=build_browse_filter(s),
            )

    rows = []
    for aspect in fetch(search):
        aspect_name = aspect.get("localized_aspect_name")
        values = aspect.get("aspect_value_distributions") or []
        for value in values:
            rows.append(
                {
                    "aspect": aspect_name,
                    "value": value.get("localized_aspect_value"),
                    "count": value.get("match_count"),
                    "values_in_aspect": len(values),
                }
            )

    df = pd.DataFrame(rows, columns=["aspect", "value", "count", "values_in_aspect"])
    if not df.empty:
        df = df.sort_values(["aspect", "count"], ascending=[True, False]).reset_index(drop=True)
    logger.info("%s: %d aspect(s), %d selectable value(s)", name, df["aspect"].nunique(), len(df))
    return df


def run_aspects(
    name: str,
    *,
    config_path: str | Path | None = None,
    top: int = 8,
    csv_path: str | Path | None = None,
) -> pd.DataFrame:
    """CLI entry point: print each aspect and its most common values."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    search = resolve_search(name, config_path=config_path)
    df = aspect_options(name, config_path=config_path)

    if df.empty:
        console.print(
            f"\n[yellow]No aspects returned for {search.name}.[/yellow] eBay reports "
            "aspects per category — check that category_ids is set."
        )
        return df

    console.print(
        f"\n[bold]{search.name}[/bold] — {df['aspect'].nunique()} aspects available "
        f"({len(df)} selectable values)\n"
    )

    table = Table(header_style="bold", expand=True)
    table.add_column("Aspect", ratio=2, min_width=14, overflow="ellipsis", no_wrap=True)
    table.add_column(f"Top {top} values (match count)", ratio=5, overflow="fold")
    table.add_column("All", width=4, justify="right")

    for aspect, group in df.groupby("aspect", sort=True):
        head = group.head(top)
        values = ", ".join(f"{r.value} [dim]({r.count:,})[/dim]" for r in head.itertuples())
        table.add_row(str(aspect), values, str(len(group)))
    console.print(table)

    console.print(
        "\n[dim]Use in searches.yaml (requires exactly one category_ids):[/dim]\n"
        "    aspects:\n"
        f'      {df.iloc[0]["aspect"]}: ["{df.iloc[0]["value"]}"]'
    )

    if csv_path:
        out = Path(csv_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False)
        console.print(f"\nAll {len(df)} values written to [bold]{out}[/bold]")

    return df


def _money(value) -> str:
    return "" if value is None or pd.isna(value) else f"{value:,.2f}"


def _build_table(df: pd.DataFrame, *, title: str):
    """One row per listing. Title flexes; a rejection column appears only when
    something was actually rejected, so a clean search isn't padded with an
    empty column."""
    from rich.table import Table

    has_rejections = len(df) > 0 and not df["passed"].all()

    table = Table(title=title, title_justify="left", header_style="bold", expand=True)
    table.add_column("", width=1, justify="center")
    table.add_column("Title", ratio=3, min_width=20, overflow="ellipsis", no_wrap=True)
    for _, header, width, justify in _FIXED_COLUMNS:
        table.add_column(header, width=width, justify=justify, overflow="ellipsis", no_wrap=True)
    if has_rejections:
        table.add_column("Rejected by", ratio=2, min_width=18, overflow="ellipsis", no_wrap=True)

    for _, row in df.iterrows():
        passed = bool(row["passed"])
        cells = [
            "[green]✓[/green]" if passed else "[red]✗[/red]",
            str(row["title"] or ""),
            _money(row["price"]),
            _money(row["total_price"]),
            str(row["seller"] or ""),
        ]
        if has_rejections:
            cells.append("" if passed else f"[red]{row['dropped_by']}[/red]")
        table.add_row(*cells, style=None if passed else "dim")
    return table


def run_preview(
    name: str,
    *,
    config_path: str | Path | None = None,
    max_results: int | None = None,
    passed_only: bool = False,
    csv_path: str | Path | None = None,
    limit_rows: int | None = None,
) -> pd.DataFrame:
    """CLI entry point: print the request, the results, and a filter breakdown."""
    from rich.console import Console

    console = Console()
    search = resolve_search(name, config_path=config_path)

    console.print(f"\n[bold]{search.name}[/bold] — what gets sent to eBay:")
    for key, value in describe_request(search).items():
        if value is not None:
            console.print(f"  {key:>13}: {value}")

    df = preview_search(
        name, config_path=config_path, max_results=max_results, passed_only=passed_only
    )

    if df.empty:
        console.print(
            "\n[yellow]No listings returned.[/yellow] The eBay-side filter above is "
            "too narrow, or the query matches nothing."
        )
        return df

    # The breakdown is the part that actually answers "are my params right?".
    rejected = df[~df["passed"]]
    console.print(
        f"\n[bold]{len(df)}[/bold] returned by eBay · "
        f"[green]{int(df['passed'].sum())} pass[/green] · "
        f"[red]{len(rejected)} rejected by post-filters[/red]"
    )
    if not rejected.empty:
        # Group by the config key, not the full reason -- reasons embed the
        # offending value ("2140 < 5000"), so grouping on them would print one
        # line per listing and tell you nothing about which filter is costly.
        console.print("\nRejected by:")
        for key, count in rejected["dropped_by"].str.split(":").str[0].value_counts().items():
            console.print(f"  {count:>4} × {key}")

    shown = df if limit_rows is None else df.head(limit_rows)
    console.print()
    console.print(_build_table(shown, title=f"{search.name} — showing {len(shown)} of {len(df)}"))
    if len(shown) < len(df):
        console.print(
            f"[dim]{len(df) - len(shown)} more rows hidden — use --rows 0 for all, "
            "or --csv to export.[/dim]"
        )

    if csv_path:
        out = Path(csv_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False)
        console.print(f"\nFull results ({len(df)} rows, all columns) written to [bold]{out}[/bold]")

    return df
