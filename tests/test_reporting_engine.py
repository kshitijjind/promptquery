"""Engine flow with a scripted fake LLM (live DB required, no LLM calls)."""
from __future__ import annotations

import json
import threading

import pytest

from promptquery.llm import LLMClient
from promptquery.reporting.config import Settings
from promptquery.reporting.engine import ReportingEngine

from .test_atlas_integration import SETTINGS, _reachable

pytestmark = pytest.mark.skipif(not _reachable(), reason="Atlas reporting DB not reachable")


class ScriptedLLM(LLMClient):
    """Returns queued responses in order and records every prompt."""
    name = "fake"
    model = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def generate(self, system: str, user: str) -> str:
        with self._lock:
            self.calls.append((system, user))
            return self.responses.pop(0) if self.responses else "OK"


def plan(q, tables, intent="report"):
    return json.dumps({"intent": intent, "standalone_question": q, "tables": tables,
                       "report_spec": {}, "uses_previous_result": False, "reply": "No."})


def engine_with(responses, tmp_path, mode="off") -> tuple[ReportingEngine, ScriptedLLM]:
    s = Settings(**{**SETTINGS.__dict__, "catalog_cache": tmp_path / "c.json", "write_answer": False,
                    "consensus_mode": mode})
    llm = ScriptedLLM(responses)
    return ReportingEngine(s, llm=llm, fast_llm=llm), llm


def test_happy_path_and_conversation_context(tmp_path):
    eng, llm = engine_with([
        plan("total sales", ["sales_orders"]),
        "```sql\nSELECT ROUND(SUM(total_amount),2) AS total FROM sales_orders\n```",
        plan("total sales by customer", ["sales_orders", "sales_customers"]),
        "```sql\nSELECT c.name, SUM(so.total_amount) t FROM sales_orders so JOIN sales_customers c "
        "ON c.id = so.customer_id GROUP BY c.name ORDER BY t DESC\n```",
    ], tmp_path)
    s = eng.session("acme")
    t1 = s.ask("total sales")
    assert t1.ok and t1.row_count == 1
    t2 = s.ask("by customer")
    assert t2.ok and t2.row_count == 3
    planner_prompt = llm.calls[2][1]
    assert "total sales" in planner_prompt and "SUM(total_amount)" in planner_prompt  # history passed
    gen_prompt = llm.calls[3][1]
    assert "follow-up" in gen_prompt  # previous SQL passed to the generator


def test_repair_loop_fixes_bad_column(tmp_path):
    eng, llm = engine_with([
        plan("customers", ["sales_customers"]),
        "```sql\nSELECT customer_name FROM sales_customers\n```",
        "```sql\nSELECT name FROM sales_customers ORDER BY name\n```",
    ], tmp_path)
    t = eng.session("acme").ask("list customers")
    assert t.ok and t.attempts == 1 and t.row_count == 3
    repair_prompt = llm.calls[2][1]
    assert "does not exist" in repair_prompt and "Columns of sales_customers" in repair_prompt


def test_guard_rejection_is_repaired_not_executed(tmp_path):
    eng, llm = engine_with([
        plan("customers", ["sales_customers"]),
        "```sql\nDELETE FROM sales_customers\n```",
        "```sql\nSELECT COUNT(*) FROM sales_customers\n```",
    ], tmp_path)
    t = eng.session("acme").ask("how many customers")
    assert t.ok and t.rows == [[3]]
    assert "unsafe" in llm.calls[2][1]


def test_refusal_intent_skips_sql(tmp_path):
    eng, llm = engine_with([plan("", [], intent="refuse")], tmp_path)
    t = eng.session("acme").ask("delete all customers")
    assert t.intent == "refuse" and t.sql is None and len(llm.calls) == 1


def test_gives_up_after_max_repairs(tmp_path):
    bad = "```sql\nSELECT nope FROM sales_customers\n```"
    eng, _ = engine_with([plan("x", ["sales_customers"])] + [bad] * 10, tmp_path)
    t = eng.session("acme").ask("x")
    assert not t.ok and t.attempts == eng.settings.max_repair


def test_conversation_window_is_bounded(tmp_path):
    responses = []
    for _ in range(8):
        responses += [plan("count", ["sales_customers"]), "```sql\nSELECT COUNT(*) FROM sales_customers\n```"]
    eng, _ = engine_with(responses, tmp_path)
    s = eng.session("acme")
    for _ in range(8):
        s.ask("count")
    assert len(s.turns) == eng.settings.max_turns


