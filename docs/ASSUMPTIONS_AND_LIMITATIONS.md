# Assumptions, Dependencies and Known Limitations

## Assumptions

1. **Tenant scope.** Atlas is multi-tenant. The bot reports on one configured company
   (`ATLAS_TENANT`, default `acme`), the way a logged-in Atlas user would.
   - It can also report on a list of companies (`acme,retail`) or on every tenant (`all`).
   - The company can be changed per session: `tenant` in the API, or `/tenant <slug>` in the REPL.
   - If your test questions assume the whole database, set `ATLAS_TENANT=all`.
2. **Metric definitions.** These come from Atlas' own code and are documented in
   `src/promptquery/atlas/data/semantic.yaml`.
   - **Sales** = value of committed sales orders (`sales_orders.total_amount`, excluding
     `DRAFT`, `CANCELLED` and `CREDIT_BLOCKED`), dated by `order_date`.
   - **Revenue** = posted GL revenue (`fin_journal_lines`, credit − debit, on REVENUE accounts).
   - **Invoiced** = AR invoices (excluding `DRAFT`/`REVERSED`) minus credit notes (`CN-…`).
   - **Outstanding / owed** = `open_amount` of `POSTED`/`PARTIALLY_PAID` invoices.
   - **Open orders** = `CONFIRMED` and `PARTIALLY_DELIVERED`; **open POs** = `APPROVED`, `SENT`
     and `PARTIALLY_RECEIVED`. Both come from Atlas' dashboards.
   - **COGS** = GL accounts referenced by `inv_item_categories.cogs_account_id`.
   - **Stock and stock value** come from `inv_item_valuations`.
   - To change a definition, edit the YAML. No code changes are needed.
3. **Dates.** Relative periods are calendar periods, and fiscal years are calendar years. This
   holds for the seeded data, where FY = calendar year.
   - "Today" is the real date unless `ATLAS_TODAY` is set. The evaluation pins 2026-09-27.
   - In a follow-up, "last year" is read relative to the period being discussed when the
     today-based reading would simply repeat that period. For example, after "sales in 2025",
     "what about last year?" means 2024.
4. **Currency.** Amounts are reported in the functional currency; all seeded documents are USD.
   The GL uses the `functional_*` amounts.
