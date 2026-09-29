# Project Walkthrough: Everything That Was Done, and Why

This is the "understand everything" document. It explains:

- the two repositories;
- what the prototype did and where it broke;
- every change that was made, with the file it lives in;
- how one question flows through the system;
- how the system was tested.

The companion documents are:

- `ARCHITECTURE_ATLAS.md`: the short formal architecture document for the submission.
- `TESTING.md`: numbers.
- `ASSUMPTIONS_AND_LIMITATIONS.md`

---

## Part 1: The challenge in one paragraph

We were given two public repos:

- **PromptQuery**: a prototype "ask your database in English" tool.
- **Atlas ERP**: an open-source ERP whose data lives in PostgreSQL.

The job:

- Update PromptQuery's architecture and code so it works as a **reporting bot on the Atlas
  database**.
- It must answer business questions accurately, and hold **multi-turn conversations of up to
  six prompts**, where later prompts depend on earlier prompts or their results.
- It must be **built for ≥ 95 % accuracy** on unseen questions.
- It must keep **safeguards** on database access and SQL.
- Deliverables: code, a brief architecture document, a README, assumptions/limitations, and a
  description of the testing.

---

## Part 2: The two repositories

### 2.1 Atlas ERP (`atlas-erp/`): the database we report on

- **Stack.** FastAPI + SQLAlchemy backend and a React frontend.
  - Database: **PostgreSQL 16**, created by 53 Alembic migrations.
  - Run with `docker compose`.
- **Size.** 132 tables in the `public` schema, one prefix per module:

| Prefix | Module | Examples |
|---|---|---|
| `fin_` | Finance | `fin_accounts` (chart of accounts), `fin_journal_entries` / `fin_journal_lines` (general ledger), `fin_customer_invoices` (AR), `fin_vendor_bills` (AP), receipts, payments, assets |
| `sales_` | Sales | `sales_customers`, `sales_quotes`, `sales_orders` / `_lines`, `sales_deliveries`, `sales_billings`, `sales_returns` |
| `inv_` | Inventory | `inv_items` (products), `inv_warehouses`, `inv_bins`, `inv_stock_moves`, `inv_item_valuations` (stock value) |
| `proc_` | Procurement | `proc_vendors`, `proc_purchase_orders` / `_lines`, `proc_goods_receipts` |
| `hr_`, `crm_`, `mfg_`, `ps_`, `qm_`, `pm_`, `hsp_` | HR, CRM, manufacturing, projects, quality, maintenance, hospitality | … |
| `core_`, `adm_` | Users, roles, API keys, audit log, tenants | secrets: never reportable |

**Things about Atlas that make natural-language-to-SQL hard.** These drive the whole design:

1. **Multi-tenant without database protection.**
   - Every table has `tenant_id`. The demo seed creates 7 companies, and we added an 8th
     "volume" company.
   - Tenant filtering happens only inside Atlas' Python ORM. There is **no row-level security**,
     so a raw SQL query sees every company's data mixed together.
2. **Status columns are plain text.**
   - Values like `DRAFT`, `POSTED`, `PARTIALLY_PAID` or `CREDIT_BLOCKED` have no database
     constraint, so the allowed values are invisible in the schema.
   - They exist only in Python enums.
3. **Business rules live in code.** Some examples:
   - "Revenue" comes from the general ledger, and its sign must be flipped (ledger amounts are
     debit-positive).
   - A reversed journal entry and its reversal are both "posted" and cancel each other out.
   - Credit notes are stored in the invoice table as positive amounts, and must be subtracted.
   - "Open orders" means `CONFIRMED` + `PARTIALLY_DELIVERED`.
4. **Missing foreign keys.**
   - Invoices point at customers through `partner_id`, which has no FK constraint.
   - So a schema-driven join finder cannot discover "invoice → customer".
5. **Unsigned stock quantities.** Stock-move quantities are always positive; the direction comes
   from `move_type` and which bin column is set.
