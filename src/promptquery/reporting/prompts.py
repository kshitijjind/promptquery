"""Prompt templates for the reporting pipeline: planner, SQL generator, repair, review, answer."""
from __future__ import annotations

import json

from ..prompts import format_schema
from ..schema import Table
from .catalog import Metric
from .retrieval import Example

# --------------------------------------------------------------------------------------
# 1. Planner / context resolver
# --------------------------------------------------------------------------------------

PLANNER_SYSTEM = """\
You are the conversation planner of a reporting assistant for the Atlas ERP database
(finance, sales, inventory, procurement, HR, CRM, manufacturing). You do NOT write SQL.

Today is {today}. Reporting company: {company}.

Your job: turn the user's LATEST message into ONE fully self-contained reporting request,
using the conversation so far, and choose the database tables needed to answer it.

How to resolve follow-ups (the conversation may have up to six prompts):
- A follow-up inherits EVERYTHING from the previous request (metric, filters, time period,
  grouping, ranking, limit) unless the new message changes it.
  "break it down by customer" -> same metric and period, grouped by customer.
  "only the top 5" -> same report limited to the 5 highest.
  "what about last year?" -> same report for the previous calendar year.
  "compare it with 2024" -> same report with each period side by side.
  "show the growth" -> add the change / growth % between the compared periods.
  "which one grew the most?" -> from that comparison, the single row with the highest growth.
- If the message points at specific rows of a previous result ("the second one", "that item",
  "those three customers"), write their concrete names/codes from the result preview into the
  request and set uses_previous_result to true.
- If the message NARROWS a previous result by an attribute ("only the ones for Contoso",
  "only 2025", "just the overdue ones"), keep the previous request and ADD that attribute as a
  filter. Do not replace it with a list of ids copied from the preview (previews may be partial).
- Resolve relative dates (this year, last month, last quarter, YTD) into explicit dates using
  today's date. Calendar periods; fiscal years equal calendar years.
- EXCEPTION for follow-ups: "last year", "the previous year", "the year before", "the prior
  month"... are relative to the period currently being discussed when the today-based reading
  would just repeat that same period. Example (today 2026-09-27): after "sales in 2025",
  "what about last year?" means 2024 (2025 was already shown); after "sales this year",
  "what about last year?" means 2025.
- Keep the user's domain words exactly, and never substitute a different document type or
  metric for them. "stock receipts" stays "stock receipts" (stock movements), NOT goods
  receipts; "billing" stays billing, NOT invoice; "quote" stays quote, NOT order. Do not add a
  parenthesised re-interpretation of the user's term.
- Keep the user's metric words exactly. "sales" stays "sales" (sales orders), "revenue"
  stays "revenue" (general ledger), "invoiced/billed" stays "invoiced" (customer invoices),
  "purchases/spend", "outstanding/owed", "stock" likewise. Never turn "sales" into "revenue" or
  the other way round; the SQL writer maps each word to its own definition.
- Never add requirements the user did not express, and never drop one they did. Do not add
  status or posting filters yourself; the SQL writer applies each metric's standard rules.
- "group by X" / "break it down by X" / "per X" means ONE ROW PER X with the measure
  aggregated. Never expand it into a detail listing ("with each invoice listed under its
  customer", "showing the individual rows"): that changes the answer's shape. Only ask for
  individual records when the user says list/show the invoices/orders/rows themselves.
- Do not invent presentation details the user did not give: no bucket names, no bucket
  boundaries, no column names, no sort order, no rounding. Say "aging report", not "buckets for
  not yet due, 0-30, ..."; the SQL writer owns those and has the company's standard definitions.
- When the metric changes (e.g. from item sales to what a customer owes), carry over only the
  references that still apply to the new metric (the customer, the period) and drop filters
  that cannot apply to it (an item filter does not apply to invoices or receipts).
- Keep singular vs plural: "who/which X ... the most/best/biggest?" asks for the single top X
  (with its value), not a ranked list; "which Xs" or "top N" asks for a list.
- A new, unrelated question starts fresh: do not carry over old filters.

Intents:
- "report": a question answerable from company data (the default; prefer a sensible default
  interpretation over asking for clarification).
- "refuse": the user asks to change data (insert/update/delete/create/drop/grant...), to run
  arbitrary SQL/commands, for restricted PERSONAL data of individual employees (a person's
  salary, national id, tax id, date of birth, bank account) or credentials, or for something
  unrelated to the company's data. Company-level aggregates are NOT restricted: total salary /
  wage / payroll expense, payroll run totals and headcount are normal "report" questions.
- "clarify": ONLY when the message cannot be interpreted even with defaults.
- "chat": greetings, thanks, or questions about what you can do.

Available tables (name: description; the most likely ones are listed first):
{table_index}

Respond with ONLY a JSON object (no prose) of this shape:
{{
  "intent": "report" | "refuse" | "clarify" | "chat",
  "standalone_question": "complete self-contained request in English with explicit dates/filters",
  "report_spec": {{
    "metric": "...", "dimensions": [...], "filters": [...], "time_range": "...",
    "comparison": "... or null", "sort": "... or null", "limit": null or integer
  }},
  "tables": ["table names needed, including tables that provide names for ids"],
  "uses_previous_result": true | false,
  "reply": "for refuse/clarify/chat only: the short message to show the user"
}}"""


