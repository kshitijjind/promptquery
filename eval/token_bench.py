"""Token-cost benchmark: measure the REAL size of the SQL-generator system prompt.

The accuracy benchmark (``eval/parsing_bench.py``) tells us *whether* each pipeline
retrieves the right tables. This benchmark tells us *how big* the resulting
SQL-generator system prompt is, in tokens, for each pipeline — the number that
backs the README's "Tokens / query" column.

For every question we build the EXACT system prompt the SQL generator would
receive (``SYSTEM_PROMPT.format(schema=format_schema(tables))``) under each
pipeline, and count its tokens with ``tiktoken.encoding_for_model("gpt-4o")``
(the ``o200k_base`` encoding). We do NOT call the expensive SQL generator — we
only build and measure the prompt. The single cheap call per question is the
v0.2 LLM table-selector (gpt-4o-mini by default), which genuinely shapes which
tables land in the prompt and therefore must run to get an honest count.

Pipelines (mirroring the production CLI ``run_question`` in
``src/promptquery/cli.py`` with its default params: top-k 50, select 15,
max-tables 25):

    naive   = all tables in the schema
    pq_v1   = TF-IDF (top-k) -> FK expansion              (no LLM selector)
    pq_v2   = TF-IDF (top-k) -> LLM selector (top select_n) -> FK expansion

Usage:
    python -m eval.token_bench --fixture eval/fixtures/odoo.schema.json \
                               --questions eval.questions.odoo \
                               --out eval/results_odoo_tokens.json
"""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path

import tiktoken
from rich.console import Console
from rich.table import Table as RichTable

from promptquery.llm import OpenAIClient
from promptquery.prompts import build_system_prompt
from promptquery.retrieval import TfIdfRetriever, expand_via_fks, llm_select_tables
from promptquery.schema import Schema

EVAL_DIR = Path(__file__).resolve().parent

# Encoding for token counting. gpt-4o (and gpt-4o-mini) use o200k_base.
TOKEN_MODEL = "gpt-4o"
_ENC = tiktoken.encoding_for_model(TOKEN_MODEL)


def count_tokens(text: str) -> int:
    return len(_ENC.encode(text))


# -- Schema loading (same as parsing_bench) --------------------------------

def load_schema(path: Path) -> Schema:
    with open(path) as f:
        data = json.load(f)
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    return Schema.from_dict(data)


# -- Pipelines: build the table set the SQL generator would see ------------
# These mirror src/promptquery/cli.py:run_question exactly so the measured
# prompt is the one the real pipeline emits.

def naive_tables(schema: Schema) -> list:
    return list(schema.tables)


def pq_v1_tables(question: str, schema: Schema, retriever: TfIdfRetriever,
                 *, top_k: int, max_tables: int) -> list:
    """TF-IDF -> FK expansion. No LLM selector (production --no-selector path)."""
    ranked = retriever.rank(question, top_k=top_k)
    candidates = [t for t, score in ranked if score > 0] or [t for t, _ in ranked[:3]]
    return expand_via_fks(schema, candidates, max_total=max_tables)


def pq_v2_tables(question: str, schema: Schema, retriever: TfIdfRetriever,
                 selector_llm: OpenAIClient,
                 *, top_k: int, select_n: int, max_tables: int) -> list:
    """TF-IDF -> LLM selector -> FK expansion (production default path)."""
    ranked = retriever.rank(question, top_k=top_k)
    candidates = [t for t, score in ranked if score > 0] or [t for t, _ in ranked[:3]]
    if selector_llm is not None and len(candidates) > select_n:
        try:
            selected = llm_select_tables(question, candidates, selector_llm,
                                         max_select=select_n)
            if selected:
                candidates = selected
        except Exception:
            # Degrade to v1 behavior, exactly like the production CLI does.
            pass
    return expand_via_fks(schema, candidates, max_total=max_tables)


# -- Runner ----------------------------------------------------------------

