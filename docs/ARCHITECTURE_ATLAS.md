# Atlas AI Reporting Bot: Architecture

## 1. Problem and approach

The task is to turn **PromptQuery**, a stateless natural-language-to-SQL CLI, into a reporting
bot for the **Atlas ERP** PostgreSQL database. The bot has four requirements:

- answer business questions accurately,
- hold conversations of up to six dependent prompts,
- never put the data at risk,
- be built so that accuracy can be measured.

The main idea is simple. Accuracy on an ERP depends mostly on **business semantics**, not on
SQL syntax. For example, what counts as "sales"? How are credit notes signed? Which statuses
are "open"? And how do you stop one tenant from seeing another tenant's rows? So we:

- keep PromptQuery's good parts,
- add a database guard layer, a semantic layer, a conversation planner and an execution-checked
  repair loop,
- measure everything with an execution-accuracy harness.

## 2. Diagnosis of the prototype

| Area | What PromptQuery did | Problem for Atlas |
|---|---|---|
| Conversation | Every question was stateless. The REPL only recalled typed lines. There was no HTTP API. | Could not handle 6-prompt follow-ups. |
| Security | A sqlglot SELECT-only check, a read-only session, and a denylist of 8 functions. | `dblink('…','DELETE …')` opens a new session that is **not** read-only, so it bypasses the check. `pg_sleep`, `pg_read_file`, `SELECT INTO` and `FOR UPDATE` were not blocked. There was no row cap (`fetchall()`). |
| Multi-tenancy | Not handled. | Atlas enforces tenants only in its ORM and has **no RLS**. Raw SQL mixed all 8 tenants' data. |
| Semantics | Only the raw schema. | Atlas statuses are plain VARCHAR with no CHECK constraints. Credit notes are stored as positive rows in the invoice table. Customer references (`partner_id`) have no FK. GL signs are debit-positive. The LLM cannot see any of this. |
| Retrieval | TF-IDF, then an LLM selector (with Odoo examples hard-coded), then FK expansion. If nothing matched, 3 arbitrary tables were used. | No business vocabulary. Joins that have no FK are never found. |
| Repair | 1 round, and only on a DB error. | No hint about the real columns. Empty or wrong results were never reconsidered. |
| Evaluation | Tested an older pipeline. The "100 %/94 %" figures come from table-parsing only and never executed SQL. | No reliable accuracy signal. |
| Robustness | No retries. Schema was re-introspected on every start. Some models were rejected (`max_tokens`). | Flaky under rate limits, and slow. |

## 3. Architecture

```
User question
   │
   ▼
1 PLAN (LLM) + semantic layer ── history (≤6 turns: question, resolved request, SQL, result
   │                              preview) + table index → intent, standalone request, spec, tables
   ▼
2 SCHEMA / RETRIEVAL ────────── matched metrics + similar verified examples + chosen tables
   │                              + FK "name" tables (≤18 tables, never the full schema)
   ▼
3 GENERATE ONE SQL (LLM)
   ▼
4 STATIC SQL GUARD ──────────── single read-only statement, deny/allow-lists, table allow-list,
   │                              LIMIT injection            ─┐
   ▼                                                           │ any error → REPAIR (LLM, ≤3)
5 EXPLAIN ──────────────────── unknown columns/types caught before running            │
   ▼                                                           │
6 EXECUTE ─────────────────── atlas_reporting role, READ ONLY txn, tenant GUC,         │
   │                              timeout, capped fetch, ROLLBACK ─────────────────────┘
   ▼
7 RESULT VALIDATION ──────────── deterministic checks → confidence
   │   • needed repairs / empty-result review?     • empty or all-zero result?
   │   • status literals not in Atlas' value lists?  • status filter inside a LEFT JOIN's ON?
   │   • more rows than the requested top-N?         • matched metric's tables not used?
   │   • negative values in count columns?
   ▼
CONFIDENCE?
   ├── HIGH ──────────────────────────────────────────────► 8 ANSWER (fast LLM) ✅
   └── LOW → generate an independent SECOND SQL (steps 3-6)
             ├── results agree → "verified" ──────────────► 8 ANSWER ✅
             └── disagree → THIRD SQL tie-breaker → majority ► 8 ANSWER
Interfaces: `atlas-bot chat` (REPL), `atlas-bot ask q1 q2 …`, FastAPI `POST /chat` + web UI.
```

`ATLAS_CONSENSUS` controls how aggressive this is:

- `adaptive` (the default): what is drawn above.
- `always`: two SQL candidates in parallel on every turn, plus a tie-breaker when they disagree.
- `off`: a single SQL, with no verification.

Every turn reports `confidence` (`high` / `verified` / `majority` / `low`) and
`confidence_reasons`, both in the API and in the eval output.

**Code map.** All paths are under `src/promptquery/`.