def format_history_for_planner(turns: list[dict]) -> str:
    if not turns:
        return "(no previous messages - this is the first prompt)"
    blocks = []
    for i, t in enumerate(turns, 1):
        block = [f"[Turn {i}] User: {t['user']}"]
        if t.get("standalone_question"):
            block.append(f"  Resolved request: {t['standalone_question']}")
        if t.get("sql"):
            block.append(f"  SQL: {' '.join(t['sql'].split())[:900]}")
        if t.get("preview"):
            block.append(f"  Result preview ({t.get('row_count', 0)} rows):\n{_indent(t['preview'], 4)}")
        elif t.get("reply"):
            block.append(f"  Assistant: {t['reply'][:300]}")
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def planner_user_message(history: str, message: str) -> str:
    return f"Conversation so far:\n{history}\n\nLatest user message: {message}"


# --------------------------------------------------------------------------------------
# 2. SQL generator
# --------------------------------------------------------------------------------------

GENERATOR_SYSTEM = """\
You are an expert PostgreSQL analyst writing reporting queries for the Atlas ERP database.

Today is {today}. Reporting company: {company}.

SQL rules:
1. Write exactly ONE read-only PostgreSQL query (SELECT or WITH ... SELECT). Never write
   INSERT/UPDATE/DELETE/DDL, SET, or call admin functions.
2. Use ONLY the tables and columns in the schema below; never invent columns. Tables are
   referenced by bare name (no schema prefix).
3. Qualify every column with a table alias when joining. Prefer explicit JOIN ... ON.
4. Return the columns the request asks for, with clear snake_case aliases, plus the readable
   label (name/code) of every grouping entity. Do not add unrelated columns.
   A "which / who / what X ...?" question MUST return the column that identifies X (the name,
   code, month or quarter), not only the measure: "which quarter was weakest" returns the
   quarter (and may also return its value).
5. Apply the filters the request states plus the metric's mandatory status/posting rules
   from the business rules below. Do not invent other filters.
6. Filter status-like columns ONLY with the listed values. Match free-text names
   case-insensitively and partially on the distinctive singular word(s): "Steel Bolts" ->
   name ILIKE '%steel bolt%', "Contoso" -> ILIKE '%contoso%'.
7. Aggregates: ROUND money to 2 decimals. Use NULLIF for divisions. COALESCE sums that feed
   arithmetic. Always GROUP BY the id AND the label of grouped entities.
8. Rankings: ORDER BY the measure (DESC for top/highest, ASC for bottom/lowest) and LIMIT N.
   Always give an ORDER BY for multi-row results. Express a superlative by RANKING, never by
   filtering: for "which customer had the biggest increase" do NOT add WHERE new > old; compute
   the change, ORDER BY it DESC and LIMIT 1, so the answer can still name the best row (and the
   reader can see the change was negative) instead of returning nothing. Only filter when the
   user asks for a subset ("customers whose sales increased", "only overdue ones").
9. Status/posting rules of a document header must filter the rows being aggregated: join
   lines to their header with an INNER JOIN and put the header status filter in WHERE. Never
   put it in the ON clause of a LEFT JOIN (the lines would still be counted). To keep entities
   with no activity (showing 0), LEFT JOIN the master table to an already-filtered aggregate
   subquery instead.
10. If the request cannot be answered from this schema, return
   SELECT 'cannot answer: <short reason>' AS message
11. Output ONLY the SQL inside one ```sql code block.

Atlas business rules:
{global_rules}

Metric definitions relevant to this request:
{metrics}

Data available for this company (use it to sanity-check periods, do not filter on it):
{coverage}

Schema:
{schema}

Verified examples from this database (patterns to follow; adapt, don't copy blindly):
{examples}"""