def test_consensus_majority_vote(tmp_path):
    a = "```sql\nSELECT COUNT(*) AS n FROM sales_customers\n```"
    b = "```sql\nSELECT COUNT(*) AS n FROM sales_orders\n```"
    eng, llm = engine_with([plan("how many customers", ["sales_customers"]), a, b, a], tmp_path,
                           mode="always")
    t = eng.session("acme").ask("how many customers")
    assert t.ok and t.rows == [[3]]          # 2 of 3 candidates agree on the customer count
    assert t.timings["candidates"] == 3


def test_consensus_agreement_needs_no_tie_breaker(tmp_path):
    a = "```sql\nSELECT COUNT(*) AS n FROM sales_customers\n```"
    a2 = "```sql\nSELECT COUNT(id) AS customers FROM sales_customers\n```"
    eng, llm = engine_with([plan("how many customers", ["sales_customers"]), a, a2], tmp_path,
                           mode="always")
    t = eng.session("acme").ask("how many customers")
    assert t.ok and t.timings["candidates"] == 2 and len(llm.calls) == 3


def test_vacuous_aggregate_triggers_review(tmp_path):
    eng, llm = engine_with([
        plan("stock of steel bolts", ["inv_item_valuations", "inv_items"]),
        "```sql\nSELECT COALESCE(SUM(iv.on_hand_qty),0) q FROM inv_item_valuations iv JOIN inv_items i "
        "ON i.id = iv.item_id WHERE i.name ILIKE '%Steel Bolts%'\n```",
        "```sql\nSELECT COALESCE(SUM(iv.on_hand_qty),0) q FROM inv_item_valuations iv JOIN inv_items i "
        "ON i.id = iv.item_id WHERE i.name ILIKE '%steel bolt%'\n```",
    ], tmp_path)
    t = eng.session("acme").ask("How many Steel Bolts do we have?")
    assert t.ok and t.rows[0][0] > 0 and "returned NO rows" in llm.calls[2][1]


def test_adaptive_high_confidence_uses_one_sql(tmp_path):
    eng, llm = engine_with([plan("how many customers", ["sales_customers"]),
                            "```sql\nSELECT COUNT(*) AS n FROM sales_customers\n```"], tmp_path, mode="adaptive")
    t = eng.session("acme").ask("how many customers")
    assert t.ok and t.confidence == "high" and t.timings["candidates"] == 1 and len(llm.calls) == 2


def test_adaptive_low_confidence_generates_verifying_sql(tmp_path):
    bad_status = "```sql\nSELECT COUNT(*) AS n FROM sales_orders WHERE status = 'OPEN'\n```"
    good = "```sql\nSELECT COUNT(*) AS n FROM sales_orders WHERE status IN ('CONFIRMED','PARTIALLY_DELIVERED')\n```"
    # candidate 1: invalid status literal -> 0 rows -> empty review returns the good query;
    # still LOW (needed a review) -> candidate 2 agrees -> "verified"
    eng, llm = engine_with([plan("open orders", ["sales_orders"]), bad_status, good, good],
                           tmp_path, mode="adaptive")
    t = eng.session("acme").ask("how many open orders")
    assert t.ok and t.rows == [[2]]
    assert t.confidence == "verified" and t.timings["candidates"] == 2
    assert any("review" in r for r in t.confidence_reasons)


def test_result_validation_flags_left_join_status_and_bad_literal():
    from promptquery.reporting.engine import validate_result
    from promptquery.reporting.executor import QueryOutput
    from promptquery.reporting.catalog import load_catalog
    from promptquery.reporting.executor import ReportingExecutor
    cat = load_catalog(ReportingExecutor(SETTINGS.reporting_dsn, "acme"), None)
    scratch = type("S", (), {"attempts": 0, "error": None})()
    sql = ("SELECT i.name, SUM(sol.ordered_quantity) q FROM inv_items i LEFT JOIN sales_order_lines sol "
           "ON sol.item_id = i.id LEFT JOIN sales_orders so ON so.id = sol.order_id "
           "AND so.status NOT IN ('DRAFT','CANCELED') GROUP BY i.name")
    reasons = validate_result((QueryOutput(["name", "q"], [("x", 1)], False), sql, scratch), {}, [], cat)
    assert any("LEFT JOIN" in r for r in reasons)
    assert any("CANCELED" in r for r in reasons)
    ok = validate_result((QueryOutput(["n"], [(3,)], False), "SELECT COUNT(*) n FROM sales_customers", scratch),
                         {}, [], cat)
    assert ok == []


