from __future__ import annotations

import csv
import io
import json
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any

import click
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console

from . import __version__
from .db import Database
from .llm import LLMClient, LLMError, extract_sql, make_client
from .prompts import build_system_prompt
from .render import render_results, render_sql
from .repair import execute_with_repair
from .retrieval import TfIdfRetriever, expand_via_fks, llm_select_tables
from .safety import UnsafeQuery, validate_select_only
from .schema import Schema, introspect


# -- Output formats --------------------------------------------------------

class OutputFormat(str, Enum):
    TABLE = "table"
    JSON = "json"
    CSV = "csv"


def format_rows(cols: list[str], rows: list[tuple], fmt: OutputFormat) -> str:
    if fmt is OutputFormat.JSON:
        out = [dict(zip(cols, [_jsonify(v) for v in row])) for row in rows]
        return json.dumps(out, indent=2, default=str)
    if fmt is OutputFormat.CSV:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        for row in rows:
            writer.writerow(["" if v is None else str(v) for v in row])
        return buf.getvalue().rstrip("\n")
    raise ValueError(f"format_rows does not render {fmt!r}; use render_results for TABLE")


def _jsonify(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    return str(v)


# -- Query execution -------------------------------------------------------

class Outcome(str, Enum):
    OK = "ok"
    LLM_ERROR = "llm_error"
    EMPTY_SQL = "empty_sql"
    UNSAFE = "unsafe"
    EXEC_ERROR = "exec_error"
    SKIPPED = "skipped"


@dataclass
class QueryResult:
    outcome: Outcome
    sql: str | None
    cols: list[str]
    rows: list[tuple]
    error: str | None = None


def run_question(
    question: str,
    schema: Schema,
    retriever: TfIdfRetriever,
    llm: LLMClient,
    selector_llm: LLMClient | None,
    db: Database,
    *,
    top_k: int,
    select_n: int,
    max_tables: int,
    confirm: bool,
    max_repair: int = 1,
    progress: Console | None = None,
    prompt_for_confirm=None,
) -> QueryResult:
    """Run a single NL question end-to-end. Used by both the REPL and --query mode."""
    ranked = retriever.rank(question, top_k=top_k)
    candidates = [t for t, score in ranked if score > 0] or [t for t, _ in ranked[:3]]

    if selector_llm is not None and len(candidates) > select_n:
        if progress:
            progress.print(f"[dim]Selecting from {len(candidates)} candidates...[/dim]")
        try:
            selected = llm_select_tables(question, candidates, selector_llm, max_select=select_n)
            if selected:
                candidates = selected
        except Exception as e:
            if progress:
                progress.print(f"[yellow]Selector error, using TF-IDF only:[/yellow] {e}")

    relevant = expand_via_fks(schema, candidates, max_total=max_tables)
    if progress:
        preview = ", ".join(t.qualified_name for t in relevant[:5])
        suffix = "..." if len(relevant) > 5 else ""
        progress.print(f"[dim]Using {len(relevant)} tables: {preview}{suffix}[/dim]")

    system_prompt = build_system_prompt(relevant)

    try:
        if progress:
            progress.print("[dim]Generating SQL...[/dim]")
        raw = llm.generate(system_prompt, question)
    except Exception as e:
        return QueryResult(Outcome.LLM_ERROR, None, [], [], str(e))

    sql = extract_sql(raw)
    if not sql:
        return QueryResult(Outcome.EMPTY_SQL, None, [], [], "LLM returned an empty response.")

    try:
        validate_select_only(sql)
    except UnsafeQuery as e:
        return QueryResult(Outcome.UNSAFE, sql, [], [], str(e))

    if progress:
        render_sql(progress, sql)

    if confirm:
        if prompt_for_confirm is None:
            return QueryResult(Outcome.SKIPPED, sql, [], [], "no confirm callback")
        answer = prompt_for_confirm().strip().lower()
        if answer not in {"y", "yes"}:
            return QueryResult(Outcome.SKIPPED, sql, [], [], "user declined")

    def _confirm_repaired(repaired_sql: str) -> bool:
        if not confirm:
            return True
        if progress:
            render_sql(progress, repaired_sql)
        if prompt_for_confirm is None:
            return False
        return prompt_for_confirm().strip().lower() in {"y", "yes"}

    result = execute_with_repair(
        db, llm, system_prompt, question, sql,
        max_repair=max_repair,
        confirm_cb=_confirm_repaired if confirm else None,
        progress_cb=(lambda msg: progress.print(f"[yellow]{msg}[/yellow]")) if progress else None,
    )
    if result.declined:
        return QueryResult(Outcome.SKIPPED, result.sql, [], [], "user declined")
    if result.error is not None:
        return QueryResult(Outcome.EXEC_ERROR, result.sql, [], [], result.error)
    if result.attempts and progress and not confirm:
        # Show the repaired SQL that actually ran (confirm mode already displayed it).
        render_sql(progress, result.sql)

    return QueryResult(Outcome.OK, result.sql, result.cols, result.rows)


# -- CLI -------------------------------------------------------------------

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("dsn", required=True)
@click.option(
    "--model",
    default=None,
    help="LLM model (e.g. claude-sonnet-4-6, gpt-4o, anthropic/claude-opus-4-7).",
)
@click.option(
    "--selector-model",
    default=None,
    help="LLM used for the table-selector step (defaults to --model). "
         "Recommended: a cheaper model (e.g. gpt-4o-mini) than the SQL generator.",
)
@click.option(
    "-q",
    "--query",
    default=None,
    help="Run a single question and exit (one-shot mode, machine-friendly output).",
)
@click.option(
    "--out",
    "out_format",
    type=click.Choice(["table", "json", "csv"], case_sensitive=False),
    default=None,
    help="Output format. Default: 'table' for REPL, 'json' for --query.",
)
@click.option(
    "--top-k",
    default=50,
    show_default=True,
    help="Number of candidate tables to surface from TF-IDF before the LLM selector.",
)
@click.option(
    "--select",
    "select_n",
    default=15,
    show_default=True,
    help="Tables the LLM selector picks from the TF-IDF candidates (before FK expansion).",
)
@click.option(
    "--max-tables",
    default=25,
    show_default=True,
    help="Maximum tables sent to the LLM after FK expansion.",
)
@click.option(
    "--max-repair",
    default=1,
    show_default=True,
    help="Repair rounds when a query fails: feed the database error back to the model "
         "and retry. 0 disables repair.",
)
@click.option(
    "--no-selector",
    is_flag=True,
    help="Disable the LLM table-selector and use TF-IDF + FK expansion only (v0.1 behavior).",
)
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    help="Skip the confirmation prompt before running queries (implied by --query).",
)
@click.version_option(__version__, prog_name="promptquery")
def main(dsn: str, model: str | None, selector_model: str | None,
         query: str | None, out_format: str | None,
         top_k: int, select_n: int, max_tables: int, max_repair: int,
         no_selector: bool, yes: bool) -> None:
    """PromptQuery — natural-language SQL for Postgres.

    DSN is a libpq connection string, e.g. postgresql://user:pass@host/db.

    Examples:

      Interactive REPL:
        promptquery postgresql://localhost/mydb

      One-shot query (machine-friendly JSON to stdout, progress to stderr):
        promptquery -q "how many users in Italy" postgresql://localhost/mydb

      One-shot CSV:
        promptquery -q "..." --out csv postgresql://localhost/mydb > rows.csv
    """
    # In --query mode all progress goes to stderr so stdout stays clean for piping.
    one_shot = query is not None
    progress = Console(stderr=one_shot)
    out_fmt = OutputFormat(
        out_format.lower() if out_format
        else ("json" if one_shot else "table")
    )

    try:
        llm = make_client(model)
    except LLMError as e:
        progress.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if no_selector:
        selector_llm = None
    else:
        try:
            selector_llm = make_client(selector_model) if selector_model else llm
        except LLMError as e:
            progress.print(f"[red]Selector LLM error:[/red] {e}")
            sys.exit(1)

    progress.print(f"[dim]Connecting to[/dim] {_redact(dsn)} [dim]...[/dim]")
    try:
        db_ctx = Database(dsn).__enter__()
    except Exception as e:
        progress.print(f"[red]Connection failed:[/red] {e}")
        sys.exit(1)

    try:
        progress.print("[dim]Introspecting schema...[/dim]")
        schema = introspect(db_ctx)
        if not schema.tables:
            progress.print("[yellow]No tables found in this database.[/yellow]")
            return
        selector_info = (
            f", selector: {selector_llm.name}/{selector_llm.model}"
            if selector_llm is not None and selector_llm is not llm
            else (" (selector: same)" if selector_llm is not None else " (selector: off)")
        )
        progress.print(f"[green]✓[/green] {len(schema.tables)} tables found "
                       f"[dim](sql: {llm.name}/{llm.model}{selector_info})[/dim]")

        retriever = TfIdfRetriever(schema)

        if one_shot:
            sys.exit(_run_one_shot(
                question=query, schema=schema, retriever=retriever,
                llm=llm, selector_llm=selector_llm, db=db_ctx,
                top_k=top_k, select_n=select_n, max_tables=max_tables,
                max_repair=max_repair, out_fmt=out_fmt, progress=progress,
            ))

        _run_repl(
            schema=schema, retriever=retriever,
            llm=llm, selector_llm=selector_llm, db=db_ctx,
            top_k=top_k, select_n=select_n, max_tables=max_tables,
            max_repair=max_repair, out_fmt=out_fmt, yes=yes, progress=progress,
        )
    finally:
        db_ctx.close()


