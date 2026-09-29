from __future__ import annotations

import math
import re
from collections import Counter

from .schema import Schema, Table


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")
_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")

_STOPWORDS = {
    "the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "with",
    "by", "from", "is", "are", "was", "were", "be", "been", "being",
    "id", "ids", "uuid", "fk", "pk", "ref",
    "how", "what", "when", "where", "who", "why", "which", "many", "much",
    "do", "does", "did", "have", "has", "had", "can", "could", "should",
    "show", "list", "get", "find", "give", "tell", "me",
}


def _stem(word: str) -> str:
    """Light plural→singular normalisation so `leads` matches `lead` etc."""
    if len(word) > 5 and word.endswith("ies"):
        return word[:-3] + "y"          # companies -> company, categories -> category
    if len(word) > 4 and word.endswith("sses"):
        return word[:-2]                # addresses -> address
    if len(word) > 4 and word.endswith("xes"):
        return word[:-2]                # taxes -> tax
    if len(word) > 4 and word.endswith("ches"):
        return word[:-2]                # batches -> batch
    if len(word) > 4 and word.endswith("shes"):
        return word[:-2]                # wishes -> wish
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]                # leads -> lead, users -> user
    return word


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    expanded = _CAMEL_RE.sub(r"\1 \2", text).replace("_", " ").replace("-", " ")
    out: list[str] = []
    for raw in _WORD_RE.findall(expanded):
        t = raw.lower()
        if t in _STOPWORDS:
            continue
        out.append(_stem(t))
    return out


def _table_terms(table: Table) -> list[str]:
    terms: list[str] = []
    # Repeat name to weight it higher in TF
    terms.extend(tokenize(table.name) * 3)
    terms.extend(tokenize(table.schema))
    if table.comment:
        terms.extend(tokenize(table.comment) * 2)
    for col in table.columns:
        terms.extend(tokenize(col.name))
    for fk in table.foreign_keys:
        terms.extend(tokenize(fk.referenced_table))
    return terms