def test_result_validation_catches_atlas_traps():
    from promptquery.reporting.catalog import load_catalog
    from promptquery.reporting.engine import validate_result
    from promptquery.reporting.executor import QueryOutput, ReportingExecutor

    cat = load_catalog(ReportingExecutor(SETTINGS.reporting_dsn, "acme"), None)
    scratch = type("S", (), {"attempts": 0, "error": None})()

    def reasons(sql, rows=((1,),), cols=("v",)):
        return validate_result((QueryOutput(list(cols), list(rows), False), sql, scratch), {}, [], cat)

    # filtering the journal header's status drops a reversed original but keeps its reversal
    assert any("fin_journal_entries.status" in r for r in reasons(
        "SELECT SUM(jl.functional_debit_amount) v FROM fin_journal_lines jl "
        "JOIN fin_journal_entries je ON je.id = jl.journal_entry_id "
        "WHERE jl.is_posted AND je.status = 'POSTED'"))

    # a filter inside a LEFT JOIN's ON clause nulls the join instead of removing rows
    assert any("LEFT JOIN" in r for r in reasons(
        "SELECT SUM(jl.functional_debit_amount) v FROM fin_accounts a "
        "LEFT JOIN fin_journal_lines jl ON jl.account_id = a.id AND jl.is_posted"))

    # a label column must not hide an all-zero measure row
    assert any("all-zero" in r for r in reasons(
        "SELECT a.name, SUM(jl.functional_debit_amount) v FROM fin_accounts a "
        "JOIN fin_journal_lines jl ON jl.account_id = a.id GROUP BY a.name",
        rows=[("2026-06-28", 0.0)], cols=("as_of", "balance")))


def test_result_validation_checks_granularity():
    from promptquery.reporting.catalog import load_catalog
    from promptquery.reporting.engine import validate_result
    from promptquery.reporting.executor import QueryOutput, ReportingExecutor

    cat = load_catalog(ReportingExecutor(SETTINGS.reporting_dsn, "acme"), None)
    scratch = type("S", (), {"attempts": 0, "error": None})()
    grouped_sql = ("SELECT c.name, SUM(so.total_amount) v FROM sales_orders so "
                   "JOIN sales_customers c ON c.id = so.customer_id GROUP BY c.name")

    def reasons(spec, cols, rows, sql=grouped_sql):
        return validate_result((QueryOutput(list(cols), list(rows), False), sql, scratch), spec, [], cat)

    # a request for one total must not come back as a breakdown
    assert any("single total" in r for r in reasons(
        {"dimensions": []}, ("item", "v"), [("a", 1.0), ("b", 2.0)]))
    # one dimension requested, but the label column repeats -> finer granularity than asked
    assert any("finer-grained" in r for r in reasons(
        {"dimensions": ["customer"]}, ("customer", "invoice", "v"),
        [("Contoso", "INV-1", 1.0), ("Contoso", "INV-2", 2.0)]))
    # the repeated column is not the FIRST one (real case: one row per invoice, customer repeats)
    assert any("finer-grained" in r for r in reasons(
        {"dimensions": ["customer"]}, ("invoice_number", "customer", "amount"),
        [("INV-1", "Contoso", 1.0), ("INV-2", "Contoso", 2.0), ("INV-3", "Northwind", 3.0)]))
    # a correct per-customer grouping is clean
    assert reasons({"dimensions": ["customer"]}, ("customer", "v"),
                   [("Contoso", 1.0), ("Northwind", 2.0)]) == []
    # a plain detail listing (no aggregate, no dimensions) is not flagged
    assert reasons({"dimensions": []}, ("invoice_number",), [("INV-1",), ("INV-2",)],
                   sql="SELECT invoice_number FROM fin_customer_invoices") == []


def test_audit_log_records_one_json_line_per_turn(tmp_path):
    log = tmp_path / "audit" / "turns.jsonl"
    s = Settings(**{**SETTINGS.__dict__, "catalog_cache": tmp_path / "c.json",
                    "write_answer": False, "consensus_mode": "off", "audit_log": log})
    llm = ScriptedLLM([plan("count customers", ["sales_customers"]),
                       "```sql\nSELECT COUNT(*) AS n FROM sales_customers\n```"])
    ReportingEngine(s, llm=llm, fast_llm=llm).session("acme").ask("how many customers")
    rec = json.loads(log.read_text().strip())
    assert rec["question"] == "how many customers" and rec["rows"] == 1
    assert rec["confidence"] and rec["tenants"] == "acme" and rec["sql"].startswith("SELECT")
    assert "seconds" in rec and rec["error"] is None