5. **Evaluated data.** Atlas' demo seed provides 7 tenants with about 3 months of data. `seed.py
   --volume` adds a tenant with 2024–2026 data (10k orders, 100k GL lines). The seeded data has
   no receipts, credit notes, reversals or draft/cancelled orders, so
   `infra/enrich_demo_data.py` creates those edge cases for `acme` **through the Atlas HTTP
   API**, which keeps Atlas' invariants.
6. **Privacy.** Following Atlas' D-009/D-052/D-059 policy:
   - Individual employees' compensation and PII are never available.
   - Company-level payroll totals are allowed.
   - `hr_payroll_run_lines` (per-employee pay) is excluded.

## Dependencies

- **Runtime:**
  - Python ≥ 3.10 (tested on 3.12);
  - PostgreSQL 16 (Atlas' Docker image);
  - Python packages `psycopg[binary]`, `sqlglot>=25`, `pyyaml`, `fastapi`, `uvicorn`, `click`,
    `rich`, `prompt-toolkit`, `anthropic` and, optionally, `openai`.
- **LLM:**
  - OpenAI (tested with `gpt-6-luna` for planning and SQL, and `gpt-4.1-mini` for answer text),
    or Anthropic;
  - any OpenAI-compatible endpoint via `OPENAI_BASE_URL`;
  - or a logged-in Claude Code CLI (`ATLAS_LLM=claude-cli/sonnet`) for development without a key.
- **One-time admin step:** `atlas-bot setup-db` needs the Atlas owner DSN. Everything else runs
  as the least-privilege `atlas_reporting` role.

## Known limitations

1. **Ambiguous questions get one reasonable reading.** The bot states the definition it used in
   the answer, and the SQL is always returned, but it does not ask clarifying questions except
   in extreme cases. For example, "aging report" may be produced per bucket or per customer and
   bucket; the remaining dev-set miss is exactly this.
2. **Latency.** A turn makes 2 LLM calls (plan and SQL) plus 1 for the answer, and up to 3
   repairs when needed. That is about 6–15 s per turn with `gpt-6-luna`. Setting
   `ATLAS_WRITE_ANSWER=0` skips the answer call. Results are not cached between sessions.
3. **Conversation memory is in-process.** The API keeps sessions in memory: 6 h TTL, 500
   sessions, lost on restart. A multi-instance deployment would need a shared store such as Redis.
4. **Schema evolution: how the `rpt` views track Atlas.** Views are **discovered from the live
   schema**, not from a hardcoded list: any `public` base table that has a `tenant_id` column
   becomes a view, minus `EXCLUDED_TABLES` and `MASKED_COLUMNS`. Every run drops and recreates
   all views, so new tables and new columns are picked up by one command:

   ```bash
   make setup-db          # = atlas-bot setup-db && atlas-bot build-catalog
   ```

   Because the views depend on Atlas' tables, an Atlas migration that ALTERs or DROPs a column
   fails while they exist ("cannot drop column … because other objects depend on it"). Migrate
   with:

   ```bash
   make atlas-migrate     # drop rpt views -> alembic upgrade head -> recreate views
   ```

   Consequences of the deny-list model, each covered by a test in `tests/test_schema_drift.py`
   so a migration fails CI instead of drifting silently:

   | Atlas change | Effect | Guard |
   |---|---|---|
   | New table with `tenant_id` | Automatically reportable | `test_every_reportable_table_is_described_or_acknowledged` |
   | New column on a reportable table | Automatically visible | `test_no_sensitive_columns_exposed` |
   | New table/column holding PII or secrets | **Exposed unless excluded** | `test_no_sensitive_columns_exposed`, `test_masked_columns_are_actually_absent` |
   | New status column | No value list, so the model may invent values and the SQL validator cannot check it | `test_status_columns_have_legal_values` |
   | Table renamed or dropped | Semantic layer, examples or exclusions go stale; a renamed secret table becomes reportable | `test_semantic_layer_has_no_stale_tables`, `test_examples_reference_real_tables`, `test_exclusions_are_not_stale` |
   | New table **without** `tenant_id` | Invisible to the bot (fail-safe, but unreportable) | reported by the audit; add it explicitly if needed |

   **Default-deny masking** is what makes the automatic discovery safe: `is_sensitive()` in
   `atlas/setup_reporting.py` drops any column whose name contains `salary`, `iban`,
   `bank_account`, `national_id`, `tax_id`, `date_of_birth`, `password`, `secret`, `token`,
   `api_key`, `card_number` and similar, on every table, unless the exact `table.column` is in
   the reviewed `SENSITIVE_ALLOWED` set. Verified by planting an `hr_bank_details(iban,
   salary_grade)` table: after `setup-db`, only `id`, `employee_id` and `notes` were exposed.

   `python infra/extract_enum_values.py` audits the legal-value lists against the Atlas source
   and the live data, reporting MISSING / STALE / ORPHANED columns (exit 1 when action is
   needed). It deliberately does **not** write the file: name-based column→enum matching proved
   unreliable (it mapped `hr_employees.status` to `ProjectStatus`), and a corrupted value list is
   worse than a missing one.

   A newly reportable table works from its column names alone, but gets no business guidance
   until it is described in `semantic.yaml`. 29 existing low-traffic tables (allocation runs,
   menu sections, routing operations…) are in that state today, recorded as a frozen baseline in
   the drift test so that *new* ones fail.
5. **Views are created in the Atlas database** (schema `rpt`, role `atlas_reporting`). This is
   additive only: no Atlas table, row or migration is touched. It does mean the owner DSN must be
   provided once.
6. **Evaluation scope.**
   - The gold answers encode the definitions above. An evaluator who uses different definitions
     (e.g. "sales" = invoiced amounts) will get different numbers. The bot's answers name the
     definition used, so this is visible.
   - Seeded demo data is small: 3 customers per tenant, and the volume tenant has uniform
     synthetic data. Accuracy on richer production data has not been measured.
   - Some dev-set cases accept more than one gold interpretation. Each alternative is listed
     explicitly in the YAML.
7. **Single statement per question.** A request that needs two unrelated result sets in one turn
   (e.g. "top customers and also the stock value") is answered with one query or partially.
   Asking in two turns works.
8. **Model dependence.** Accuracy was measured with `gpt-6-luna`. Weaker models may need more
   repair rounds. The pipeline and prompts are model-agnostic, and the provider is switched in
   `.env`.