6. **Sensitive data.** HR salary, national id and bank account columns are masked by Atlas'
   own policy, and must never be exposed.

**Data we ran against:**

- **Demo seed.** 7 tenants, about 3 months of data (Apr–Jul 2026), and roughly 3 customers per
  tenant.
- **Volume seed** (`seed.py --volume`). A "volume" tenant with 50 customers, 5,000 items, 10,000
  sales orders and 100,800 GL lines, spread over 2024–2026. This makes year-over-year questions
  meaningful.
- **Edge cases** (`infra/enrich_demo_data.py`). The demo seed had none of these, so we created
  them for `acme` through **Atlas' own HTTP API**, so every journal and invariant is real:
  - receipts: one full payment and one partial payment;
  - a credit note (created via a sales return);
  - a reversed journal entry;
  - DRAFT and CANCELLED orders;
  - a vendor payment;
  - an overdue invoice.

### 2.2 PromptQuery (`promptquery/`): the prototype we evolved

It is a Python CLI (`promptquery <dsn>`). Its pipeline, in `src/promptquery/cli.py:run_question`:

```
question → TF-IDF ranks tables (retrieval.py)
         → LLM picks ≤15 of them (retrieval.llm_select_tables)
         → add 1-hop FK neighbours (retrieval.expand_via_fks)
         → LLM writes SQL from those tables' schema (prompts.py, llm.py)
         → sqlglot checks it is a single SELECT (safety.py)
         → run it on a read-only session (db.py), 1 repair attempt on DB error (repair.py)
         → print a table (render.py)
```

**What was good, and kept:**

- the idea of retrieving only relevant tables instead of dumping the whole schema;
- sqlglot-based SQL safety;
- a read-only session;
- execution-guided repair;
- clean schema dataclasses;
- support for both Anthropic and OpenAI.

**What was missing or broken for this challenge (the diagnosis):**

| # | Problem | Consequence on Atlas |
|---|---|---|
| 1 | Completely **stateless**: no conversation memory and no HTTP API | Follow-ups like "break that down by customer" cannot work. |
| 2 | **No tenant awareness** | Every answer silently mixes 8 companies. |
| 3 | **Security holes**: only 8 blocked functions | `dblink('…','DELETE …')` opens a *new* session that is not read-only, so it can write. `pg_sleep` and `pg_read_file` were allowed. `SELECT … INTO` and `FOR UPDATE` were not blocked. Results were fetched with no row cap. |
| 4 | **No business semantics** | The model guesses what "sales", "revenue", "owed" and "open" mean, and invents status values. |
| 5 | Joins only through real FKs | `partner_id` joins are invisible. |
| 6 | Selector prompt hard-codes Odoo examples; a zero TF-IDF match falls back to 3 arbitrary tables | Wrong tables. |
| 7 | Repair: 1 round, DB errors only | No schema hints, and wrong-but-valid results are never reconsidered. |
| 8 | No retries or backoff; newer OpenAI models rejected (`max_tokens`) | Flaky, and `gpt-6-luna` would not run at all. |
| 9 | Its evaluation tested an older pipeline; the headline 100 %/94 % numbers only check table names, never results | No trustworthy accuracy signal. |

We measured the unmodified prototype on our own question set with the same scoring:
**28/64 = 43.8 %** (`eval/atlas_baseline.py`). The evolved bot scores 64/64 on those questions.

---

## Part 3: What we changed: the new architecture

### 3.1 The big picture

