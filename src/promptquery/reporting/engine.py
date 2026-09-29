"""Reporting engine: one conversation = one `ReportingSession`.

Per user message:
  plan (LLM: resolve follow-up into a standalone request, pick tables, detect intent)
  -> retrieve (metrics, examples, detail schema)
  -> generate SQL (LLM)
  -> guard (static safety + schema checks) -> EXPLAIN -> execute (read-only, tenant-scoped)
  -> repair on any failure (bounded), review once on an empty result
  -> answer (LLM, grounded on the rows)
  -> append the turn to the conversation state
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from dataclasses import asdict, dataclass, field
from typing import Callable

from ..llm import LLMClient, extract_json, extract_sql, make_client
from ..schema import Table
from . import prompts as P
from .catalog import Catalog, load_catalog
from .config import Settings
from .executor import QueryOutput, ReportingExecutor, to_jsonable
from .guard import GuardError, check_sql
from .retrieval import ReportRetriever, table_index

HIDDEN_COLUMNS = {"document_id"}

# (label, table, date column) used to describe what data exists for the company.
COVERAGE_SPECS = [
    ("sales orders", "sales_orders", "order_date"),
    ("customer invoices", "fin_customer_invoices", "invoice_date"),
    ("customer receipts", "fin_customer_receipts", "receipt_date"),
    ("GL journal lines (posted)", "fin_journal_lines", "posting_date"),
    ("purchase orders", "proc_purchase_orders", "order_date"),
    ("vendor bills", "fin_vendor_bills", "bill_date"),
    ("stock moves", "inv_stock_moves", "move_date"),
    ("deliveries", "sales_deliveries", "delivery_date"),
]
MASTER_COUNTS = [("customers", "sales_customers"), ("items", "inv_items"),
                 ("vendors", "proc_vendors"), ("warehouses", "inv_warehouses"),
                 ("employees", "hr_employees")]


@dataclass
class Turn:
    user: str
    intent: str = "report"
    standalone_question: str = ""
    report_spec: dict | None = None
    tables: list[str] = field(default_factory=list)
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    answer: str = ""
    error: str | None = None
    attempts: int = 0
    # Result validation outcome: "high" (returned directly), "verified" (low at first, then
    # confirmed by an independent second SQL), "majority" (tie-breaker vote) or "low".
    confidence: str = ""
    confidence_reasons: list[str] = field(default_factory=list)
    timings: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    def preview(self, n: int = 15) -> str:
        if not self.columns:
            return ""
        lines = [" | ".join(self.columns)]
        for r in self.rows[:n]:
            lines.append(" | ".join("" if v is None else str(v) for v in r))
        if self.row_count > n:
            lines.append(f"... ({self.row_count - n} more rows)")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d


def _compact(t: Table) -> Table:
    """Hide plumbing columns and document-registry FKs from prompts."""
    if t.name == "core_documents":
        return t
    return Table(
        schema=t.schema, name=t.name, comment=t.comment,
        columns=[c for c in t.columns if c.name not in HIDDEN_COLUMNS],
        foreign_keys=[fk for fk in t.foreign_keys
                      if fk.column not in HIDDEN_COLUMNS and fk.referenced_table != "core_documents"],
    )


def _format_rows(columns: list[str], rows: list[list], limit: int = 40) -> str:
    lines = [" | ".join(columns)]
    for r in rows[:limit]:
        lines.append(" | ".join("" if v is None else str(v) for v in r))
    if len(rows) > limit:
        lines.append(f"... {len(rows) - limit} more rows")
    return "\n".join(lines)


class ReportingEngine:
    """Shared, thread-safe-by-construction resources (catalog, retriever, LLM clients)."""

    def __init__(self, settings: Settings, llm: LLMClient | None = None,
                 fast_llm: LLMClient | None = None):
        self.settings = settings
        self.executor = ReportingExecutor(settings.reporting_dsn, settings.tenant,
                                          settings.statement_timeout_ms)
        self.catalog: Catalog = load_catalog(self.executor, settings.catalog_cache)
        self.retriever = ReportRetriever(self.catalog)
        self.llm = llm or make_client(settings.llm)
        self.fast_llm = fast_llm or (make_client(settings.fast_llm) if settings.fast_llm else self.llm)
        self._coverage_cache: dict[str, str] = {}

    def session(self, tenant: str | None = None) -> "ReportingSession":
        executor = self.executor
        if tenant and tenant != self.settings.tenant:
            executor = ReportingExecutor(self.settings.reporting_dsn, tenant,
                                         self.settings.statement_timeout_ms)
        return ReportingSession(self, executor)

    def coverage(self, executor: ReportingExecutor) -> str:
        key = executor.tenant_label
        if key in self._coverage_cache:
            return self._coverage_cache[key]
        parts = []
        selects = []
        for label, table, col in COVERAGE_SPECS:
            where = " WHERE is_posted" if table == "fin_journal_lines" else ""
            selects.append(f"SELECT '{label}', COUNT(*), MIN({col})::text, MAX({col})::text "
                           f"FROM {table}{where}")
        for label, table in MASTER_COUNTS:
            selects.append(f"SELECT '{label}', COUNT(*), NULL, NULL FROM {table}")
        try:
            rows = executor.scalar_rows(" UNION ALL ".join(selects))
        except Exception:
            rows = []
        for label, n, lo, hi in rows:
            if lo:
                parts.append(f"- {label}: {n} rows, dated {lo} to {hi}")
            elif hi is None and lo is None and label in dict(MASTER_COUNTS):
                parts.append(f"- {label}: {n}")
            else:
                parts.append(f"- {label}: {n} rows")
        text = "\n".join(parts)
        self._coverage_cache[key] = text
        return text


class ReportingSession:
    def __init__(self, engine: ReportingEngine, executor: ReportingExecutor):
        self.id = uuid.uuid4().hex[:12]
        self.engine = engine
        self.executor = executor
        self.turns: list[Turn] = []

    # -- public API ----------------------------------------------------------------------
    @property
    def company(self) -> str:
        return ", ".join(f"{t['name']} ({t['slug']})" for t in self.executor.tenants)

    def reset(self) -> None:
        self.turns.clear()

    def ask(self, message: str, *, write_answer: bool | None = None,
            progress: Callable[[str], None] | None = None) -> Turn:
        s = self.engine.settings
        today = s.current_date().isoformat()
        say = progress or (lambda _m: None)
        turn = Turn(user=message)
        t0 = time.perf_counter()
        history = self.turns[-(s.max_turns - 1):] if s.max_turns > 1 else []

        # 1. plan ------------------------------------------------------------------------
        prev_report = next((t for t in reversed(history) if t.intent == "report" and t.sql), None)
        seed_text = message + (" " + prev_report.standalone_question if prev_report else "")
        pre = self.engine.retriever.retrieve(seed_text, carry_tables=prev_report.tables if prev_report else None)
        say("planning")
        plan = self._plan(message, history, pre.ranked_tables, today)
        turn.timings["plan"] = round(time.perf_counter() - t0, 2)
        turn.intent = plan.get("intent", "report")
        turn.standalone_question = plan.get("standalone_question") or message
        turn.report_spec = plan.get("report_spec")
        if turn.intent != "report":
            turn.answer = plan.get("reply") or "I can only answer reporting questions about the company's data."
            self._remember(turn)
            return turn

        # 2. retrieve --------------------------------------------------------------------
        req = turn.standalone_question
        post = self.engine.retriever.retrieve(req, carry_tables=plan.get("tables") or [])
        chosen = [t for t in (plan.get("tables") or []) if self.engine.catalog.table(t)]
        for m in post.metrics[:2]:
            chosen += [n for n in m.tables if n not in chosen]
        if prev_report:
            chosen += [n for n in prev_report.tables if n not in chosen]
        for ex in post.examples[:2]:
            chosen += [n for n in ex.tables if n not in chosen]
        tables = [_compact(t) for t in self.engine.retriever.detail_tables(chosen, max_total=18)]
        turn.tables = [t.name for t in tables]

        system = P.build_generator_system(
            today=today, company=self.company,
            global_rules=self.engine.catalog.semantic.global_rules,
            metrics=post.metrics, coverage=self.engine.coverage(self.executor),
            tables=tables, examples=post.examples,
        )
        previous = [
            {"standalone_question": t.standalone_question, "sql": t.sql,
             "preview": t.preview(10), "row_count": t.row_count}
            for t in history if t.intent == "report" and t.sql and t.ok
        ][-2:]
        user = P.generator_user_message(request=req, original=message,
                                        spec=turn.report_spec, previous=previous)

        # 3. generate -> guard -> explain -> execute -> repair ----------------------------
        say("generating SQL")
        output, sql = self._solve(system, user, req, turn, say, metrics=post.metrics)
        turn.timings["execute"] = round(time.perf_counter() - t0, 2)
        if output is None and sql is None:
            turn.answer = "Sorry, the language model is unavailable right now."
            self._remember(turn)
            return turn

        if output is not None:
            turn.sql = sql
            turn.columns = output.columns
            turn.rows = [[to_jsonable(v) for v in r] for r in output.rows]
            turn.row_count = len(output.rows)
            turn.truncated = output.truncated
        else:
            turn.sql = sql

        # 4. answer ----------------------------------------------------------------------
        do_answer = s.write_answer if write_answer is None else write_answer
        if turn.ok and do_answer:
            say("writing answer")
            turn.answer = self._answer(message, turn, today)
        elif turn.ok:
            turn.answer = self._fallback_answer(turn)
        else:
            turn.answer = ("I couldn't produce a valid query for that request. "
                           f"Last error: {turn.error.splitlines()[0] if turn.error else 'unknown'}")
        turn.timings["total"] = round(time.perf_counter() - t0, 2)
        self._remember(turn)
        self._audit(turn)
        return turn

    # -- steps -------------------------------------------------------------------------
    def _plan(self, message: str, history: list[Turn], ranked: list[Table], today: str) -> dict:
        system = P.PLANNER_SYSTEM.format(today=today, company=self.company,
                                         table_index=table_index(ranked, limit=120))
        hist = P.format_history_for_planner([
            {"user": t.user, "standalone_question": t.standalone_question if t.intent == "report" else "",
             "sql": t.sql, "preview": t.preview(12) if t.sql else "", "row_count": t.row_count,
             "reply": t.answer}
            for t in history
        ])
        user = P.planner_user_message(hist, message)
        for _ in range(2):
            try:
                plan = extract_json(self.engine.llm.generate(system, user))
                if plan.get("intent") not in {"report", "refuse", "clarify", "chat"}:
                    plan["intent"] = "report"
                if not isinstance(plan.get("tables"), list):
                    plan["tables"] = []
                return plan
            except Exception:
                continue
        # Degrade gracefully: treat the raw message as the request.
        return {"intent": "report", "standalone_question": message, "tables": [], "report_spec": None}

    def _candidate(self, system: str, user: str, request: str,
                   say: Callable[[str], None]) -> tuple[QueryOutput | None, str | None, Turn]:
        """One independent attempt: generate SQL, then guard/execute/repair it."""
        scratch = Turn(user=request)
        try:
            sql = extract_sql(self.engine.llm.generate(system, user))
        except Exception as e:
            scratch.error = f"LLM error: {e}"
            return None, None, scratch
        output, sql = self._execute_with_repair(system, request, sql, scratch, say)
        return output, sql, scratch

    def _solve(self, system: str, user: str, request: str, turn: Turn,
               say: Callable[[str], None], metrics: list | None = None,
               ) -> tuple[QueryOutput | None, str | None]:
        """Confidence-gated generation.

        adaptive (default): generate ONE SQL -> guard -> EXPLAIN -> execute -> validate the
          result. HIGH confidence -> return it. LOW -> generate an independent second SQL; if
          both results agree -> "verified"; otherwise a third candidate breaks the tie.
        always: two candidates in parallel every time (+ tie-breaker on disagreement).
        off: a single candidate, no verification.
        """
        from concurrent.futures import ThreadPoolExecutor

        mode = self.engine.settings.consensus_mode
        spec = turn.report_spec or {}
        if mode == "always":
            with ThreadPoolExecutor(max_workers=2) as pool:
                cands = list(pool.map(lambda _: self._candidate(system, user, request, say), range(2)))
        else:
            cands = [self._candidate(system, user, request, say)]

        first = cands[0]
        reasons = validate_result(first, spec, metrics or [], self.engine.catalog, request)
        confidence = "high" if not reasons else "low"

        if mode == "adaptive" and reasons:
            say("low confidence (" + "; ".join(reasons)[:120] + "): generating a second SQL")
            cands.append(self._candidate(system, user, request, say))

        ok = [c for c in cands if c[0] is not None]
        chosen = first if first[0] is not None else (ok[0] if ok else first)
        if mode != "off" and len(cands) >= 2 and len(ok) >= 2:
            if _agree(ok[0][0], ok[1][0]):
                chosen = ok[0]
                confidence = "verified" if reasons else "high"
            else:
                say("candidates disagree: generating a tie-breaker")
                extra = self._candidate(system, user, request, say)
                cands.append(extra)
                chosen, confidence = ok[0], "low"
                if extra[0] is not None:
                    for c in ok:
                        if _agree(c[0], extra[0]):
                            chosen, confidence = c, "majority"
                            break
        elif ok and first[0] is None:
            chosen = ok[0]

        output, sql, scratch = chosen
        turn.attempts = scratch.attempts
        turn.error = None if output is not None else scratch.error
        turn.confidence = confidence
        turn.confidence_reasons = reasons
        turn.timings["candidates"] = len(cands)
        return output, sql

    def _execute_with_repair(self, system: str, request: str, sql: str, turn: Turn,
                             say: Callable[[str], None]) -> tuple[QueryOutput | None, str]:
        s = self.engine.settings
        reviewed_empty = False
        attempts = 0
        current = sql
        while True:
            error: str | None = None
            hints = ""
            output: QueryOutput | None = None
            try:
                if not current:
                    raise GuardError("the model returned no SQL")
                guarded = check_sql(current, self.engine.catalog, s.max_rows)
                self.executor.explain(guarded.sql)
                output = self.executor.run(guarded.sql, s.max_rows)
            except GuardError as e:
                error = str(e)
            except Exception as e:  # psycopg errors: undefined column, type mismatch, timeout...
                error = str(e).strip()
                hints = self._error_hints(error, current)

            if error is None and output is not None:
                if _vacuous(output) and not reviewed_empty and attempts < s.max_repair:
                    reviewed_empty = True
                    say("empty result: reviewing query")
                    try:
                        revised = extract_sql(self.engine.llm.generate(
                            system, P.REVIEW_EMPTY_USER.format(request=request, sql=current)))
                    except Exception:
                        revised = current
                    if revised and _norm_sql(revised) != _norm_sql(current):
                        attempts += 1
                        turn.attempts = attempts
                        try:
                            guarded = check_sql(revised, self.engine.catalog, s.max_rows)
                            self.executor.explain(guarded.sql)
                            out2 = self.executor.run(guarded.sql, s.max_rows)
                            if not _vacuous(out2):
                                return out2, revised
                        except Exception:
                            pass
                turn.error = None
                return output, current

            turn.error = error
            if attempts >= s.max_repair:
                return None, current
            attempts += 1
            turn.attempts = attempts
            say(f"repairing ({attempts}/{s.max_repair}): {error.splitlines()[0][:120]}")
            try:
                repaired = extract_sql(self.engine.llm.generate(system, P.REPAIR_USER.format(
                    request=request, sql=current, error=error, hints=hints)))
            except Exception:
                return None, current
            if not repaired:
                return None, current
            current = repaired

    def _error_hints(self, error: str, sql: str) -> str:
        """Add schema facts the DB error implies (e.g. the real columns of the tables involved)."""
        import re

        import sqlglot
        from sqlglot import exp

        m = re.search(r'column "?([\w.]+)"? does not exist', error)
        if not m:
            return ""
        qualifier = m.group(1).split(".")[0].lower() if "." in m.group(1) else None
        try:
            tree = sqlglot.parse_one(sql, read="postgres")
            refs = [(t.name.lower(), (t.alias or t.name).lower()) for t in tree.find_all(exp.Table)]
        except Exception:
            refs = []
        names = [name for name, alias in refs if qualifier is None or qualifier in (name, alias)]
        hints, seen = [], set()
        for n in names or [r[0] for r in refs]:
            t = self.engine.catalog.table(n)
            if t is not None and t.name not in seen:
                seen.add(t.name)
                hints.append(f"Columns of {t.name}: {', '.join(c.name for c in t.columns)}")
        return ("\nSchema facts:\n" + "\n".join(hints) + "\n") if hints else ""

    def _answer(self, question: str, turn: Turn, today: str) -> str:
        system = P.ANSWER_SYSTEM.format(today=today, company=self.company)
        user = P.answer_user_message(
            question=question, request=turn.standalone_question, columns=turn.columns,
            rows_text=_format_rows(turn.columns, turn.rows), row_count=turn.row_count,
            truncated=turn.truncated, sql=turn.sql or "")
        try:
            return self.engine.fast_llm.generate(system, user).strip()
        except Exception:
            return self._fallback_answer(turn)

    @staticmethod
    def _fallback_answer(turn: Turn) -> str:
        if not turn.rows:
            return "No matching data was found."
        if turn.row_count == 1 and len(turn.columns) <= 4:
            return "; ".join(f"{c}: {v}" for c, v in zip(turn.columns, turn.rows[0]))
        return f"{turn.row_count} rows returned."

    def _audit(self, turn: Turn) -> None:
        """Append one JSON line per turn: the observability hook (ATLAS_AUDIT_LOG).

        Records what was asked, what SQL ran, how confident the answer was and why, how long each
        stage took, and any error - but never the result rows, so the log does not become a
        second copy of the data.
        """
        path = self.engine.settings.audit_log
        if path is None:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "session": self.id, "turn": len(self.turns), "tenants": self.executor.tenant_label,
            "question": turn.user, "resolved": turn.standalone_question, "intent": turn.intent,
            "tables": turn.tables, "sql": " ".join((turn.sql or "").split()) or None,
            "rows": turn.row_count, "truncated": turn.truncated,
            "confidence": turn.confidence, "confidence_reasons": turn.confidence_reasons,
            "repairs": turn.attempts, "candidates": turn.timings.get("candidates"),
            "seconds": turn.timings.get("total"), "error": turn.error,
            "model": f"{self.engine.llm.name}/{getattr(self.engine.llm, 'model', '')}",
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            pass  # observability must never break a reply

    def _remember(self, turn: Turn) -> None:
        self.turns.append(turn)
        keep = max(self.engine.settings.max_turns, 1)
        if len(self.turns) > keep:
            self.turns = self.turns[-keep:]


def _norm_sql(sql: str) -> str:
    return " ".join(sql.lower().split())


def _vacuous(out: QueryOutput) -> bool:
    """No rows, or every numeric measure NULL/zero in every row: a SUM over a filter that
    matched nothing, a LEFT JOIN whose ON-clause filter nulled the join, or debits netted
    against credits on balanced journal entries. Label columns (text, dates) are ignored, so
    `('2026-06-28', 0.0)` still counts as vacuous."""
    if not out.rows:
        return True
    # Every numeric measure zero/NULL in EVERY row: e.g. debits netted against credits on
    # balanced journal entries, which always sums to 0.
    numeric_cols = [i for i, v in enumerate(out.rows[0])
                    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)]
    if not numeric_cols:
        return False
    return all(r[i] is None or r[i] == 0 for r in out.rows for i in numeric_cols)


def _norm_cell(v):
    if v is None:
        return None
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return round(float(v), 2)
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    return str(v).strip().lower()


def _agree(a: QueryOutput, b: QueryOutput) -> bool:
    """Do two results carry the same information? Row counts must match and every column of
    the narrower result must appear (same multiset of values) in the wider one."""
    if len(a.rows) != len(b.rows):
        return False
    if not a.rows:
        return True
    narrow, wide = (a, b) if len(a.columns) <= len(b.columns) else (b, a)

    def cols(o):
        return [sorted((_norm_cell(r[i]) for r in o.rows), key=lambda x: (x is None, str(x)))
                for i in range(len(o.columns))]

    wide_cols = cols(wide)
    used: set[int] = set()
    for col in cols(narrow):
        match = next((j for j, w in enumerate(wide_cols) if j not in used and _cells_eq(col, w)), None)
        if match is None:
            return False
        used.add(match)
    return True


def _cells_eq(x: list, y: list) -> bool:
    for p, q in zip(x, y):
        if isinstance(p, float) and isinstance(q, float):
            if abs(p - q) > max(0.011, 1e-6 * abs(q)):
                return False
        elif p != q:
            return False
    return True


# Column names whose literals must come from the catalog's value lists.
_ENUM_HINT = ("status", "stage", "type", "_mode", "method", "partner_type", "normal_balance")


# "grouped by customer", "per item", "by month", "breakdown by region", "each vendor"
_GROUPING_RE = re.compile(r"\b(?:group(?:ed)?\s+by|broken\s+down\s+by|breakdown\s+by|"
                          r"split\s+by|per|by\s+each|for\s+each|each)\b", re.I)


def validate_result(cand, spec: dict, metrics: list, catalog, request: str = "") -> list[str]:
    """Result validation for confidence gating. Returns the reasons confidence is LOW
    (an empty list means HIGH). Cheap, deterministic checks only - no LLM call."""
    import sqlglot
    from sqlglot import exp

    output, sql, scratch = cand
    if output is None:
        return ["no valid result: " + (scratch.error or "unknown error").splitlines()[0][:100]]
    reasons: list[str] = []
    if scratch.attempts:
        reasons.append(f"needed {scratch.attempts} repair/review round(s)")
    if _vacuous(output):
        reasons.append("empty or all-zero result")
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return reasons + ["SQL could not be re-parsed"]

    tables = [t.name.lower() for t in tree.find_all(exp.Table)]
    enum_by_col: dict[str, set[str]] = {}
    for name in tables:
        t = catalog.table(name)
        if t is None:
            continue
        for c in t.columns:
            if c.enum_values:
                enum_by_col.setdefault(c.name, set()).update(c.enum_values)

    # 1. literals compared with status-like columns must be legal values
    for node in tree.find_all(exp.EQ, exp.NEQ, exp.In):
        col = node.this if isinstance(node, exp.In) else node.left
        if not isinstance(col, exp.Column) or col.name not in enum_by_col:
            continue
        lits = node.expressions if isinstance(node, exp.In) else [node.right]
        for lit in lits:
            if isinstance(lit, exp.Literal) and lit.is_string and lit.this not in enum_by_col[col.name]:
                reasons.append(f"unknown value '{lit.this}' for {col.name}")

    # 2. a filter inside a LEFT JOIN's ON clause does not remove rows: it nulls the join,
    #    which keeps unwanted lines and turns sums into 0
    for join in tree.find_all(exp.Join):
        if (join.side or "").upper() != "LEFT" or join.args.get("on") is None:
            continue
        on = join.args["on"]
        for node in on.find_all(exp.EQ, exp.NEQ, exp.In, exp.GT, exp.LT, exp.GTE, exp.LTE):
            left = node.this if isinstance(node, exp.In) else node.left
            right = None if isinstance(node, exp.In) else node.right
            # a join predicate compares two columns; a FILTER compares a column to a literal
            if isinstance(left, exp.Column) and (isinstance(node, exp.In) or isinstance(right, exp.Literal)):
                reasons.append(f"filter on {left.name} inside a LEFT JOIN condition")
                break
        for col in on.find_all(exp.Column):
            if col.name == "is_posted":
                reasons.append("is_posted inside a LEFT JOIN condition")
                break

    # 2b. Atlas-specific: posting must come from fin_journal_lines.is_posted, never from the
    #     journal header's status (a reversed entry stays POSTED and its reversal offsets it)
    for node in tree.find_all(exp.EQ, exp.NEQ, exp.In):
        col = node.this if isinstance(node, exp.In) else node.left
        if (isinstance(col, exp.Column) and col.name == "status"
                and ("fin_journal_entries" in tables or (col.table or "").lower().startswith("je"))):
            reasons.append("filters fin_journal_entries.status (use fin_journal_lines.is_posted)")
            break

    # 3. result shape vs the request
    limit = spec.get("limit") if isinstance(spec, dict) else None
    if isinstance(limit, int) and limit > 0 and len(output.rows) > limit:
        reasons.append(f"{len(output.rows)} rows but the request asked for {limit}")

    # 4. the matched business metric's tables should be used
    if metrics:
        top = metrics[0]
        if top.tables and not set(top.tables) & set(tables):
            reasons.append(f"does not use the tables of metric '{top.name}'")

    # 5. granularity: the result must match the grouping the request asked for. A detail
    #    listing in place of a total, or a finer grouping than requested, is a common and
    #    otherwise invisible error (e.g. "what did we sell in total" answered per item).
    dims = spec.get("dimensions") if isinstance(spec, dict) else None
    dims = [d for d in (dims or []) if isinstance(d, str) and d.strip()]
    grouped = tree.find(exp.Group) is not None
    # The planner does not always fill `dimensions`, so also read the resolved request: asking to
    # group and getting an ungrouped detail listing is a wrong answer shape either way.
    wants_group = bool(dims) or bool(_GROUPING_RE.search(request))
    if wants_group and not grouped and len(output.rows) > 1:
        reasons.append("request asks for a per-group breakdown but the query does not aggregate "
                       f"(returned {len(output.rows)} detail rows)")
    elif not dims and grouped and len(output.rows) > 1:
        if not _GROUPING_RE.search(request):
            reasons.append(f"request asks for a single total but the result is grouped "
                           f"into {len(output.rows)} rows")
    elif len(dims) == 1 and len(output.rows) > 1:
        # One group per row is the whole point of a GROUP BY, so in a correctly grouped result
        # every label column is unique. ANY repeated label means the rows are finer-grained than
        # the requested grouping (e.g. one row per invoice when "by customer" was asked).
        for i, sample in enumerate(output.rows[0]):
            if isinstance(sample, (int, float, Decimal)) and not isinstance(sample, bool):
                continue
            values = [r[i] for r in output.rows]
            if len(set(values)) < len(values):
                reasons.append(f"grouped by '{dims[0]}' but column '{output.columns[i]}' "
                               f"repeats, so the result is finer-grained than requested")
                break

    # 6. counts can't be negative
    for i, col in enumerate(output.columns):
        if any(k in col.lower() for k in ("count", "orders", "invoices", "number")):
            if any(isinstance(r[i], (int, float, Decimal)) and r[i] < 0 for r in output.rows):
                reasons.append(f"negative values in count column '{col}'")
    return list(dict.fromkeys(reasons))
