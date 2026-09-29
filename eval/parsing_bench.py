"""Parsing-mode benchmark: naive vs PromptQuery on schemas where execution is impractical.

Used when we have a real big schema (Odoo, ERPNext, etc.) but no seeded data and no
gold reference SQL. Instead of executing queries, we PARSE the generated SQL and check:

    PASS  iff  must_retrieve_tables ⊆ tables_referenced_in_sql
                AND tables_referenced_in_sql ⊆ schema_tables  (no hallucinations)

Both pipelines use the SAME system-prompt template; only the schema block differs.

Usage:
    python -m eval.parsing_bench --fixture eval/fixtures/odoo.schema.json \
                                  --questions eval.questions.odoo \
                                  --model gpt-5.4
"""
from __future__ import annotations

import argparse
import importlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp
from rich.console import Console
from rich.table import Table as RichTable

from promptquery.llm import OpenAIClient, extract_sql
from promptquery.prompts import SYSTEM_PROMPT, format_schema
from promptquery.retrieval import TfIdfRetriever, expand_via_fks, llm_select_tables
from promptquery.safety import UnsafeQuery, validate_select_only
from promptquery.schema import Schema

EVAL_DIR = Path(__file__).resolve().parent


# -- Schema loading --------------------------------------------------------

def load_schema(path: Path) -> Schema:
    with open(path) as f:
        data = json.load(f)
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    return Schema.from_dict(data)


def all_table_names(schema: Schema) -> set[str]:
    return {t.name for t in schema.tables}


# -- Pipelines (same shape as end_to_end.py but parameterized) -------------

def naive_pipeline(question: str, schema: Schema, llm: OpenAIClient) -> str:
    system = SYSTEM_PROMPT.format(schema=format_schema(schema.tables))
    raw = llm.generate(system, question)
    return extract_sql(raw)


def promptquery_pipeline(question: str, schema: Schema, llm: OpenAIClient,
                         top_k: int = 15, max_tables: int = 25) -> tuple[str, list[str]]:
    """v1 retrieval: TF-IDF (now stemmed) → FK expansion → SQL gen."""
    retriever = TfIdfRetriever(schema)
    ranked = retriever.rank(question, top_k=top_k)
    seed = [t for t, score in ranked if score > 0] or [t for t, _ in ranked[:5]]
    relevant = expand_via_fks(schema, seed, max_total=max_tables)
    system = SYSTEM_PROMPT.format(schema=format_schema(relevant))
    raw = llm.generate(system, question)
    sql = extract_sql(raw)
    return sql, [t.qualified_name for t in relevant]


def promptquery_v2_pipeline(
    question: str, schema: Schema,
    sql_llm: OpenAIClient, selector_llm: OpenAIClient,
    tfidf_top_k: int = 50, llm_select_top: int = 15, max_tables: int = 25,
) -> tuple[str, list[str]]:
    """v2 retrieval: TF-IDF (top-50) → LLM selector (top-15) → FK expansion → SQL gen.

    The LLM selector handles semantic mismatch that TF-IDF cannot
    (e.g. 'invoice' → `account_move`, 'shipment' → `stock_picking`).
    Uses a separate (cheaper) LLM client for the selector to keep cost down.
    """
    retriever = TfIdfRetriever(schema)
    ranked = retriever.rank(question, top_k=tfidf_top_k)
    candidates = [t for t, _ in ranked]
    selected = llm_select_tables(question, candidates, selector_llm,
                                  max_select=llm_select_top)
    if not selected:
        selected = candidates[:llm_select_top]
    relevant = expand_via_fks(schema, selected, max_total=max_tables)
    system = SYSTEM_PROMPT.format(schema=format_schema(relevant))
    raw = sql_llm.generate(system, question)
    sql = extract_sql(raw)
    return sql, [t.qualified_name for t in relevant]


# -- Parsing scorer --------------------------------------------------------

def extract_referenced_tables(sql: str) -> set[str]:
    """Return the set of table names referenced in a SQL statement."""
    tree = sqlglot.parse_one(sql, dialect="postgres")
    names: set[str] = set()
    for tbl in tree.find_all(exp.Table):
        name = tbl.name
        if name:
            names.add(name.lower())
    return names


def score_parsing(sql: str | None, must_retrieve: set[str],
                  schema_tables: set[str]) -> tuple[bool, str, set[str]]:
    """Returns (passed, reason, referenced_tables)."""
    if not sql:
        return False, "empty SQL", set()
    try:
        referenced = extract_referenced_tables(sql)
    except Exception as e:
        return False, f"parse error: {type(e).__name__}: {str(e)[:60]}", set()
    if not referenced:
        return False, "no tables referenced", set()

    must = {t.lower() for t in must_retrieve}
    schema = {t.lower() for t in schema_tables}

    missing = must - referenced
    if missing:
        return False, f"missing: {','.join(sorted(missing))[:80]}", referenced

    hallucinated = referenced - schema
    if hallucinated:
        return False, f"hallucinated: {','.join(sorted(hallucinated))[:80]}", referenced

    return True, "ok", referenced