- New code:
  - `reporting/engine.py`: the orchestrator, conversation state, and result validation /
    confidence gating (`validate_result`, `_solve`).
  - `reporting/prompts.py`: all prompt templates.
  - `reporting/retrieval.py`: retrieval.
  - `reporting/guard.py`: SQL validation.
  - `reporting/executor.py`: query execution.
  - `reporting/catalog.py`: schema catalog and cache.
  - `reporting/server.py` and `reporting/cli.py`: HTTP API and CLI.
  - `atlas/setup_reporting.py`: creates the DB guard rails.
  - `atlas/data/semantic.yaml`, `examples.yaml`, `enum_values.json`: Atlas knowledge, kept as data.
- Reused and hardened:
  - `safety.py`: larger denylist, `SELECT INTO` / lock detection.
  - `llm.py`: retries, rate-limit back-off, reasoning-model parameters, `OPENAI_BASE_URL`, a
    Claude-CLI provider, JSON extraction.
  - `schema.py` and `retrieval.py`: the TF-IDF retriever and the schema types.

## 4. Schema and table selection

1. **Reporting catalog.** Built once and cached in `.cache/atlas_catalog.json`.
   - The bot introspects 114 `rpt` views: 132 Atlas tables, minus secrets and infrastructure,
     minus masked columns.
   - Views have no FKs, so relationships are read from the base tables. The `tenant_id` half of
     Atlas' composite FKs is dropped.
   - Six *virtual FKs* are added for references that have no constraint, e.g.
     `fin_customer_invoices.partner_id → sales_customers.id`.
   - Status value lists come from Atlas' own StrEnums (`enum_values.json`, 78 columns).
   - Table and column descriptions come from `semantic.yaml`.
2. **Candidate ranking.** Four signals are combined:
   - TF-IDF over names, descriptions and column notes;
   - business-metric keywords ("owe" → receivables, "stock value" → inventory);
   - the closest verified examples;
   - tables used in the previous turn.
3. **Hierarchical selection.** The planner LLM sees only a one-line **index** of tables, with
   the most relevant listed first, and picks the tables it needs.
   - Full column detail is sent only for the chosen tables, the matched metrics' tables and the
     closest examples' tables.
   - One-hop FK neighbours that give readable names (customers, items, vendors, accounts,
     warehouses) are added, capped at 18 tables.
   - The full schema is never sent to the model.

## 5. Joins

- Join paths come from real FKs plus the virtual FKs, and are shown as `FK col -> table(col)` in
  the schema prompt.
- Plumbing columns (`document_id`) and document-registry FKs are hidden, so they do not
  distract the model.
- Canonical multi-hop paths are written down in the business rules. Examples:
  - document chain: quote → order → delivery → billing → invoice → receipt;
  - stock-move warehouse via `inv_bins`;
  - AR customer via `partner_id`.

## 6. SQL generation

- The generator prompt contains:
  - 10 SQL rules;
  - Atlas business rules, e.g. "sales" = committed orders; GL revenue sign flip; never filter
    `journal_entries.status`; credit notes subtracted; half-open date ranges; conditional
    aggregation for period comparisons;
  - the definitions of the matched metrics;
  - **data coverage** per company (row counts and date ranges), which helps sanity-check periods;
  - the chosen schema, with status value lists;
  - 3–4 similar verified examples.
- Answers are grounded in results, never invented.
- `ROUND(…, 2)`, readable labels and a deterministic `ORDER BY` are required.
- If the question cannot be answered, the model returns a typed `SELECT 'cannot answer: …'` message.

## 7. Validation and safeguards (defence in depth)

1. **Database layer** (`atlas-bot setup-db`, idempotent).
   - A `LOGIN` role `atlas_reporting` with `SELECT` on `rpt` only: no `public`, no catalogs of
     secrets.
   - It runs with `default_transaction_read_only`, `statement_timeout` and `search_path = rpt`.
   - Every view is `security_barrier` and filters on `tenant_id = ANY(current_setting('atlas.tenant_ids'))`.
   - If the setting is unset, the views return **zero rows**, so the system fails closed.
   - Masked HR columns (salary, national id, tax id, date of birth, bank account) and
     per-employee payroll lines do not exist in the views.
2. **Session layer.**
   - Each query runs on a fresh connection in a `READ ONLY` transaction and is always rolled back.
   - The tenant scope is set by the bot process with a bound parameter, never by generated SQL.
   - Rows are fetched with a cap (`fetchmany(max_rows+1)`, which also detects truncation).
3. **Static guard** (before anything executes).
   - Exactly one statement.
   - Root must be SELECT, UNION or WITH.
   - A deep scan rejects DML/DDL anywhere, including inside CTEs.
   - `SELECT INTO`, `FOR UPDATE` and `set_config`/`current_setting` are blocked.
   - Functions: `dblink*`, `lo_*`, `pg_sleep*`, `pg_read_file`, `query_to_xml`, … are denied,
     and any function sqlglot does not model must be on an allow-list.
   - Only known views may be referenced, and schema prefixes are rejected. Unknown tables get a
     "did you mean …" hint.
   - `LIMIT` is added automatically.
   - `EXPLAIN` runs before execution.
