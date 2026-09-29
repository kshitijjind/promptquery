"""End-to-end comparison: naive API call vs PromptQuery.

Two pipelines:
  1. NAIVE   — full schema in the prompt, LLM generates SQL, execute.
  2. PROMPTQUERY — retrieve relevant tables, filtered prompt, LLM, safety guard, execute.

Both use:
  - the same LLM (default: gpt-5.4 via OpenAI)
  - the same system prompt template (only the schema block differs)
  - the same Postgres test database (real execution, real result sets)

Scoring:
  - Execute the gold reference SQL once per question → gold result set
  - Execute each pipeline's generated SQL → candidate result set
  - PASS iff result sets match as sets of row tuples (order-insensitive)
  - Errors (parse fail, exec fail, safety-rejected, hallucinated table) count as FAIL

Usage:
  python -m eval.end_to_end --model gpt-5.4
  python -m eval.end_to_end --model gpt-5.4 --pad 0
  python -m eval.end_to_end --model gpt-5.4 --pad 200
  python -m eval.end_to_end --model gpt-5.4 --pad 0 --pad 200   # run both, compare
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table as RichTable

from promptquery.llm import OpenAIClient, extract_sql
from promptquery.prompts import SYSTEM_PROMPT, format_schema
from promptquery.retrieval import TfIdfRetriever, expand_via_fks
from promptquery.safety import UnsafeQuery, validate_select_only
from promptquery.schema import Schema

from .padding import pad_schema
from .questions.shop import QUESTIONS, Question
from .retrieval import load_schema as load_schema_json

EVAL_DIR = Path(__file__).resolve().parent
SCHEMA_JSON = EVAL_DIR / "fixtures" / "shop.schema.json"
DEFAULT_DSN = "postgresql://promptquery:promptquery@127.0.0.1:55432/shop"


# -- DB helper -------------------------------------------------------------

def execute(dsn: str, sql: str) -> set[tuple]:
    """Execute SQL read-only and return the result set as a frozenset of row tuples.

    Returning a set makes the comparison order-insensitive, which matches how
    most NL-to-SQL benchmarks score execution-equality.
    """
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '15s'")
            cur.execute("SET default_transaction_read_only = on")
            cur.execute(sql)
            if cur.description is None:
                return set()
            rows = cur.fetchall()
            # Normalize each cell to a JSON-stringified scalar so dict/datetime
            # values compare cleanly.
            normalized = {
                tuple(json.dumps(v, default=str, sort_keys=True) for v in row)
                for row in rows
            }
            return normalized


# -- Pipelines -------------------------------------------------------------

def naive_pipeline(question: str, schema: Schema, llm: OpenAIClient) -> str:
    """Dump the entire schema, ask for SQL. No retrieval, no safety guard.
    This is the strawman: "the LLM with full schema context."
    """
    system = SYSTEM_PROMPT.format(schema=format_schema(schema.tables))
    raw = llm.generate(system, question)
    return extract_sql(raw)


def promptquery_pipeline(question: str, schema: Schema, llm: OpenAIClient,
                         top_k: int = 10, max_tables: int = 20) -> tuple[str, list[str]]:
    """Full PromptQuery: retrieve → prompt → LLM → safety. Returns (sql, retrieved_tables)."""
    retriever = TfIdfRetriever(schema)
    ranked = retriever.rank(question, top_k=top_k)
    seed = [t for t, score in ranked if score > 0] or [t for t, _ in ranked[:3]]
    relevant = expand_via_fks(schema, seed, max_total=max_tables)
    system = SYSTEM_PROMPT.format(schema=format_schema(relevant))
    raw = llm.generate(system, question)
    sql = extract_sql(raw)
    return sql, [t.qualified_name for t in relevant]


# -- Result / scoring ------------------------------------------------------

@dataclass
class Outcome:
    pipeline: str          # "naive" or "promptquery"
    question: str
    sql: str | None
    error: str | None
    passed: bool
    n_rows_gold: int
    n_rows_candidate: int
    elapsed_s: float
    extra: dict = field(default_factory=dict)


def run_question(q: Question, schema: Schema, llm: OpenAIClient, dsn: str,
                 gold_set: set[tuple]) -> tuple[Outcome, Outcome]:
    # --- naive ---
    t0 = time.perf_counter()
    naive_outcome = _evaluate_pipeline(
        pipeline_name="naive",
        question=q.text,
        run=lambda: (naive_pipeline(q.text, schema, llm), {}),
        dsn=dsn,
        gold_set=gold_set,
        safety_check=False,
        start_time=t0,
    )

    # --- promptquery ---
    t0 = time.perf_counter()
    def run_pq():
        sql, retrieved = promptquery_pipeline(q.text, schema, llm)
        return sql, {"retrieved": retrieved}
    pq_outcome = _evaluate_pipeline(
        pipeline_name="promptquery",
        question=q.text,
        run=run_pq,
        dsn=dsn,
        gold_set=gold_set,
        safety_check=True,
        start_time=t0,
    )
    return naive_outcome, pq_outcome


def _evaluate_pipeline(pipeline_name: str, question: str, run, dsn: str,
                       gold_set: set[tuple], safety_check: bool,
                       start_time: float) -> Outcome:
    sql = None
    extra: dict = {}
    error: str | None = None
    candidate_set: set[tuple] = set()
    try:
        sql, extra = run()
        if not sql:
            error = "empty SQL"
        elif safety_check:
            try:
                validate_select_only(sql)
            except UnsafeQuery as e:
                error = f"safety-rejected: {e}"
                sql = None
    except Exception as e:
        error = f"llm error: {type(e).__name__}: {e}"

    if sql and not error:
        try:
            candidate_set = execute(dsn, sql)
        except Exception as e:
            error = f"exec error: {type(e).__name__}: {str(e).splitlines()[0][:160]}"

    passed = (error is None) and (candidate_set == gold_set)
    return Outcome(
        pipeline=pipeline_name,
        question=question,
        sql=sql,
        error=error,
        passed=passed,
        n_rows_gold=len(gold_set),
        n_rows_candidate=len(candidate_set),
        elapsed_s=time.perf_counter() - start_time,
        extra=extra,
    )


# -- Reporting -------------------------------------------------------------

def render_per_question(console: Console, label: str,
                        outcomes: list[tuple[Question, Outcome, Outcome]]) -> None:
    table = RichTable(show_header=True, header_style="bold cyan",
                      title=f"Per-question results — {label}")
    table.add_column("#", width=3, style="dim")
    table.add_column("Question", overflow="fold", max_width=44)
    table.add_column("Naive", justify="center", width=6)
    table.add_column("PQ",    justify="center", width=6)
    table.add_column("Naive error / rows", overflow="fold", max_width=24)
    table.add_column("PQ error / rows",    overflow="fold", max_width=24)
    for i, (q, n, p) in enumerate(outcomes, start=1):
        n_mark = "[green]✓[/green]" if n.passed else "[red]✗[/red]"
        p_mark = "[green]✓[/green]" if p.passed else "[red]✗[/red]"
        n_detail = n.error if n.error else f"{n.n_rows_candidate} rows"
        p_detail = p.error if p.error else f"{p.n_rows_candidate} rows"
        if n.passed:
            n_detail = f"[green]{n.n_rows_candidate} rows[/green]"
        if p.passed:
            p_detail = f"[green]{p.n_rows_candidate} rows[/green]"
        table.add_row(str(i), q.text, n_mark, p_mark, n_detail, p_detail)
    console.print(table)


def render_summary(console: Console, results: dict) -> None:
    table = RichTable(show_header=True, header_style="bold magenta",
                      title="Execution-accuracy summary")
    table.add_column("Config")
    table.add_column("Tables", justify="right")
    table.add_column("Naive %", justify="right")
    table.add_column("PromptQuery %", justify="right")
    table.add_column("Delta", justify="right")
    table.add_column("Naive avg latency", justify="right")
    table.add_column("PQ avg latency", justify="right")

    for cfg_name, cfg_data in results.items():
        n_pct = 100 * cfg_data["naive_pass"] / max(cfg_data["total"], 1)
        p_pct = 100 * cfg_data["pq_pass"] / max(cfg_data["total"], 1)
        delta = p_pct - n_pct
        delta_str = (
            f"[green]+{delta:.1f}[/green]" if delta > 0
            else (f"[red]{delta:.1f}[/red]" if delta < 0 else "0.0")
        )
        table.add_row(
            cfg_name,
            str(cfg_data["n_tables"]),
            f"{cfg_data['naive_pass']}/{cfg_data['total']}  ({n_pct:.1f}%)",
            f"{cfg_data['pq_pass']}/{cfg_data['total']}  ({p_pct:.1f}%)",
            delta_str,
            f"{cfg_data['naive_avg_s']:.2f}s",
            f"{cfg_data['pq_avg_s']:.2f}s",
        )
    console.print(table)


# -- Main ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.end_to_end")
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--pad", type=int, action="append",
                        help="Run with N noise tables padded onto the schema. "
                             "Pass multiple times to compare configs (e.g. --pad 0 --pad 200).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit to first N questions (for smoke-testing)")
    parser.add_argument("--out", type=Path, default=EVAL_DIR / "results.json",
                        help="Where to write raw per-question outcomes as JSON")
    args = parser.parse_args()

    pads = args.pad if args.pad else [0]
    questions = QUESTIONS[: args.limit] if args.limit else QUESTIONS

    console = Console()
    llm = OpenAIClient(model=args.model)
    console.print(f"[dim]Using model:[/dim] [bold]{args.model}[/bold] "
                  f"[dim]· questions: {len(questions)} · configs: {pads}[/dim]\n")

    base = load_schema_json(SCHEMA_JSON)
    all_results = {}
    raw_outcomes = {}

    # Pre-execute gold once (independent of pad config).
    console.print("[dim]Executing gold reference SQL...[/dim]")
    gold_sets: dict[str, set[tuple]] = {}
    gold_errors: list[str] = []
    for q in questions:
        try:
            gold_sets[q.text] = execute(args.dsn, q.reference_sql)
        except Exception as e:
            gold_errors.append(f"  {q.text}: {e}")
            gold_sets[q.text] = set()
    if gold_errors:
        console.print(f"[yellow]Warning: {len(gold_errors)} gold queries failed[/yellow]")
        for line in gold_errors:
            console.print(f"[yellow]{line}[/yellow]")

    for pad in pads:
        cfg_name = f"shop+{pad}-noise" if pad else "shop"
        schema = pad_schema(base, pad)
        console.print(f"\n[bold]== {cfg_name} ({len(schema.tables)} tables) ==[/bold]")

        per_q: list[tuple[Question, Outcome, Outcome]] = []
        for i, q in enumerate(questions, start=1):
            console.print(f"  [dim]{i:>2}/{len(questions)}[/dim] {q.text[:70]}", end="\n")
            naive, pq = run_question(q, schema, llm, args.dsn, gold_sets[q.text])
            per_q.append((q, naive, pq))

        render_per_question(console, cfg_name, per_q)
        all_results[cfg_name] = {
            "n_tables": len(schema.tables),
            "total": len(per_q),
            "naive_pass": sum(1 for _, n, _ in per_q if n.passed),
            "pq_pass":    sum(1 for _, _, p in per_q if p.passed),
            "naive_avg_s": sum(n.elapsed_s for _, n, _ in per_q) / max(len(per_q), 1),
            "pq_avg_s":    sum(p.elapsed_s for _, _, p in per_q) / max(len(per_q), 1),
        }
        raw_outcomes[cfg_name] = [
            {"question": q.text, "naive": asdict(n), "pq": asdict(p)}
            for q, n, p in per_q
        ]

    console.print()
    render_summary(console, all_results)

    args.out.write_text(json.dumps({
        "model": args.model,
        "configs": all_results,
        "raw": raw_outcomes,
    }, indent=2))
    console.print(f"\n[dim]Raw results written to {args.out}[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
