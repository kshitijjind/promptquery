"""`atlas-bot` command line: setup, catalog, interactive chat, one-shot conversations, API server."""
from __future__ import annotations

import json
import sys

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table as RichTable

from .config import Settings

console = Console()


def _engine(settings: Settings):
    from .engine import ReportingEngine

    return ReportingEngine(settings)


def _render_turn(turn, show_sql: bool) -> None:
    if turn.sql and show_sql:
        console.print(Syntax(turn.sql, "sql", theme="ansi_dark", word_wrap=True))
    if turn.columns:
        table = RichTable(show_lines=False, header_style="bold")
        for c in turn.columns:
            table.add_column(c)
        for r in turn.rows[:50]:
            table.add_row(*["" if v is None else (f"{v:,.2f}" if isinstance(v, float) else str(v)) for v in r])
        console.print(table)
        if turn.row_count > 50 or turn.truncated:
            console.print(f"[dim]showing 50 of {turn.row_count}{'+' if turn.truncated else ''} rows[/dim]")
    console.print(Panel(Markdown(turn.answer or ""), border_style="green" if turn.ok else "red"))


@click.group()
@click.option("--env-file", default=".env", show_default=True, help="Optional KEY=VALUE env file.")
@click.pass_context
def main(ctx: click.Context, env_file: str) -> None:
    """Atlas AI reporting bot: ask the Atlas ERP database questions in plain English."""
    ctx.obj = Settings.from_env(env_file)


@main.command("setup-db")
@click.option("--admin-dsn", envvar="ATLAS_ADMIN_DSN", required=True,
              help="Owner/admin DSN of the Atlas database.")
@click.option("--password", envvar="ATLAS_REPORTING_PASSWORD", default="reporting_pw",
              show_default=True, help="Password to set for the atlas_reporting role.")
@click.pass_obj
def setup_db(settings: Settings, admin_dsn: str, password: str) -> None:
    """Create the read-only role and tenant-scoped `rpt` views (idempotent)."""
    from ..atlas.setup_reporting import setup_reporting

    views = setup_reporting(admin_dsn, password, f"{settings.statement_timeout_ms}ms")
    console.print(f"[green]ok[/green]: {len(views)} reporting views in schema rpt; role atlas_reporting ready")
    settings.catalog_cache.unlink(missing_ok=True)


@main.command("drop-db-views")
@click.option("--admin-dsn", envvar="ATLAS_ADMIN_DSN", required=True,
              help="Owner/admin DSN of the Atlas database.")
@click.option("--drop-role", is_flag=True, help="Also drop the atlas_reporting role.")
@click.confirmation_option(prompt="Drop the rpt reporting schema (views only; no Atlas data)?")
@click.pass_obj
def drop_db_views(settings: Settings, admin_dsn: str, drop_role: bool) -> None:
    """Remove the `rpt` views so Atlas migrations can ALTER/DROP columns. Re-run setup-db after."""
    from ..atlas.setup_reporting import drop_reporting

    drop_reporting(admin_dsn, drop_role=drop_role)
    settings.catalog_cache.unlink(missing_ok=True)
    console.print("[green]ok[/green]: schema rpt dropped"
                  + (" and role atlas_reporting removed" if drop_role else "")
                  + "; run `atlas-bot setup-db` after migrating Atlas")


@main.command("build-catalog")
@click.pass_obj
def build_catalog(settings: Settings) -> None:
    """(Re)introspect the reporting schema into the JSON cache."""
    from .catalog import load_catalog
    from .executor import ReportingExecutor

    ex = ReportingExecutor(settings.reporting_dsn, settings.tenant, settings.statement_timeout_ms)
    cat = load_catalog(ex, settings.catalog_cache, refresh=True)
    fks = sum(len(t.foreign_keys) for t in cat.schema.tables)
    console.print(f"[green]ok[/green]: {len(cat.schema.tables)} tables, {fks} relationships -> {settings.catalog_cache}")


@main.command()
@click.pass_obj
def tenants(settings: Settings) -> None:
    """List the companies (tenants) available for reporting."""
    from .executor import ReportingExecutor

    ex = ReportingExecutor(settings.reporting_dsn, "all", settings.statement_timeout_ms)
    for t in ex.directory():
        console.print(f"{t['slug']:<24} {t['name']}")


@main.command()
@click.option("--tenant", default=None, help="Company slug(s) or 'all' (default: ATLAS_TENANT).")
@click.option("--show-sql/--hide-sql", default=True, show_default=True)
@click.pass_obj
def chat(settings: Settings, tenant: str | None, show_sql: bool) -> None:
    """Interactive multi-turn reporting chat. Commands: /reset /tenant <slug> /sql /exit"""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory

    engine = _engine(settings)
    session = engine.session(tenant)
    console.print(Panel(f"Atlas reporting bot - company: [bold]{session.company}[/bold]\n"
                        "Ask a question. /reset starts a new conversation, /tenant <slug> switches "
                        "company, /sql toggles SQL, /exit quits.", border_style="cyan"))
    prompt = PromptSession(history=InMemoryHistory())
    while True:
        try:
            text = prompt.prompt(f"[{len(session.turns) + 1}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text in {"/exit", "/quit"}:
            break
        if text == "/reset":
            session.reset()
            console.print("[dim]conversation reset[/dim]")
            continue
        if text == "/sql":
            show_sql = not show_sql
            continue
        if text.startswith("/tenant"):
            slug = text.split(maxsplit=1)[1] if " " in text else ""
            try:
                session = engine.session(slug)
                console.print(f"[dim]now reporting on {session.company}; conversation reset[/dim]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")
            continue
        with console.status("thinking...") as status:
            turn = session.ask(text, progress=lambda m: status.update(m + "..."))
        _render_turn(turn, show_sql)


@main.command()
@click.argument("questions", nargs=-1, required=True)
@click.option("--tenant", default=None, help="Company slug(s) or 'all'.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
@click.pass_obj
def ask(settings: Settings, questions: tuple[str, ...], tenant: str | None, as_json: bool) -> None:
    """Run one conversation non-interactively: each QUESTION is the next prompt."""
    engine = _engine(settings)
    session = engine.session(tenant)
    results = []
    for q in questions:
        turn = session.ask(q)
        if as_json:
            results.append(turn.to_dict())
        else:
            console.print(f"\n[bold cyan]> {q}[/bold cyan]")
            _render_turn(turn, show_sql=True)
    if as_json:
        json.dump(results, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    if any(not r.ok for r in session.turns):
        sys.exit(1)


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8088, show_default=True, type=int)
@click.pass_obj
def serve(settings: Settings, host: str, port: int) -> None:
    """Start the HTTP API (POST /chat) and a minimal web chat UI at /."""
    import uvicorn

    from .server import create_app

    uvicorn.run(create_app(settings), host=host, port=port)


if __name__ == "__main__":
    main()
