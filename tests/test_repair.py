"""Tests for the execution-guided repair loop (promptquery.repair)."""
from __future__ import annotations

from promptquery.repair import REPAIR_PROMPT, execute_with_repair


class _ScriptedLLM:
    """Returns canned responses in order; records every prompt it was sent."""
    name = "fake"
    model = "fake-1"

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if not self._responses:
            raise RuntimeError("no scripted responses left")
        return self._responses.pop(0)


class _FlakyDB:
    """Raises for SQL in `failing`, succeeds otherwise."""

    def __init__(self, failing: dict[str, str], rows=None):
        self._failing = failing
        self._rows = rows or [(1,)]
        self.executed: list[str] = []

    def execute(self, sql: str):
        self.executed.append(sql)
        if sql in self._failing:
            raise RuntimeError(self._failing[sql])
        return (["count"], self._rows)


def test_no_repair_needed_executes_once():
    db = _FlakyDB(failing={})
    llm = _ScriptedLLM([])
    result = execute_with_repair(db, llm, "sys", "q", "SELECT 1", max_repair=1)
    assert result.error is None
    assert result.attempts == 0
    assert result.sql == "SELECT 1"
    assert llm.calls == []  # no LLM round when the query just works


def test_repairs_on_db_error_and_succeeds():
    bad = "SELECT count(*) FROM t WHERE status = 'overdue'"
    good = "SELECT count(*) FROM t WHERE status = 'past_due'"
    db = _FlakyDB(failing={bad: 'invalid input value for enum invoice_status: "overdue"'})
    llm = _ScriptedLLM([f"```sql\n{good}\n```"])

    result = execute_with_repair(db, llm, "sys", "overdue invoices?", bad, max_repair=1)

    assert result.error is None
    assert result.attempts == 1
    assert result.sql == good
    assert db.executed == [bad, good]
    # The repair prompt carried the failed SQL and the DB's own error message.
    _, user = llm.calls[0]
    assert bad in user and "invalid input value" in user


def test_gives_up_after_max_repair():
    bad = "SELECT broken"
    db = _FlakyDB(failing={bad: "syntax error", "SELECT also_broken": "still broken"})
    llm = _ScriptedLLM(["```sql\nSELECT also_broken\n```"])

    result = execute_with_repair(db, llm, "sys", "q", bad, max_repair=1)

    assert result.error == "still broken"
    assert result.attempts == 1


def test_max_repair_zero_disables_repair():
    bad = "SELECT broken"
    db = _FlakyDB(failing={bad: "syntax error"})
    llm = _ScriptedLLM(["```sql\nSELECT 1\n```"])

    result = execute_with_repair(db, llm, "sys", "q", bad, max_repair=0)

    assert result.error == "syntax error"
    assert result.attempts == 0
    assert llm.calls == []


def test_unsafe_repair_is_never_executed():
    bad = "SELECT broken"
    db = _FlakyDB(failing={bad: "syntax error"})
    llm = _ScriptedLLM(["```sql\nDELETE FROM t\n```"])

    result = execute_with_repair(db, llm, "sys", "q", bad, max_repair=1)

    assert result.error == "syntax error"  # original failure, unchanged
    assert db.executed == [bad]            # the DELETE never reached the database


def test_identical_repair_bails_out():
    bad = "SELECT broken"
    db = _FlakyDB(failing={bad: "syntax error"})
    llm = _ScriptedLLM([f"```sql\n{bad}\n```"])

    result = execute_with_repair(db, llm, "sys", "q", bad, max_repair=3)

    assert result.error == "syntax error"
    assert db.executed == [bad]  # no point re-running the same statement


def test_confirm_callback_can_decline_repaired_sql():
    bad = "SELECT broken"
    good = "SELECT 1"
    db = _FlakyDB(failing={bad: "syntax error"})
    llm = _ScriptedLLM([f"```sql\n{good}\n```"])

    result = execute_with_repair(
        db, llm, "sys", "q", bad, max_repair=1, confirm_cb=lambda sql: False,
    )

    assert result.declined is True
    assert db.executed == [bad]  # declined repair never ran


def test_repair_prompt_mentions_enum_rule():
    # The repair message nudges the model back to the schema's enum lists — the
    # dominant hard-error class the loop exists to fix.
    assert "enum" in REPAIR_PROMPT