# -- Outcome + runner ------------------------------------------------------

@dataclass
class Outcome:
    pipeline: str
    question: str
    sql: str | None
    referenced: list[str]
    passed: bool
    reason: str
    elapsed_s: float
    extra: dict = field(default_factory=dict)


def run_one(pipeline_name: str, question_text: str, must_retrieve: set[str],
            schema_tables: set[str], sql: str | None, error: str | None,
            extra: dict, start_time: float) -> Outcome:
    if error:
        return Outcome(
            pipeline=pipeline_name,
            question=question_text,
            sql=sql,
            referenced=[],
            passed=False,
            reason=error,
            elapsed_s=time.perf_counter() - start_time,
            extra=extra,
        )
    passed, reason, referenced = score_parsing(sql, must_retrieve, schema_tables)
    return Outcome(
        pipeline=pipeline_name,
        question=question_text,
        sql=sql,
        referenced=sorted(referenced),
        passed=passed,
        reason=reason,
        elapsed_s=time.perf_counter() - start_time,
        extra=extra,
    )


def _run_pq_variant(name: str, q, schema: Schema, schema_tables: set[str],
                    runner_fn, must: set[str]) -> Outcome:
    """Wrap a PQ pipeline call with timing + safety-guard + error capture."""
    t0 = time.perf_counter()
    sql, err, extra = None, None, {}
    try:
        sql, retrieved = runner_fn()
        extra = {"retrieved": retrieved}
        try:
            validate_select_only(sql)
        except UnsafeQuery as e:
            err = f"safety-rejected: {e}"
            sql = None
    except Exception as e:
        err = f"llm error: {type(e).__name__}: {str(e).splitlines()[0][:100]}"
    return run_one(name, q.text, must, schema_tables, sql, err, extra, t0)


def evaluate_question(
    q, schema: Schema, schema_tables: set[str],
    sql_llm: OpenAIClient, selector_llm: OpenAIClient,
) -> tuple[Outcome, Outcome, Outcome]:
    """Returns (naive_outcome, pq_v1_outcome, pq_v2_outcome)."""
    must = set(q.must_retrieve)

    # --- naive ---
    t0 = time.perf_counter()
    naive_sql, naive_err = None, None
    try:
        naive_sql = naive_pipeline(q.text, schema, sql_llm)
    except Exception as e:
        naive_err = f"llm error: {type(e).__name__}: {str(e).splitlines()[0][:100]}"
    naive = run_one("naive", q.text, must, schema_tables,
                    naive_sql, naive_err, {}, t0)

    # --- promptquery v1 (TF-IDF + FK) ---
    pq_v1 = _run_pq_variant(
        "pq_v1", q, schema, schema_tables,
        lambda: promptquery_pipeline(q.text, schema, sql_llm),
        must,
    )

    # --- promptquery v2 (TF-IDF wide + LLM selector + FK) ---
    pq_v2 = _run_pq_variant(
        "pq_v2", q, schema, schema_tables,
        lambda: promptquery_v2_pipeline(q.text, schema, sql_llm, selector_llm),
        must,
    )

    return naive, pq_v1, pq_v2


# -- Reporting -------------------------------------------------------------