def _avg(values: list[int]) -> float:
    return sum(values) / len(values) if values else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.token_bench")
    parser.add_argument("--fixture", type=Path, required=True,
                        help="Schema JSON, e.g. eval/fixtures/odoo.schema.json")
    parser.add_argument("--questions", required=True,
                        help="Dotted import path of a module exporting QUESTIONS, "
                             "e.g. eval.questions.odoo")
    parser.add_argument("--selector-model", default="gpt-4o-mini",
                        help="Cheap LLM used for the v0.2 table-selector step.")
    parser.add_argument("--top-k", type=int, default=50,
                        help="TF-IDF candidates surfaced before the selector.")
    parser.add_argument("--select", dest="select_n", type=int, default=15,
                        help="Tables the LLM selector keeps before FK expansion.")
    parser.add_argument("--max-tables", type=int, default=25,
                        help="Max tables after FK expansion.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--schema-name", default=None)
    args = parser.parse_args()

    console = Console()
    module = importlib.import_module(args.questions)
    questions = module.QUESTIONS[: args.limit] if args.limit else module.QUESTIONS

    schema = load_schema(args.fixture)
    schema_name = args.schema_name or args.fixture.stem.replace(".schema", "")
    retriever = TfIdfRetriever(schema)
    selector_llm = OpenAIClient(model=args.selector_model)

    # Naive prompt is question-independent (all tables, always) — build once.
    naive_prompt = build_system_prompt(naive_tables(schema))
    naive_tok = count_tokens(naive_prompt)
    naive_n_tables = len(schema.tables)

    console.print(
        f"[dim]Token model:[/dim] [bold]{TOKEN_MODEL}[/bold] "
        f"([bold]{_ENC.name}[/bold]) "
        f"[dim]· Selector:[/dim] [bold]{args.selector_model}[/bold]\n"
        f"[dim]Schema:[/dim] {schema_name} ({naive_n_tables} tables) "
        f"[dim]· questions: {len(questions)} · "
        f"top-k {args.top_k}, select {args.select_n}, max-tables {args.max_tables}[/dim]\n"
        f"[dim]Naive prompt (all {naive_n_tables} tables):[/dim] "
        f"[bold]{naive_tok:,}[/bold] tokens\n"
    )

    raw: list[dict] = []
    naive_tokens: list[int] = []
    v1_tokens: list[int] = []
    v2_tokens: list[int] = []

    for i, q in enumerate(questions, start=1):
        v1_tabs = pq_v1_tables(q.text, schema, retriever,
                               top_k=args.top_k, max_tables=args.max_tables)
        v2_tabs = pq_v2_tables(q.text, schema, retriever, selector_llm,
                               top_k=args.top_k, select_n=args.select_n,
                               max_tables=args.max_tables)

        v1_prompt = build_system_prompt(v1_tabs)
        v2_prompt = build_system_prompt(v2_tabs)
        v1_tok = count_tokens(v1_prompt)
        v2_tok = count_tokens(v2_prompt)

        naive_tokens.append(naive_tok)
        v1_tokens.append(v1_tok)
        v2_tokens.append(v2_tok)

        raw.append({
            "question": q.text,
            "must_retrieve": list(q.must_retrieve),
            "naive": {"tables": naive_n_tables, "prompt_tokens": naive_tok},
            "pq_v1": {
                "tables": len(v1_tabs),
                "table_names": [t.qualified_name for t in v1_tabs],
                "prompt_tokens": v1_tok,
            },
            "pq_v2": {
                "tables": len(v2_tabs),
                "table_names": [t.qualified_name for t in v2_tabs],
                "prompt_tokens": v2_tok,
            },
        })

        console.print(
            f"  [dim]{i:>2}/{len(questions)}[/dim] {q.text[:54]:<54} "
            f"naive=[bold]{naive_tok:>6,}[/bold] "
            f"v1=[bold]{v1_tok:>5,}[/bold]({len(v1_tabs)}t) "
            f"v2=[bold]{v2_tok:>5,}[/bold]({len(v2_tabs)}t)"
        )

    avg_naive = _avg(naive_tokens)
    avg_v1 = _avg(v1_tokens)
    avg_v2 = _avg(v2_tokens)

    summary = {
        "schema_name": schema_name,
        "n_tables": naive_n_tables,
        "encoding": _ENC.name,
        "token_model": TOKEN_MODEL,
        "selector_model": args.selector_model,
        "params": {
            "top_k": args.top_k,
            "select_n": args.select_n,
            "max_tables": args.max_tables,
        },
        "total_questions": len(questions),
        "avg_tokens_naive": round(avg_naive, 1),
        "avg_tokens_pq_v1": round(avg_v1, 1),
        "avg_tokens_pq_v2": round(avg_v2, 1),
        "factor_naive_over_v1": round(avg_naive / avg_v1, 2) if avg_v1 else None,
        "factor_naive_over_v2": round(avg_naive / avg_v2, 2) if avg_v2 else None,
        "min_tokens_pq_v2": min(v2_tokens) if v2_tokens else None,
        "max_tokens_pq_v2": max(v2_tokens) if v2_tokens else None,
    }

    out_table = RichTable(show_header=True, header_style="bold magenta",
                          title=f"Token summary — {schema_name} "
                                f"({naive_n_tables} tables, {_ENC.name})")
    out_table.add_column("Pipeline", style="bold")
    out_table.add_column("Avg tokens", justify="right")
    out_table.add_column("× vs naive", justify="right")
    out_table.add_row("naive", f"{avg_naive:,.0f}", "1.00×")
    out_table.add_row("pq_v1 (TF-IDF + FK)", f"{avg_v1:,.0f}",
                      f"{avg_naive / avg_v1:.1f}× fewer" if avg_v1 else "-")
    out_table.add_row("pq_v2 (TF-IDF + selector + FK)", f"{avg_v2:,.0f}",
                      f"{avg_naive / avg_v2:.1f}× fewer" if avg_v2 else "-")
    console.print()
    console.print(out_table)

    args.out.write_text(json.dumps({"summary": summary, "raw": raw}, indent=2))
    console.print(f"\n[dim]Token results written to {args.out}[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