```
                         ┌──────────────── Atlas PostgreSQL ────────────────┐
                         │  public.* (132 Atlas tables, untouched)          │
                         │  rpt.*    (114 security_barrier views, NEW)      │
  atlas-bot setup-db ───►│  role atlas_reporting (SELECT on rpt only, NEW)  │
                         └──────────────────────▲──────────────────────────┘
                                                │ read-only, tenant-scoped
 user ─► CLI / HTTP API / web chat ─► ReportingSession (remembers ≤6 turns)
            1 PLAN      (LLM)  understand the message in context → standalone request + tables
            2 RETRIEVE         metrics + verified examples + schema detail for chosen tables
            3 GENERATE  (LLM)  ONE SQL, with Atlas business rules
            4 GUARD            static SQL safety + allow-lists + LIMIT
            5 EXPLAIN/EXECUTE  as atlas_reporting in a READ ONLY transaction
              (errors → REPAIR with the real column list; empty/zero result → one self-review)
            6 RESULT VALIDATION → CONFIDENCE
                 HIGH → answer
                 LOW  → independent 2nd SQL; agree = "verified"; disagree → 3rd SQL, majority
            7 ANSWER    (LLM)  short grounded summary + the table + the SQL
```

### 3.2 Change-by-change: what, where, and why

**A. Database guard rails: `src/promptquery/atlas/setup_reporting.py` (new)**

Run once with `atlas-bot setup-db`, using Atlas' owner credentials. It is idempotent and
additive: no Atlas table is modified.

- **Creates schema `rpt` with one view per reportable table (114 views).**
  - Each view has `WITH (security_barrier)`.
  - Each view filters `WHERE tenant_id = ANY(rpt.scope_tenant_ids())`. That function reads the
    session setting `atlas.tenant_ids`, which our executor sets. If the setting is unset, the
    views return **zero rows**, so the system fails closed.
  - Each view drops `tenant_id`, which also removes a source of LLM confusion, and every
    masked HR column.
  - Secret or infrastructure tables (users, API keys, sessions, audit log, jobs…) and
    per-employee payroll lines get **no view at all**.
- **Creates role `atlas_reporting`.**
  - `LOGIN`, and `SELECT` on `rpt` only; nothing on `public`.
  - Role defaults: `default_transaction_read_only=on`, `statement_timeout`, `search_path=rpt`.
- **Creates `rpt.tenant_directory()`.**
  - A security-definer function that maps a tenant slug to its id, exposing only
    (id, slug, name).
  - Generated SQL can never call it, because the SQL guard allow-lists functions.

*Why:* safety is enforced by **Postgres itself**, whatever SQL the model writes, and the model
no longer has to remember a tenant filter. That alone removes the most common Atlas error.

**B. Executor: `reporting/executor.py` (new)**

- Every query opens a fresh connection as `atlas_reporting` and runs a `READ ONLY` transaction.
- Inside it, the executor sets three things with bound parameters, never with generated SQL:
  the timeout, the search path and the tenant.
- It runs `EXPLAIN` first, then the query, and **always rolls back**.
- It fetches `max_rows+1` rows, so it can report truncation.

**C. SQL guard: `reporting/guard.py` (new) + hardened `safety.py`**

`safety.py` checks the statement itself:

- exactly one statement, whose root is SELECT, UNION or WITH;
- DML/DDL is rejected **anywhere in the tree**, including inside CTEs;
- `SELECT INTO` and `FOR UPDATE` are rejected;
- a **denylist of about 40 functions and prefixes**, including `dblink*`, `lo_*`, `pg_sleep*`,
  `pg_read_file`, `set_config`, `current_setting`, `query_to_xml`, `nextval`/`setval` and
  advisory locks.

`guard.py` adds checks against the catalog:

- a **table allow-list**: only known views, and any schema prefix is rejected;
- a **function allow-list** for functions sqlglot does not model natively;
- automatic `LIMIT`;
- errors written so the repair model can act on them, e.g. "table 'sales_order' does not exist;
  did you mean sales_orders?".

**D. Catalog: `reporting/catalog.py` (new)**

It introspects the 114 views and caches the result to `.cache/atlas_catalog.json`, so no
per-request introspection is needed. It also:

- re-attaches **foreign keys** from the base tables (views have none), dropping the `tenant_id`
  half of Atlas' composite keys;
- adds **virtual FKs** for `partner_id` references;
- merges **status value lists** from `atlas/data/enum_values.json`: 78 columns, extracted from
  Atlas' Python enums;
