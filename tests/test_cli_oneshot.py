"""Tests for the CLI's --query (one-shot) mode and --out formats.

We unit-test the pieces we own (format_rows, run_question's outcome logic)
rather than spawning a real CLI with a real database. The end-to-end
"does the CLI actually run" smoke test is covered by `promptquery --help`
and by the eval suite hitting the same code paths.
"""
from __future__ import annotations

import json

import pytest

from promptquery.cli import (
    OutputFormat,
    Outcome,
    format_rows,
    main,
    run_question,
)
from promptquery.schema import Column, ForeignKey, Schema, Table
from promptquery.retrieval import TfIdfRetriever


# -- format_rows ----------------------------------------------------------

def test_format_rows_json_basic():
    out = format_rows(
        cols=["id", "name"],
        rows=[(1, "Marco"), (2, "Lucia")],
        fmt=OutputFormat.JSON,
    )
    parsed = json.loads(out)
    assert parsed == [{"id": 1, "name": "Marco"}, {"id": 2, "name": "Lucia"}]


def test_format_rows_json_handles_none_and_bytes():
    out = format_rows(
        cols=["a", "b", "c"],
        rows=[(None, b"abc", 3.14)],
        fmt=OutputFormat.JSON,
    )
    parsed = json.loads(out)
    assert parsed[0]["a"] is None
    assert parsed[0]["b"] == "<3 bytes>"
    assert parsed[0]["c"] == 3.14


def test_format_rows_csv_basic():
    out = format_rows(
        cols=["id", "name"],
        rows=[(1, "Marco"), (2, "Lucia, the bold")],
        fmt=OutputFormat.CSV,
    )
    lines = out.splitlines()
    assert lines[0] == "id,name"
    assert lines[1] == "1,Marco"
    # CSV quoting handles the comma in the value:
    assert lines[2] == '2,"Lucia, the bold"'


def test_format_rows_csv_handles_none():
    # Multi-column: NULL renders as an unquoted empty field. (Single-column rows
    # of all-NULL get quoted to disambiguate from an empty line — csv quirk.)
    out = format_rows(cols=["id", "name"], rows=[(1, None)], fmt=OutputFormat.CSV)
    assert out.splitlines()[1] == "1,"


def test_format_rows_rejects_table_format():
    with pytest.raises(ValueError, match="render_results"):
        format_rows(cols=["x"], rows=[(1,)], fmt=OutputFormat.TABLE)


# -- run_question logic ---------------------------------------------------

def _example_schema() -> Schema:
    users = Table(
        schema="public", name="users",
        comment="end-user accounts",
        columns=[
            Column("id", "bigint", False, True),
            Column("email", "text", True, False),
        ],
    )
    orders = Table(
        schema="public", name="orders",
        columns=[
            Column("id", "bigint", False, True),
            Column("user_id", "bigint", False, False),
            Column("total", "numeric", False, False),
        ],
        foreign_keys=[ForeignKey("user_id", "public", "users", "id")],
    )
    return Schema(tables=[users, orders])


class _FakeLLM:
    name = "fake"
    model = "fake-1"

    def __init__(self, response: str):
        self._response = response

    def generate(self, system: str, user: str) -> str:
        return self._response


class _FakeDB:
    """In-memory stand-in for promptquery.db.Database — only execute() is used."""
    def __init__(self, rows=None, exec_error=None):
        self._rows = rows or []
        self._exec_error = exec_error
        self.last_sql: str | None = None

    def execute(self, sql: str):
        self.last_sql = sql
        if self._exec_error:
            raise RuntimeError(self._exec_error)
        return (["count"], self._rows)


def test_run_question_happy_path_returns_ok():
    schema = _example_schema()
    retriever = TfIdfRetriever(schema)
    llm = _FakeLLM("```sql\nSELECT COUNT(*) FROM users\n```")
    db = _FakeDB(rows=[(5,)])

    result = run_question(
        question="how many users",
        schema=schema, retriever=retriever,
        llm=llm, selector_llm=None, db=db,
        top_k=10, select_n=15, max_tables=10,
        confirm=False,
    )
    assert result.outcome is Outcome.OK
    assert result.cols == ["count"]
    assert result.rows == [(5,)]
    assert db.last_sql == "SELECT COUNT(*) FROM users"


def test_run_question_safety_guard_rejects_dml():
    schema = _example_schema()
    retriever = TfIdfRetriever(schema)
    llm = _FakeLLM("```sql\nDELETE FROM users WHERE id = 1\n```")
    db = _FakeDB()

    result = run_question(
        question="delete everything",
        schema=schema, retriever=retriever,
        llm=llm, selector_llm=None, db=db,
        top_k=10, select_n=15, max_tables=10,
        confirm=False,
    )
    assert result.outcome is Outcome.UNSAFE
    assert db.last_sql is None  # never reached execute()


def test_run_question_empty_response():
    schema = _example_schema()
    retriever = TfIdfRetriever(schema)
    llm = _FakeLLM("")
    db = _FakeDB()
    result = run_question(
        question="anything", schema=schema, retriever=retriever,
        llm=llm, selector_llm=None, db=db,
        top_k=10, select_n=15, max_tables=10, confirm=False,
    )
    assert result.outcome is Outcome.EMPTY_SQL


def test_run_question_llm_exception():
    schema = _example_schema()
    retriever = TfIdfRetriever(schema)

    class _BoomLLM:
        name = "boom"; model = "x"
        def generate(self, system: str, user: str) -> str:
            raise RuntimeError("api down")

    result = run_question(
        question="anything", schema=schema, retriever=retriever,
        llm=_BoomLLM(), selector_llm=None, db=_FakeDB(),
        top_k=10, select_n=15, max_tables=10, confirm=False,
    )
    assert result.outcome is Outcome.LLM_ERROR
    assert "api down" in (result.error or "")


def test_run_question_exec_error_propagates_as_outcome():
    schema = _example_schema()
    retriever = TfIdfRetriever(schema)
    llm = _FakeLLM("```sql\nSELECT * FROM users\n```")
    db = _FakeDB(exec_error="relation users does not exist")

    result = run_question(
        question="anything", schema=schema, retriever=retriever,
        llm=llm, selector_llm=None, db=db,
        top_k=10, select_n=15, max_tables=10, confirm=False,
    )
    assert result.outcome is Outcome.EXEC_ERROR
    assert "relation users" in (result.error or "")


# -- CLI surface (just argv parsing — no real DB / LLM call) --------------

def test_cli_help_lists_query_flag():
    """`--query` / `-q` and `--out` show up in --help."""
    from click.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "--query" in result.output
    assert "--out" in result.output
    assert "json" in result.output and "csv" in result.output


def test_cli_rejects_invalid_out_format():
    from click.testing import CliRunner
    runner = CliRunner()
    # Use a fake DSN; we should fail at --out validation before reaching it.
    result = runner.invoke(main, [
        "--query", "x", "--out", "xml",
        "postgresql://x:y@localhost/db",
    ])
    assert result.exit_code != 0
    assert "xml" in result.output.lower() or "invalid value" in result.output.lower()