def _run_one_shot(*, question, schema, retriever, llm, selector_llm, db,
                  top_k, select_n, max_tables, max_repair,
                  out_fmt: OutputFormat, progress: Console) -> int:
    result = run_question(
        question=question, schema=schema, retriever=retriever,
        llm=llm, selector_llm=selector_llm, db=db,
        top_k=top_k, select_n=select_n, max_tables=max_tables,
        max_repair=max_repair, confirm=False, progress=progress,
    )

    if result.outcome is Outcome.LLM_ERROR:
        progress.print(f"[red]LLM error:[/red] {result.error}")
        return 1
    if result.outcome is Outcome.EMPTY_SQL:
        progress.print(f"[red]{result.error}[/red]")
        return 1
    if result.outcome is Outcome.UNSAFE:
        progress.print(f"[red]Refusing to run query:[/red] {result.error}")
        if result.sql:
            render_sql(progress, result.sql)
        return 2
    if result.outcome is Outcome.EXEC_ERROR:
        progress.print(f"[red]Query error:[/red] {result.error}")
        return 3

    # OK — write results to stdout in the chosen format.
    if out_fmt is OutputFormat.TABLE:
        # Table output goes to stdout via a non-stderr console so it can be piped.
        stdout_console = Console()
        render_results(stdout_console, result.cols, result.rows)
    else:
        sys.stdout.write(format_rows(result.cols, result.rows, out_fmt) + "\n")
    return 0