- merges **descriptions** from the semantic layer.

**E. Semantic layer: `atlas/data/semantic.yaml` (new)**

This is Atlas business knowledge, kept as data and editable without code changes:

- `global_rules`: vocabulary (product = item, supplier = vendor), the document chains, which
  date column goes with which document, how to do year/period filters and comparisons, how to
  rank, and privacy rules.
- `metrics`: 16 definitions with trigger keywords. Examples: sales order value, GL revenue,
  invoiced revenue with credit notes, receivables and aging, payables, purchases, open orders,
  inventory, stock movements, GL balances, CRM pipeline, headcount, payroll, production.
- `virtual_foreign_keys`
- `tables`: a one-line description for about 90 tables, plus column notes.

**F. Verified example bank: `atlas/data/examples.yaml` (new)**

- 34 question→SQL pairs.
- Each one is executed against two tenants by the test suite.
- The 3–4 most similar examples are shown to the SQL generator.
- The example questions are deliberately **different** from the evaluation questions.

**G. Retrieval: `reporting/retrieval.py` (new; reuses PromptQuery's TF-IDF)**

- Ranks tables by combining four signals:
  - TF-IDF over names, descriptions and columns;
  - metric keyword matches;
  - tables of similar examples;
  - tables used in the previous turn.
- The planner LLM sees only a compact **one-line index** of tables and picks what it needs.
- Full column detail is then sent only for:
  - the chosen tables,
  - the matched metrics' tables,
  - the examples' tables,
  - one-hop FK "name" tables (customers, items, vendors, accounts…),
  - capped at 18 tables.
- The full 114-table schema is never sent.

**H. Conversation planner: the multi-turn core (`reporting/prompts.py` PLANNER + `engine._plan`)**

The planner gets the structured history of up to 6 turns. For each turn that is: the user
text, the resolved request, the SQL and a result preview. It returns JSON with:

- `intent`: `report`, `refuse`, `clarify` or `chat`;
- a **standalone request** with explicit dates and filters;
- a report spec (metric, dimensions, filters, time range, comparison, limit);
- tables.

It resolves follow-ups with explicit rules, each one learned from a real failure seen in
evaluation:

| Rule | Example |
|---|---|
| Inherit everything unless changed | "break it down by customer" |
| Narrow by adding a filter, not by copying ids from a preview | "only Contoso" |
| Relative periods are relative to the discussed period when the today-based reading would repeat it | After "sales in 2025", "what about last year?" = **2024** |
| Keep the user's metric word | "sales" (orders) ≠ "revenue" (GL) ≠ "invoiced" (AR) |
| Drop filters that don't apply to a new metric | An item filter doesn't apply to "what do they owe" |
| "who … the most" is a single row | |
| Refuse writes, SQL injection attempts and individual salary/PII, but allow company-level payroll totals | |

**I. Engine: `reporting/engine.py` (new)**

This is the orchestrator: plan → retrieve → generate → guard → explain/execute → repair →
answer. Its features:

- **Result validation and confidence gating** (`validate_result()` and `_solve()`, mode
  `ATLAS_CONSENSUS=adaptive`, the default).
  - The engine generates **one** SQL, then guards it, runs `EXPLAIN`, executes it and
    **validates the result** with deterministic checks:
    - Did it need repairs or an empty-result review?
    - Is the result empty or all zero?
    - Does it filter on a status value that doesn't exist in Atlas?
    - Is there a status filter inside a `LEFT JOIN … ON` (which silently keeps draft/cancelled
      lines)?
    - Are there more rows than the requested top-N?
    - Did it ignore the tables of the business metric the question matched?
    - Are there negative counts?
  - **HIGH confidence** (no check fired): the answer is returned right away. That's one SQL,
    the cheapest and fastest path.
  - **LOW confidence:** an independent second SQL is generated and executed.
    - If both results agree (same rows, and same values in the shared columns), the answer is
      returned as **verified**.
    - If they disagree, a third SQL breaks the tie by majority vote.
  - `ATLAS_CONSENSUS=always` generates 2 SQL on every turn (the earlier behaviour), and `off`
    uses 1 SQL with no verification.
  - Every turn exposes `confidence` and `confidence_reasons` in the API and in the eval output.
- **Repair.**
  - Guard rejections, `EXPLAIN` errors and runtime errors are sent back to the model, with the
    exact error and the **real column list** of the tables involved.
  - Up to 3 rounds.
- **Vacuous-result review.**
  - "No rows", or a single row of only 0/NULL (e.g. `SUM` over a filter that matched nothing,
    such as `ILIKE '%Steel Bolts%'` when the item is "Steel Bolt M6"), triggers one self-review.
  - The review checks status values, dates, joins and name matching.
- **Data coverage.** Per-company row counts and date ranges are added to the prompt, so the
  model knows, for example, that acme's data starts in April 2026.
- **Conversation state.** A sliding window of `ATLAS_MAX_TURNS=6` turns, stored as structured
  turns, not raw chat.

**J. LLM layer: `llm.py` (modified)**

- Retries with exponential backoff, which honour "try again in Xs" rate-limit hints.
- Reasoning-model support (`gpt-5`/`gpt-6`/o-series use `max_completion_tokens`), plus an
  automatic parameter-style switch on "Unsupported parameter" errors.
- `OPENAI_BASE_URL`, for any OpenAI-compatible endpoint.
- `ClaudeCLIClient`: runs prompts through a logged-in Claude Code CLI, isolated (no tools, no
  settings), for development without an API key.
- `extract_json()`, used for the planner output.

**K. Interfaces: `reporting/cli.py`, `reporting/server.py` (new)**

- `atlas-bot` CLI commands: `setup-db`, `build-catalog`, `tenants`, `chat` (REPL with `/reset`,
  `/tenant`, `/sql`), `ask q1 q2 …` (one conversation, `--json`) and `serve`.
- FastAPI server:
  - `POST /chat {message, session_id?, tenant?}`
  - `GET /sessions/{id}`
  - `POST /sessions/{id}/reset`
  - `/tenants`
  - `/health`
  - a minimal web chat at `/`
- Sessions are kept in memory with a TTL and LRU eviction, and a per-session lock keeps turns
  sequential.

**L2. Schema-evolution safety (new)**

- `is_sensitive()` in `atlas/setup_reporting.py`: **default-deny masking by column name**, so a
  table Atlas adds later cannot leak PII even though discovery is automatic.
- `GLOBAL_TABLES`: mechanism for exposing genuinely shared reference tables that have no
  `tenant_id` (unfiltered views); empty today.
- `fallback_description()` in `reporting/catalog.py`: an undescribed table still gets its module
  domain ("Finance / accounting: allocation runs …") instead of a bare name.
- `atlas-bot drop-db-views` + `make atlas-migrate`: the `rpt` views depend on Atlas tables, so
  column-altering migrations fail while they exist; this drops, migrates and recreates.
- `infra/extract_enum_values.py`: audits the status-value lists against the Atlas source and the
  live data (MISSING / STALE / ORPHANED). Report-only by design — automated column→enum matching
  was tried and rejected as unreliable.
- `tests/test_schema_drift.py` (11 tests): turns every drift case into a CI failure.

**L3. Operations (new)**

- `ATLAS_API_TOKEN`: bearer-token auth on the HTTP API and web UI (constant-time compare).
- `ATLAS_AUDIT_LOG`: one JSON line per turn — the observability hook. It found a real bug during
  testing: a "biggest increase" query that filtered `WHERE new > old` and so returned nothing
  when every customer declined.

**L. Configuration: `reporting/config.py`, `.env.example` (new)**

- Everything is controlled through environment variables or `.env`.
- It accepts either `ATLAS_LLM=openai/gpt-6-luna` or the generic
  `LLM_PROVIDER / LLM_API_KEY / STRONG_MODEL / FAST_MODEL`.
- The strong model plans and writes SQL; the fast model writes the answer text.

**M. Tooling: `Makefile`, `infra/` (new)**

- `make install / atlas-up / atlas-seed / setup-db / test / eval / chat / serve`.
- `infra/atlas-compose.override.yml` remaps Atlas' ports to 55433 (Postgres) and 18000 (API),
  so they don't clash with other local services.

**N. Evaluation and tests: `eval/atlas_eval.py`, `eval/atlas/*.yaml`, `tests/*` (new)**

See Part 5.

### 3.3 Files at a glance

| Status | Path | Purpose |
|---|---|---|
| modified | `src/promptquery/safety.py` | bigger denylist, INTO/lock detection, `function_names()` |
| modified | `src/promptquery/llm.py` | retries, reasoning models, base URL, Claude CLI, JSON extraction |
| modified | `pyproject.toml` | sqlglot ≥ 25, pyyaml, fastapi, uvicorn, httpx; `atlas-bot` entry point |
| modified | `README.md` | now the bot's README (original moved to `docs/PROMPTQUERY_README.md`) |
| new | `src/promptquery/reporting/{engine,prompts,retrieval,guard,executor,catalog,config,cli,server}.py` | the reporting engine |
| new | `src/promptquery/atlas/setup_reporting.py`, `atlas/data/{semantic.yaml,examples.yaml,enum_values.json}` | Atlas profile |
| new | `eval/atlas_eval.py`, `eval/atlas_baseline.py`, `eval/atlas/{single_turn,multi_turn,security,holdout,holdout_b}.yaml` | evaluation |
| new | `tests/test_reporting_guard.py`, `test_reporting_engine.py`, `test_atlas_integration.py`, `test_eval_compare.py` | 55 new tests |
| new | `docs/*.md`, `Makefile`, `.env.example`, `infra/*` | docs and tooling |
| unchanged | the original `promptquery` CLI | still works (`promptquery <dsn>`) |

---

## Part 4: One conversation, step by step

Setup: tenant `volume`, today = 2026-09-27.

**Turn 1: "Show me total sales in 2025"**

1. *Plan.* There is no history. The intent is `report`, and the standalone request becomes
   "total sales from 2025-01-01 to 2025-12-31". The planner picks `sales_orders`.
2. *Retrieve.* The keyword "sales" matches the `sales_order_value` metric, which says: sum
   `total_amount`, excluding DRAFT/CANCELLED/CREDIT_BLOCKED, dated by `order_date`. The most
   similar example is "What is the total sales amount this year?".
3. *Generate.* One SQL is generated:
   `SELECT ROUND(SUM(so.total_amount),2) … WHERE so.status NOT IN (…) AND so.order_date >= '2025-01-01' AND so.order_date < '2026-01-01'`.
4. *Guard.* The query is one SELECT on a known view, so it passes, and `LIMIT 1001` is appended.
5. *Execute.* `EXPLAIN` runs, then the query runs as `atlas_reporting` with
   `atlas.tenant_ids = <volume id>`, and the transaction is rolled back. The result is
   **4,693,492.00**.
6. *Result validation.* No repairs were needed, the result is non-empty, the status literals
   are valid Atlas values, and the query uses the `sales_orders` metric table. Confidence is
   **HIGH**, so no second SQL is generated.
7. *Answer.* "Total sales in 2025 were USD 4,693,492.00 (confirmed orders, excluding drafts…)".
   The turn is stored.

**Turn 2: "Break that down by customer"**

- The planner sees turn 1's request and SQL, and writes "total sales for 2025 **by customer**".
- The generator gets turn 1's SQL as context, so it keeps the same definition and adds
  `JOIN sales_customers … GROUP BY c.id, c.name`. The result is 50 rows.

**Turn 3: "Only show top 5"** → same request + `ORDER BY … DESC LIMIT 5`.

**Turn 4: "What about last year?"**

- Read against today, "last year" would be 2025, but that is exactly what was just shown.
- So the planner rule resolves it to **2024**: the top 5 of 2024.

**Turn 5: "Compare it with 2025"**

- The top-5 customers of 2024, with 2024 and 2025 side by side.
- This uses `SUM(...) FILTER (WHERE …)`, following the business rule for comparisons.

**Turn 6: "Which customer had the biggest increase?"**

- The planner restricts the question to the 5 customers from the previous result.
- The query computes 2025 − 2024 and returns the single top row.
- The bot answers honestly: none of the five grew; the smallest decline was Volume Customer 49.

**If the user says "Delete that customer"**, the planner returns `intent=refuse` and no SQL is
generated. Even if SQL were generated, the guard rejects DELETE, and the role cannot write anyway.

---

## Part 5: How it was tested

1. **Unit tests** (no LLM calls): 123 pytest tests.
   - Guard: 19 attack queries rejected, valid reporting queries accepted.
   - Comparator.
   - The engine with a **scripted fake LLM**, covering:
     - conversation context passing;
     - repair with column hints;
     - an unsafe SQL query being repaired rather than executed;
     - refusal;
     - giving up after max repairs;
     - the 6-turn window;
     - confidence gating (HIGH → 1 SQL; LOW → verifying 2nd SQL);
     - result-validation checks (bad status literal, status filter in a LEFT JOIN, all-zero
       measures, answer granularity);
     - consensus majority vote;
     - zero-result review.
2. **Live-database integration tests**, against the real Atlas DB:
   - writes rejected;
   - base tables and secrets invisible;
   - masked columns absent;
   - **tenant isolation** (acme ∩ retail = ∅);
   - unscoped sessions see nothing;
   - statement timeout enforced;
   - row cap;
   - catalog semantics;
   - all 34 examples run on 2 tenants.
3. **Execution-accuracy evaluation** (`eval/atlas_eval.py`):
   - The bot's SQL result is compared with the result of hand-verified gold SQL on the live DB.
   - Column names, column order and extra columns are ignored.
   - Numbers are compared with a 0.01 tolerance.
   - Row order is checked only when the question implies it.
   - Sets used:
     - **dev**: 64 single-turn + 10 conversations (51 turns) + 12 security scenarios;
     - **hold-out A**: written after the first tuning, run blind once;
     - **hold-out B**: written after *all* tuning, run blind.
   - Security scenarios also check that a DB fingerprint (row counts) is unchanged, and that
     no forbidden column appears in the SQL.
4. **End-to-end manual runs** through the CLI (`atlas-bot ask …`) and the HTTP API, including a
   full 6-turn conversation ending with a refused delete.
5. **Baseline comparison.** `eval/atlas_baseline.py` runs the *original* PromptQuery pipeline on
   the same questions.

Results and failure analysis: `TESTING.md`. Summary, over three independent runs of
the final code: **99.2–100 %** on the 127 dev turns, **100 %** on hold-out A (37 turns) and
**96.7 %** on hold-out B (30 turns). The blind first runs of the two hold-out sets, before their
failures were examined, scored **95.5 %** combined.

---

## Part 6: How to run it (short version)

```bash
cd promptquery
make install && cp .env.example .env         # put your LLM key in .env
make atlas-up && make atlas-seed             # Atlas in Docker + data (needs ../atlas-erp)
make setup-db                                # read-only role + tenant views + catalog
make chat                                    # or: make serve  -> http://127.0.0.1:8088
make test && make eval                       # tests and accuracy evaluation
```

Local environment used during development:

| Component | Where |
|---|---|
| Atlas Postgres | `localhost:55433` (owner `atlas/atlas`, reporting `atlas_reporting/reporting_pw`) |
| Atlas API | `localhost:18000` |
| Bot API / web chat | `localhost:8088` |
| LLMs | OpenAI `gpt-6-luna` (plan + SQL), `gpt-4.1-mini` (answer text) |