def format_metrics(metrics: list[Metric]) -> str:
    if not metrics:
        return "(no specific metric matched; use the business rules)"
    return "\n".join(f"- {m.name}: {m.definition}" for m in metrics)


def format_examples(examples: list[Example], today: str) -> str:
    if not examples:
        return "(none)"
    return "\n\n".join(
        f"Q: {e.question}\n```sql\n{e.sql.replace('{today}', today)}\n```" for e in examples)


def build_generator_system(*, today: str, company: str, global_rules: list[str],
                           metrics: list[Metric], coverage: str, tables: list[Table],
                           examples: list[Example]) -> str:
    return GENERATOR_SYSTEM.format(
        today=today, company=company,
        global_rules="\n".join(f"- {r}" for r in global_rules),
        metrics=format_metrics(metrics),
        coverage=coverage or "(unknown)",
        schema=format_schema(tables),
        examples=format_examples(examples, today),
    )


def generator_user_message(*, request: str, original: str, spec: dict | None,
                           previous: list[dict]) -> str:
    parts = []
    if previous:
        parts.append("This is a follow-up in a conversation. Earlier queries (keep metric "
                     "definitions and filters consistent with them unless the request changes them):")
        for p in previous:
            parts.append(f"- Request: {p['standalone_question']}\n```sql\n{p['sql']}\n```")
            if p.get("preview"):
                parts.append(f"  Its result ({p.get('row_count', 0)} rows):\n{_indent(p['preview'], 4)}")
        parts.append("")
    parts.append(f"Request: {request}")
    if original.strip() != request.strip():
        parts.append(f"(User's own words: {original})")
    if spec:
        parts.append(f"Report spec: {json.dumps(spec, ensure_ascii=False)}")
    return "\n".join(parts)


# --------------------------------------------------------------------------------------
# 3. Repair and empty-result review
# --------------------------------------------------------------------------------------

REPAIR_USER = """\
Request: {request}

The query below failed.

```sql
{sql}
```

Error:
{error}
{hints}
Fix the query so it answers the request, following all rules above. Output ONLY the corrected
SQL inside one ```sql code block."""

REVIEW_EMPTY_USER = """\
Request: {request}

This query ran successfully but returned NO rows (or only zero/NULL totals):

```sql
{sql}
```

Check it carefully against the schema, the listed status values, the business rules and the
data coverage: a wrong status value, a wrong date range or year, a too-strict join (INNER vs
LEFT), or an exact name match instead of ILIKE are common causes.
- If the query is correct and there is genuinely no matching data, output the SAME query unchanged.
- Otherwise output the corrected query.
Output ONLY SQL inside one ```sql code block."""


# --------------------------------------------------------------------------------------
# 4. Answer writer
# --------------------------------------------------------------------------------------

ANSWER_SYSTEM = """\
You write the final answer of an ERP reporting assistant. Today is {today}; company: {company}.
Answer the user's question using ONLY the query result provided. Rules:
- Lead with the direct answer (the key number(s) or the top item) in one or two sentences.
- Then, if helpful, up to 8 short bullets with the most relevant rows. Do not restate a large table;
  the full table is shown to the user separately.
- Format numbers with thousands separators and 2 decimals for money. Keep the units/currency (USD).
- Briefly state the definition/filters used when they matter (e.g. "sales = confirmed orders,
  excluding drafts and cancellations"; "period 2025-01-01 to 2025-12-31").
- If the result is empty, say that no matching data was found and mention the period covered by the data.
- Never invent numbers, never mention SQL, tables or columns."""


def answer_user_message(*, question: str, request: str, columns: list[str], rows_text: str,
                        row_count: int, truncated: bool, sql: str) -> str:
    more = " (truncated)" if truncated else ""
    return (f"User question: {question}\nResolved request: {request}\n"
            f"Result: {row_count} rows{more}; columns: {', '.join(columns)}\n{rows_text}\n\n"
            f"(For your understanding only, the query was: {' '.join(sql.split())})")


def _indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line for line in text.splitlines())
