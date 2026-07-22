"""Slack mrkdwn formatting helpers.

Slack has no native table markup, so `table()` renders a column-aligned grid
inside a fenced code block (monospace keeps the alignment intact, mirroring
what `rich`/`render_table` produces in the terminal).
"""

from __future__ import annotations

# Not a hard Slack API limit (chat.postMessage allows far more) -- kept low so
# a single message stays readable and doesn't get collapsed behind "see more".
MAX_MESSAGE_CHARS = 2900

# Spaces between table columns.
COLUMN_SPACING = 4

# Code blocks don't get horizontal scroll on Slack mobile, so a row wider than
# this wraps mid-cell and destroys the alignment. Tables wider than this fall
# back to a stacked "field: value" layout instead of side-by-side columns.
MOBILE_SAFE_WIDTH = 34


def title(text: str) -> str:
    return f"*{text}*"


def bullets(items: list[str]) -> str:
    return "\n".join(f"• {item}" for item in items)


def _stacked(headers: list[str], str_rows: list[list[str]]) -> str:
    """One record per block, `Header: value` per line -- degrades gracefully
    on narrow screens since there's no fixed-width column to wrap mid-cell."""
    records = [
        "\n".join(f"{h}: {v}" for h, v in zip(headers, row, strict=False)) for row in str_rows
    ]
    return "```\n" + "\n\n".join(records) + "\n```"


def table(headers: list[str], rows: list[list], col_spacing: int = COLUMN_SPACING) -> str:
    str_rows = [[str(c) for c in row] for row in rows]
    widths = [max([len(headers[i])] + [len(r[i]) for r in str_rows]) for i in range(len(headers))]

    if sum(widths) + col_spacing * (len(widths) - 1) > MOBILE_SAFE_WIDTH:
        return _stacked(headers, str_rows)

    sep = " " * col_spacing

    def fmt_row(cells: list[str]) -> str:
        return sep.join(c.ljust(w) for c, w in zip(cells, widths, strict=False))

    lines = [fmt_row(headers), fmt_row(["-" * w for w in widths])]
    lines += [fmt_row(r) for r in str_rows]
    return "```\n" + "\n".join(lines) + "\n```"


def table_messages(
    heading: str,
    headers: list[str],
    rows: list[list],
    max_chars: int = MAX_MESSAGE_CHARS,
) -> list[str]:
    """Bold heading + fenced table, split across multiple messages if needed."""
    if not rows:
        return [f"{title(heading)}\n\n_No rows._"]

    # Reserve room for the heading line and a "(part i/N)" suffix that gets
    # added after chunking is decided, so the final message stays under max_chars.
    table_budget = max_chars - len(heading) - 20

    chunks: list[list[list]] = []
    current: list[list] = []
    for row in rows:
        candidate = current + [row]
        if current and len(table(headers, candidate)) > table_budget:
            chunks.append(current)
            current = [row]
        else:
            current = candidate
    chunks.append(current)

    messages = []
    for i, chunk in enumerate(chunks, start=1):
        heading_text = heading if len(chunks) == 1 else f"{heading} (part {i}/{len(chunks)})"
        messages.append(f"{title(heading_text)}\n\n{table(headers, chunk)}")
    return messages
