"""Schema retrieval for reporting questions.

Four signals are combined, then the planner LLM makes the final table choice from a compact
index (name + one-line description) instead of full schemas:

1. TF-IDF over table name, description, column names and column notes (PromptQuery's
   retriever, fed the semantic layer's descriptions).
2. Business-metric matching: keyword phrases from the semantic layer ("outstanding",
   "revenue", "stock value") pin the metric's canonical tables and its definition.
3. Example matching: the most similar verified question->SQL examples contribute their tables.
4. Conversation carry-over: tables used by the previous turn stay in play for follow-ups.

After the planner picks tables, `detail_tables` adds one-hop FK neighbours that supply
human-readable names (customer, item, vendor, warehouse, account...).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp

from ..atlas import DATA_DIR
from ..retrieval import TfIdfRetriever, tokenize
from ..schema import Schema, Table
from .catalog import Catalog, Metric

# Small master-data tables that give ids a readable name; pulled in via FKs.
NAME_TABLES = {
    "sales_customers", "inv_items", "proc_vendors", "inv_warehouses", "inv_bins",
    "fin_accounts", "hr_departments", "hr_positions", "hr_employees", "inv_item_categories",
    "sales_customer_groups", "fin_cost_centers", "fin_profit_centers", "ps_projects",
}


@dataclass
class Example:
    question: str
    sql: str
    tables: list[str] = field(default_factory=list)


def _sql_tables(sql: str) -> list[str]:
    try:
        tree = sqlglot.parse_one(sql.replace("{today}", "2000-01-01"), read="postgres")
    except Exception:
        return []
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    out: list[str] = []
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        if name and name not in ctes and name not in out:
            out.append(name)
    return out


def load_examples(path: Path | None = None) -> list[Example]:
    data = yaml.safe_load((path or DATA_DIR / "examples.yaml").read_text()) or []
    return [Example(e["q"], e["sql"].strip(), _sql_tables(e["sql"])) for e in data]


class _TextIndex:
    """Tiny TF-IDF cosine index over short documents (example questions)."""

    def __init__(self, docs: list[str]):
        self._docs = [tokenize(d) for d in docs]
        n = max(len(self._docs), 1)
        df: Counter[str] = Counter()
        for d in self._docs:
            df.update(set(d))
        self._idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
        self._vecs = [self._vec(d) for d in self._docs]

    def _vec(self, tokens: list[str]) -> dict[str, float]:
        tf = Counter(tokens)
        v = {t: c * self._idf.get(t, 1.0) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def rank(self, query: str) -> list[tuple[int, float]]:
        q = self._vec(tokenize(query))
        scores = [(i, sum(w * v.get(t, 0.0) for t, w in q.items())) for i, v in enumerate(self._vecs)]
        return sorted(scores, key=lambda x: x[1], reverse=True)


@dataclass
class RetrievalResult:
    ranked_tables: list[Table]          # every table, most relevant first (for the index)
    metrics: list[Metric]               # matched business metrics
    examples: list[Example]             # most similar verified examples
    suggested: list[str]                # tables strongly suggested by metrics/examples/history


def _normalise(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9&%]+", " ", text.lower()) + " "


class ReportRetriever:
    def __init__(self, catalog: Catalog, examples: list[Example] | None = None):
        self.catalog = catalog
        self.tfidf = TfIdfRetriever(catalog.schema)
        self.examples = examples if examples is not None else load_examples()
        self._example_index = _TextIndex([e.question for e in self.examples])

    def match_metrics(self, text: str, limit: int = 3) -> list[Metric]:
        norm = _normalise(text)
        stems = set(tokenize(text))
        scored: list[tuple[float, Metric]] = []
        for m in self.catalog.semantic.metrics.values():
            score = 0.0
            for kw in m.keywords:
                kw_norm = _normalise(kw).strip()
                if f" {kw_norm} " in norm:
                    score += 1.0 + 0.5 * kw_norm.count(" ")   # multi-word phrases weigh more
                elif " " not in kw_norm and tokenize(kw_norm) and tokenize(kw_norm)[0] in stems:
                    score += 0.8
            if score:
                scored.append((score, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:limit]]

    def retrieve(self, text: str, carry_tables: list[str] | None = None,
                 n_examples: int = 4) -> RetrievalResult:
        ranked = [t for t, _ in self.tfidf.rank(text, top_k=len(self.catalog.schema.tables))]
        metrics = self.match_metrics(text)
        examples = [self.examples[i] for i, s in self._example_index.rank(text)[:n_examples] if s > 0.05]

        suggested: list[str] = []
        for name in (carry_tables or []):
            if name not in suggested:
                suggested.append(name)
        for m in metrics:
            for name in m.tables:
                if name not in suggested:
                    suggested.append(name)
        for ex in examples[:2]:
            for name in ex.tables:
                if name not in suggested:
                    suggested.append(name)
        suggested = [s for s in suggested if self.catalog.table(s) is not None]

        # Suggested tables first in the index, then TF-IDF order.
        front = [self.catalog.table(s) for s in suggested]
        rest = [t for t in ranked if t.name not in set(suggested)]
        return RetrievalResult(front + rest, metrics, examples, suggested)

    def detail_tables(self, chosen: list[str], max_total: int = 18) -> list[Table]:
        """Chosen tables plus one-hop FK neighbours that are readable-name master tables."""
        out: list[Table] = []
        seen: set[str] = set()
        for name in chosen:
            t = self.catalog.table(name)
            if t is not None and t.name not in seen:
                out.append(t)
                seen.add(t.name)
        for t in list(out):
            for fk in t.foreign_keys:
                if len(out) >= max_total:
                    break
                ref = fk.referenced_table
                if ref in NAME_TABLES and ref not in seen and self.catalog.table(ref) is not None:
                    out.append(self.catalog.table(ref))
                    seen.add(ref)
        return out[:max_total]


def table_index(tables: list[Table], limit: int | None = None) -> str:
    """Compact one-line-per-table index for the planner."""
    lines = []
    for t in tables[:limit] if limit else tables:
        desc = (t.comment or "").split(". ")[0][:140]
        lines.append(f"- {t.name}: {desc}" if desc else f"- {t.name}")
    return "\n".join(lines)


def schema_subset(schema: Schema, names: list[str]) -> list[Table]:
    by = {t.name: t for t in schema.tables}
    return [by[n] for n in names if n in by]
