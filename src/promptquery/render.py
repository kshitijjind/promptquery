from __future__ import annotations

from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table as RichTable


def render_sql(console: Console, sql: str) -> None:
    syntax = Syntax(sql, "sql", theme="monokai", word_wrap=True, line_numbers=False)
    console.print(syntax)


def render_results(
    console: Console,
    columns: list[str],
    rows: list[tuple],
    limit: int = 100,
) -> None:
    if not columns:
        console.print("[dim]Query executed (no result set).[/dim]")
        return
    if not rows:
        console.print("[dim]No rows returned.[/dim]")
        return

    table = RichTable(show_header=True, header_style="bold cyan", show_lines=False)
    for col in columns:
        table.add_column(col, overflow="fold")

    for row in rows[:limit]:
        table.add_row(*[_fmt(v) for v in row])

    console.print(table)
    if len(rows) > limit:
        console.print(
            f"[dim]({len(rows) - limit} more rows truncated; showing first {limit})[/dim]"
        )
    console.print(f"[dim]{len(rows)} row(s)[/dim]")


def _fmt(value: object) -> str:
    if value is None:
        return "[dim]NULL[/dim]"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)