def render(console: Console, per_q: list[tuple],
           totals_s: dict[str, float],
           schema_name: str, n_tables: int,
           sql_model: str, selector_model: str) -> dict:
    table = RichTable(
        show_header=True, header_style="bold cyan",
        title=f"Per-question results — {schema_name} ({n_tables} tables) · "
              f"sql={sql_model}, selector={selector_model}",
    )
    table.add_column("#", width=3, style="dim")
    table.add_column("Question", overflow="fold", max_width=36)
    table.add_column("Naive", justify="center", width=5)
    table.add_column("PQ v1", justify="center", width=5)
    table.add_column("PQ v2", justify="center", width=5)
    table.add_column("v1 reason", overflow="fold", max_width=22)
    table.add_column("v2 reason", overflow="fold", max_width=22)

    naive_pass = v1_pass = v2_pass = 0
    for i, (q, n, p1, p2) in enumerate(per_q, start=1):
        if n.passed:
            naive_pass += 1
        if p1.passed:
            v1_pass += 1
        if p2.passed:
            v2_pass += 1
        def mark(o):
            return "[green]✓[/green]" if o.passed else "[red]✗[/red]"
        table.add_row(str(i), q.text, mark(n), mark(p1), mark(p2), p1.reason, p2.reason)
    console.print(table)

    total = len(per_q)
    summary = RichTable(show_header=True, header_style="bold magenta", title="Summary")
    summary.add_column("Metric", style="bold")
    summary.add_column("Naive", justify="right")
    summary.add_column("PQ v1", justify="right")
    summary.add_column("PQ v2", justify="right")
    summary.add_row(
        "Pass rate",
        f"{naive_pass}/{total}  ({100*naive_pass/max(total,1):.1f}%)",
        f"{v1_pass}/{total}  ({100*v1_pass/max(total,1):.1f}%)",
        f"{v2_pass}/{total}  ({100*v2_pass/max(total,1):.1f}%)",
    )
    def fmt_delta(d):
        return ("[green]+" if d > 0 else ("[red]" if d < 0 else "")) + \
                              f"{d:+d}q ({100*d/max(total,1):+.1f}pp)" + \
                              ("[/green]" if d > 0 else ("[/red]" if d < 0 else ""))
    summary.add_row("vs Naive (delta)", "0", fmt_delta(v1_pass - naive_pass),
                    fmt_delta(v2_pass - naive_pass))
    summary.add_row(
        "Avg latency",
        f"{totals_s['naive']/max(total,1):.2f}s",
        f"{totals_s['v1']/max(total,1):.2f}s",
        f"{totals_s['v2']/max(total,1):.2f}s",
    )
    console.print(summary)

    return {
        "schema_name": schema_name,
        "n_tables": n_tables,
        "sql_model": sql_model,
        "selector_model": selector_model,
        "total": total,
        "naive_pass": naive_pass,
        "v1_pass": v1_pass,
        "v2_pass": v2_pass,
        "delta_v1_vs_naive": v1_pass - naive_pass,
        "delta_v2_vs_naive": v2_pass - naive_pass,
        "avg_naive_s": totals_s["naive"] / max(total, 1),
        "avg_v1_s":    totals_s["v1"]    / max(total, 1),
        "avg_v2_s":    totals_s["v2"]    / max(total, 1),
        # Legacy keys kept for any downstream consumers:
        "model": sql_model,
        "pq_pass": v2_pass,
        "delta": v2_pass - naive_pass,
        "avg_pq_s": totals_s["v2"] / max(total, 1),
    }


# -- main ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.parsing_bench")
    parser.add_argument("--fixture", type=Path, required=True,
                        help="Schema JSON, e.g. eval/fixtures/odoo.schema.json")
    parser.add_argument("--questions", required=True,
                        help="Dotted import path of a module exporting QUESTIONS, "
                             "e.g. eval.questions.odoo")
    parser.add_argument("--model", default="gpt-5.4",
                        help="LLM used for SQL generation (naive + PQ v1/v2).")
    parser.add_argument("--selector-model", default=None,
                        help="LLM used for the v0.2 table-selector step. "
                             "Defaults to --model. A cheaper model (e.g. gpt-4o-mini) "
                             "is recommended.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", type=Path, default=EVAL_DIR / "results_parsing.json")
    parser.add_argument("--schema-name", default=None,
                        help="Pretty name for the schema (default: derived from fixture filename)")
    args = parser.parse_args()

    console = Console()
    module = importlib.import_module(args.questions)
    questions = module.QUESTIONS[: args.limit] if args.limit else module.QUESTIONS

    schema = load_schema(args.fixture)
    schema_tables = all_table_names(schema)
    schema_name = args.schema_name or args.fixture.stem.replace(".schema", "")

    sql_llm = OpenAIClient(model=args.model)
    selector_model = args.selector_model or args.model
    selector_llm = sql_llm if selector_model == args.model else OpenAIClient(model=selector_model)
    console.print(
        f"[dim]SQL model:[/dim] [bold]{args.model}[/bold] "
        f"[dim]· Selector model:[/dim] [bold]{selector_model}[/bold]\n"
        f"[dim]Schema:[/dim] {schema_name} ({len(schema.tables)} tables) "
        f"[dim]· questions: {len(questions)}[/dim]\n"
    )

    per_q: list[tuple] = []
    totals_s = {"naive": 0.0, "v1": 0.0, "v2": 0.0}
    for i, q in enumerate(questions, start=1):
        console.print(f"  [dim]{i:>2}/{len(questions)}[/dim] {q.text[:70]}")
        n, p1, p2 = evaluate_question(q, schema, schema_tables, sql_llm, selector_llm)
        per_q.append((q, n, p1, p2))
        totals_s["naive"] += n.elapsed_s
        totals_s["v1"]    += p1.elapsed_s
        totals_s["v2"]    += p2.elapsed_s

    console.print()
    summary = render(console, per_q, totals_s,
                     schema_name, len(schema.tables), args.model, selector_model)

    raw = [
        {
            "question": q.text,
            "must_retrieve": list(q.must_retrieve),
            "naive": asdict(n),
            "pq_v1": asdict(p1),
            "pq_v2": asdict(p2),
        }
        for q, n, p1, p2 in per_q
    ]
    args.out.write_text(json.dumps({"summary": summary, "raw": raw}, indent=2))
    console.print(f"\n[dim]Raw results written to {args.out}[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
