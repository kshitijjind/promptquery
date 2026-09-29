"""Execution-guided repair: run the generated SQL and, if the database rejects it,
feed the database's own error back to the model for a bounded number of repair rounds.

Only hard execution errors trigger a repair. Empty results don't — an empty result is
often the correct answer, and "fixing" it risks replacing a right answer with a wrong one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from .llm import LLMClient, extract_sql
from .safety import UnsafeQuery, validate_select_only

if TYPE_CHECKING:
    from .db import Database


REPAIR_PROMPT = """\
The query below failed when run against the database. Fix it and return a corrected
query, following all the rules above (including the schema's enum value lists).

Question: {question}

Failed SQL:
{sql}

Database error:
{error}

Output ONLY the corrected SQL inside a ```sql code block."""


@dataclass
class RepairResult:
    sql: str                              # the SQL that was last attempted
    cols: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    error: str | None = None              # None = executed successfully
    attempts: int = 0                     # repair rounds actually used
    declined: bool = False                # user declined a repaired query (REPL confirm)


def execute_with_repair(
    db: "Database",
    llm: LLMClient,
    system_prompt: str,
    question: str,
    sql: str,
    *,
    max_repair: int = 1,
    confirm_cb: Callable[[str], bool] | None = None,
    progress_cb: Callable[[str], None] | None = None,
) -> RepairResult:
    """Execute `sql`; on a database error, ask the model to repair it (≤ max_repair rounds).

    Repaired SQL goes through validate_select_only before it is ever executed — the
    safety layer applies to every attempt, not just the first. If a repair fails safety,
    comes back empty, or the model errors, the original failure is returned unchanged.
    `confirm_cb` (REPL confirm mode) is asked before any repaired query runs.
    """
    attempts = 0
    current = sql
    while True:
        try:
            cols, rows = db.execute(current)
            return RepairResult(current, cols, rows, None, attempts)
        except Exception as e:  # psycopg errors carry the message we want to feed back
            error = str(e)

        if attempts >= max_repair:
            return RepairResult(current, error=error, attempts=attempts)
        attempts += 1
        if progress_cb:
            progress_cb(f"query failed ({error.splitlines()[0]}); repairing "
                        f"[{attempts}/{max_repair}]")

        try:
            raw = llm.generate(
                system_prompt,
                REPAIR_PROMPT.format(question=question, sql=current, error=error),
            )
        except Exception:
            return RepairResult(current, error=error, attempts=attempts)

        repaired = extract_sql(raw)
        if not repaired or repaired == current:
            return RepairResult(current, error=error, attempts=attempts)
        try:
            validate_select_only(repaired)
        except UnsafeQuery:
            return RepairResult(current, error=error, attempts=attempts)

        if confirm_cb is not None and not confirm_cb(repaired):
            return RepairResult(repaired, error=error, attempts=attempts, declined=True)
        current = repaired