def _run_repl(*, schema, retriever, llm, selector_llm, db,
              top_k, select_n, max_tables, max_repair,
              out_fmt: OutputFormat, yes: bool, progress: Console) -> None:
    session: PromptSession[str] = PromptSession(history=InMemoryHistory())
    progress.print(
        "\n[bold]PromptQuery[/bold] — ask a question in plain English, "
        "or type [bold]exit[/bold] to quit.\n"
    )

    while True:
        try:
            question = session.prompt("? ").strip()
        except (KeyboardInterrupt, EOFError):
            progress.print("\n[dim]bye[/dim]")
            return
        if not question:
            continue
        if question.lower() in {"exit", "quit", r"\q"}:
            return

        result = run_question(
            question=question, schema=schema, retriever=retriever,
            llm=llm, selector_llm=selector_llm, db=db,
            top_k=top_k, select_n=select_n, max_tables=max_tables,
            max_repair=max_repair, confirm=not yes, progress=progress,
            prompt_for_confirm=(lambda: session.prompt("Run? [y/N] ")) if not yes else None,
        )

        if result.outcome is Outcome.LLM_ERROR:
            progress.print(f"[red]LLM error:[/red] {result.error}")
        elif result.outcome is Outcome.EMPTY_SQL:
            progress.print(f"[red]{result.error}[/red]")
        elif result.outcome is Outcome.UNSAFE:
            progress.print(f"[red]Refusing to run query:[/red] {result.error}")
            if result.sql:
                render_sql(progress, result.sql)
        elif result.outcome is Outcome.EXEC_ERROR:
            progress.print(f"[red]Query error:[/red] {result.error}\n")
        elif result.outcome is Outcome.SKIPPED:
            progress.print("[dim]Skipped.[/dim]\n")
        else:  # OK
            if out_fmt is OutputFormat.TABLE:
                render_results(progress, result.cols, result.rows)
            else:
                progress.print(format_rows(result.cols, result.rows, out_fmt))
            progress.print()


def _redact(dsn: str) -> str:
    if "://" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("://")
    if "@" in rest:
        creds, _, host = rest.partition("@")
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return dsn


if __name__ == "__main__":
    main()
