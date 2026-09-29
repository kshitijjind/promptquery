"""Retrieval-quality eval for PromptQuery.

Runs every question in the suite through the schema retriever (with FK
expansion) and reports recall@K + MRR per question and overall.

Usage:
    python -m eval.retrieval                  # default: shop suite, K=20
    python -m eval.retrieval --top-k 10
    python -m eval.retrieval --max-tables 20
    python -m eval.retrieval --quiet          # only the summary

No database connection is needed. No LLM API key is needed. The schema
fixture is loaded from JSON committed to the repo. The eval is fast,
deterministic, and free.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table as RichTable

from promptquery.retrieval import TfIdfRetriever, expand_via_fks
from promptquery.schema import Schema

from .questions.shop import QUESTIONS, Question

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_FIXTURE = EVAL_DIR / "fixtures" / "shop.schema.json"


# -- scoring ---------------------------------------------------------------

@dataclass
class QuestionResult:
    question: Question
    retrieved: list[str]        # qualified names in retrieval order (post-rank, pre-FK-expand)
    after_fk: list[str]         # qualified names after FK expansion (what the LLM would see)
    elapsed_ms: float

    @property
    def must_retrieve(self) -> set[str]:
        return set(self.question.must_retrieve)

    @property
    def covered(self) -> set[str]:
        return self.must_retrieve & set(self.after_fk)

    @property
    def missing(self) -> set[str]:
        return self.must_retrieve - set(self.after_fk)

    @property
    def passed(self) -> bool:
        return not self.missing

    @property
    def first_hit_rank(self) -> int | None:
        """1-based rank of the first must-retrieve table in the ranked list."""
        for i, name in enumerate(self.retrieved, start=1):
            if name in self.must_retrieve:
                return i
        return None

    def recall_at(self, k: int) -> float:
        hits = sum(1 for name in self.retrieved[:k] if name in self.must_retrieve)
        return hits / max(len(self.must_retrieve), 1)


@dataclass
class SuiteResult:
    results: list[QuestionResult]
    top_k: int
    max_tables: int

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def pass_rate(self) -> float:
        return self.pass_count / max(self.total, 1)

    def mean_recall_at(self, k: int) -> float:
        return statistics.mean(r.recall_at(k) for r in self.results) if self.results else 0.0

    @property
    def mrr(self) -> float:
        """Mean Reciprocal Rank of the FIRST must-retrieve table per question."""
        if not self.results:
            return 0.0
        rrs = [1.0 / r.first_hit_rank if r.first_hit_rank else 0.0 for r in self.results]
        return statistics.mean(rrs)

    @property
    def median_elapsed_ms(self) -> float:
        return statistics.median(r.elapsed_ms for r in self.results) if self.results else 0.0


# -- runner ----------------------------------------------------------------

def load_schema(path: Path) -> Schema:
    with open(path) as f:
        data = json.load(f)
    # Strip metadata keys before deserializing
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    return Schema.from_dict(data)


def run(
    questions: list[Question],
    schema: Schema,
    top_k: int = 10,
    max_tables: int = 20,
) -> SuiteResult:
    retriever = TfIdfRetriever(schema)
    results: list[QuestionResult] = []
    for q in questions:
        t0 = time.perf_counter()
        ranked = retriever.rank(q.text, top_k=top_k)
        seed = [t for t, score in ranked if score > 0] or [t for t, _ in ranked[:3]]
        expanded = expand_via_fks(schema, seed, max_total=max_tables)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        results.append(QuestionResult(
            question=q,
            retrieved=[t.qualified_name for t, _ in ranked],
            after_fk=[t.qualified_name for t in expanded],
            elapsed_ms=elapsed_ms,
        ))
    return SuiteResult(results=results, top_k=top_k, max_tables=max_tables)


# -- reporting -------------------------------------------------------------

def render(suite: SuiteResult, console: Console, quiet: bool = False) -> None:
    if not quiet:
        per_q = RichTable(show_header=True, header_style="bold cyan", title="Per-question results")
        per_q.add_column("#", style="dim", width=3)
        per_q.add_column("Question", overflow="fold", max_width=46)
        per_q.add_column("Diff", width=6)
        per_q.add_column("Need", overflow="fold", max_width=22)
        per_q.add_column("First hit", width=10, justify="right")
        per_q.add_column("R@5", width=5, justify="right")
        per_q.add_column("R@10", width=6, justify="right")
        per_q.add_column("Pass", width=4, justify="center")

        for i, r in enumerate(suite.results, start=1):
            need = ", ".join(sorted(r.must_retrieve))
            if r.missing:
                need = f"[red]{need}[/red]\n[dim]missing: {', '.join(sorted(r.missing))}[/dim]"
            first = str(r.first_hit_rank) if r.first_hit_rank else "[red]—[/red]"
            mark = "[green]✓[/green]" if r.passed else "[red]✗[/red]"
            per_q.add_row(
                str(i),
                r.question.text,
                r.question.difficulty,
                need,
                first,
                f"{r.recall_at(5):.2f}",
                f"{r.recall_at(10):.2f}",
                mark,
            )
        console.print(per_q)

    summary = RichTable(show_header=True, header_style="bold magenta", title="Summary")
    summary.add_column("Metric", style="bold")
    summary.add_column("Value", justify="right")
    summary.add_row("Suite", "shop (14 tables)")
    summary.add_row("Questions", str(suite.total))
    summary.add_row("Pass rate (must-retrieve fully covered)",
                    f"{suite.pass_count}/{suite.total}  ({100*suite.pass_rate:.1f}%)")
    summary.add_row("Mean recall@5", f"{suite.mean_recall_at(5):.3f}")
    summary.add_row("Mean recall@10", f"{suite.mean_recall_at(10):.3f}")
    summary.add_row("Mean recall@20", f"{suite.mean_recall_at(20):.3f}")
    summary.add_row("Mean Reciprocal Rank", f"{suite.mrr:.3f}")
    summary.add_row("Median latency / question", f"{suite.median_elapsed_ms:.2f} ms")
    summary.add_row("Retriever top-k / FK cap", f"{suite.top_k} / {suite.max_tables}")
    console.print(summary)


# -- breakdown by difficulty / tag -----------------------------------------

def render_breakdown(suite: SuiteResult, console: Console) -> None:
    diffs: dict[str, list[QuestionResult]] = {}
    for r in suite.results:
        diffs.setdefault(r.question.difficulty, []).append(r)

    by_diff = RichTable(show_header=True, header_style="bold yellow", title="By difficulty")
    by_diff.add_column("Difficulty")
    by_diff.add_column("Pass", justify="right")
    by_diff.add_column("Mean R@10", justify="right")
    for level in ("easy", "medium", "hard"):
        rs = diffs.get(level, [])
        if not rs:
            continue
        passes = sum(1 for r in rs if r.passed)
        mean_r10 = statistics.mean(r.recall_at(10) for r in rs)
        by_diff.add_row(level, f"{passes}/{len(rs)}", f"{mean_r10:.3f}")
    console.print(by_diff)


# -- main ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.retrieval",
        description="PromptQuery retrieval-quality eval.",
    )
    parser.add_argument("--top-k", type=int, default=10,
                        help="Top-K used by the retriever (default 10).")
    parser.add_argument("--max-tables", type=int, default=20,
                        help="FK-expansion cap (default 20).")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE,
                        help="Schema fixture JSON (default eval/fixtures/shop.schema.json).")
    parser.add_argument("--quiet", action="store_true",
                        help="Only print the summary, not the per-question table.")
    parser.add_argument("--fail-under", type=float, default=None,
                        help="Exit non-zero if pass rate is below this value (0-1).")
    args = parser.parse_args()

    console = Console()
    schema = load_schema(args.fixture)
    suite = run(QUESTIONS, schema, top_k=args.top_k, max_tables=args.max_tables)

    render(suite, console, quiet=args.quiet)
    render_breakdown(suite, console)

    if args.fail_under is not None and suite.pass_rate < args.fail_under:
        console.print(
            f"[red]✗ pass rate {suite.pass_rate:.3f} < threshold {args.fail_under:.3f}[/red]"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