class TfIdfRetriever:
    def __init__(self, schema: Schema):
        self.schema = schema
        self.tables: list[Table] = list(schema.tables)
        self._docs: list[list[str]] = [_table_terms(t) for t in self.tables]
        self._n = max(len(self._docs), 1)

        df: Counter[str] = Counter()
        for doc in self._docs:
            for term in set(doc):
                df[term] += 1
        self._df = df

        self._vecs: list[dict[str, float]] = [self._tfidf(doc) for doc in self._docs]
        self._norms: list[float] = [_norm(v) for v in self._vecs]

    def _idf(self, term: str) -> float:
        return math.log((self._n + 1) / (self._df.get(term, 0) + 1)) + 1.0

    def _tfidf(self, doc: list[str]) -> dict[str, float]:
        tf = Counter(doc)
        return {term: count * self._idf(term) for term, count in tf.items()}

    def rank(self, query: str, top_k: int = 10) -> list[tuple[Table, float]]:
        q_tokens = tokenize(query)
        if not q_tokens or not self.tables:
            return [(t, 0.0) for t in self.tables[:top_k]]
        q_vec = self._tfidf(q_tokens)
        q_norm = _norm(q_vec)
        scored: list[tuple[Table, float]] = []
        for table, vec, norm in zip(self.tables, self._vecs, self._norms):
            dot = 0.0
            shorter, longer = (q_vec, vec) if len(q_vec) < len(vec) else (vec, q_vec)
            for term, weight in shorter.items():
                other = longer.get(term)
                if other is not None:
                    dot += weight * other
            score = dot / (q_norm * norm) if q_norm and norm else 0.0
            scored.append((table, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


def _norm(v: dict[str, float]) -> float:
    return math.sqrt(sum(x * x for x in v.values())) or 1.0


def _fk_target_key(fk, default_schema: str = "public") -> tuple[str, str]:
    return (fk.referenced_schema or default_schema, fk.referenced_table)


def expand_via_fks(
    schema: Schema,
    seed: list[Table],
    max_total: int = 20,
) -> list[Table]:
    """Add tables connected to `seed` via foreign keys until `max_total` is reached.

    Walks one hop outward (seed -> referenced) AND inward (referencing -> seed).
    Preserves seed order; new tables are appended in discovery order.
    """
    if max_total <= 0:
        return []

    by_key: dict[tuple[str, str], Table] = {(t.schema, t.name): t for t in schema.tables}
    selected: dict[tuple[str, str], Table] = {}
    for t in seed:
        if len(selected) >= max_total:
            return list(selected.values())
        selected[(t.schema, t.name)] = t

    # Outbound: from selected to their referenced tables
    for table in list(selected.values()):
        for fk in table.foreign_keys:
            if len(selected) >= max_total:
                return list(selected.values())
            key = _fk_target_key(fk)
            if key in by_key and key not in selected:
                selected[key] = by_key[key]

    # Inbound: tables referencing anything in selected
    seed_keys = {(t.schema, t.name) for t in seed}
    for table in schema.tables:
        if len(selected) >= max_total:
            break
        key = (table.schema, table.name)
        if key in selected:
            continue
        if any(_fk_target_key(fk) in seed_keys for fk in table.foreign_keys):
            selected[key] = table

    return list(selected.values())


# --- LLM-assisted table selector ------------------------------------------
# Second stage of v0.2 retrieval: TF-IDF gives us a wide candidate set
# (~50 tables), the LLM then reads the candidate list and the question and
# returns just the relevant tables. Handles semantic mismatch that TF-IDF
# misses — e.g. "invoice" ↔ Odoo's `account_move`, "shipment" ↔ `stock_picking`.


_SELECTOR_SYSTEM = """\
You select database tables relevant to a natural-language question.

Each candidate table is shown with up to five of its column names and any
table comment. Pick the tables that ANY correct SQL answer would need to
reference.

Consider semantic equivalents the table name doesn't spell out (for
example, in Odoo `account_move` holds invoices, `res_partner` holds
customers/vendors/contacts, `stock_picking` holds shipments).

Output ONLY a JSON array of table names, ordered by relevance, with up to
{max_select} items.

Example: ["sale_order", "res_partner", "product_product"]

Candidate tables:
{candidate_block}
"""

_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*?\]")


def _candidate_block(candidates: list[Table]) -> str:
    lines: list[str] = []
    for t in candidates:
        cols = ", ".join(c.name for c in t.columns[:5])
        more = f", +{len(t.columns) - 5}" if len(t.columns) > 5 else ""
        line = f"- {t.qualified_name}({cols}{more})"
        if t.comment:
            line += f"  -- {t.comment[:80]}"
        lines.append(line)
    return "\n".join(lines)


def llm_select_tables(
    question: str,
    candidates: list[Table],
    llm,
    max_select: int = 15,
) -> list[Table]:
    """Use the LLM to pick the relevant tables out of a TF-IDF candidate set.

    Falls back to the candidate prefix on any error (network, parse,
    schema-naming-mismatch) so the pipeline degrades gracefully into v1
    behavior rather than crashing.
    """
    if len(candidates) <= max_select:
        return candidates

    system = _SELECTOR_SYSTEM.format(
        max_select=max_select,
        candidate_block=_candidate_block(candidates),
    )

    try:
        response = llm.generate(system, question)
    except Exception:
        return candidates[:max_select]

    match = _JSON_ARRAY_RE.search(response)
    if not match:
        return candidates[:max_select]
    import json
    try:
        names = json.loads(match.group(0))
    except json.JSONDecodeError:
        return candidates[:max_select]
    if not isinstance(names, list):
        return candidates[:max_select]

    by_name: dict[str, Table] = {t.name.lower(): t for t in candidates}
    selected: list[Table] = []
    seen: set[str] = set()
    for n in names:
        if not isinstance(n, str):
            continue
        key = n.lower().rsplit(".", 1)[-1]
        if key in by_name and key not in seen:
            selected.append(by_name[key])
            seen.add(key)
        if len(selected) >= max_select:
            break

    return selected or candidates[:max_select]