4. **Default-deny masking (schema evolution).** Table discovery is automatic, so `is_sensitive()`
   drops any column whose name suggests personal or secret data (`salary`, `iban`, `national_id`,
   `password`, `token`, `card_number`, …) from **every** view, unless the exact `table.column` is
   in a reviewed allow-list. A later Atlas migration adding such a column cannot leak it.
   `tests/test_schema_drift.py` fails if anything slips through.
5. **API layer.** `ATLAS_API_TOKEN` enables constant-time bearer-token auth on every endpoint
   except `/health`; `ATLAS_AUDIT_LOG` records one JSON line per turn (question, resolved
   request, SQL, confidence and reasons, timings, error — never result rows).
6. **Intent layer.** The planner refuses requests to change data, requests for individual
   compensation/PII, and off-topic requests without generating any SQL. Even if it were
   bypassed, layers 1–3 still hold.

## 8. Conversational context

- A `ReportingSession` keeps the last 6 turns. Each turn stores:
  - the user text,
  - the **resolved standalone request**,
  - the report spec (metric, dimensions, filters, time range, comparison, limit),
  - the tables,
  - the SQL,
  - a result preview.
- The planner rewrites each new message into a self-contained request. It follows explicit rules:
  - inherit metric, period, grouping and limit unless changed;
  - resolve "those customers" / "that month" into concrete values taken from the previous result;
  - resolve relative periods relative to the period under discussion ("last year" after "2025" = 2024);
  - keep the user's metric word ("sales" ≠ "revenue");
  - drop filters that cannot apply to a new metric;
  - "who … the most" means a single row.
- The generator also receives the previous two SQL statements and their results. This keeps
  metric definitions consistent, and it can reuse them as CTEs.
- Chat history is **never** passed to the model verbatim. Only the structured state goes in.
- The HTTP API keeps sessions keyed by `session_id` (TTL 6 h, LRU 500), with a per-session lock
  so turns stay sequential.

## 9. Result validation, confidence and error recovery

**Confidence gating.**

- After the first SQL executes, `validate_result()` runs cheap, deterministic checks. There is
  no LLM call and no gold answer involved; the list is in the diagram above.
- If none of them fire, confidence is **HIGH** and the answer is returned: one SQL, the fastest
  path.
- If any of them fire, confidence is **LOW**, and an independent second SQL is generated and
  executed.
  - If the two results agree (same row count, and the narrower result's columns appear with the
    same values in the wider one), the answer is **verified**.
  - If not, a third SQL breaks the tie by majority.
- This spends extra LLM calls only on doubtful answers. It targets both run-to-run variance and
  real SQL bugs, e.g. a `LEFT JOIN … ON … AND status NOT IN (…)` that silently keeps draft and
  cancelled lines.

**Repair.**

- Guard rejections, `EXPLAIN` errors and runtime errors are all fed back to the model. The
  prompt includes the failing SQL, the exact error and the real column list of the tables
  involved. There are up to 3 rounds (`ATLAS_MAX_REPAIR`).
- An empty result triggers one self-review, which checks status values, date ranges, join types
  and ILIKE matching. The model either confirms the query or fixes it, and the fix is kept only
  if it returns rows.
- LLM calls retry with back-off and honour "try again in Xs" rate-limit hints.
- If the planner's output does not parse, the raw question is used instead.

## 10. Evaluation methodology and results

See [TESTING.md](TESTING.md). In short:

- The harness measures **execution accuracy**: it runs the bot's SQL and hand-verified gold SQL
  against the live database and compares the result sets. Column order, column names and extra
  columns are ignored, and there is a 0.01 numeric tolerance.
- There are three sets:
  - a **dev** set: 62 single-turn questions, 10 conversations of 4–6 turns, and 12 security cases;
  - a **hold-out** set: 21 single-turn questions and 4 conversations, written after tuning and
    never used for it;
  - a pytest suite: 115 tests, including live-DB security tests.
- The questions span 5 tenants.

| Set | Result |
|---|---|
| Hold-out A + B, **blind first runs** | 64/67 turns = **95.5 %** |
| Hold-out A, final code, 3 runs | 37/37 = **100 %** every run (4/4 conversations) |
| Hold-out B, final code, 3 runs | 29/30 = **96.7 %** every run (the miss is an ambiguous "list all accounts") |
| Dev (64 single-turn + 51 conversation turns + 12 security), 3 runs | **99.2 % / 99.2 % / 100 %** |
| Security scenarios | **12/12** (DB fingerprint unchanged, no leaked columns) |
| Original PromptQuery pipeline, same 64 questions | 28/64 = 43.8 % |
| Confidence gate cost | 1.00–1.17 SQL per turn; 0 turns left unresolved at LOW confidence |

## 11. Limitations

See [ASSUMPTIONS_AND_LIMITATIONS.md](ASSUMPTIONS_AND_LIMITATIONS.md).
