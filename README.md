# Atlas AI Reporting Bot

A conversational reporting assistant for the **[Atlas ERP](https://github.com/Taha-Mahmoodi/atlas-erp)**
PostgreSQL database. Ask business questions in plain English and follow up for up to six (or
more) prompts. The bot plans, writes, validates, executes and explains **read-only, tenant-scoped**
SQL.

It evolves the [PromptQuery](docs/PROMPTQUERY_README.md) prototype.

The pipeline, per question:

1. planner + semantic layer
2. schema retrieval
3. **one** SQL
4. static guard
5. EXPLAIN
6. execute (read-only)
7. **result validation → confidence**
   - HIGH → answer.
   - LOW → an independent second SQL verifies the result (and a third breaks a tie).

For the diagnosis and the design, see:

- **[docs/PROJECT_WALKTHROUGH.md](docs/PROJECT_WALKTHROUGH.md)**: the full end-to-end
  explanation of both repos and every change.

- **[docs/ARCHITECTURE_ATLAS.md](docs/ARCHITECTURE_ATLAS.md)**: approach, architecture changes,
  table selection, joins, SQL generation, validation, conversation context.
- **[docs/TESTING.md](docs/TESTING.md)**: how it was tested, and the results.
- **[docs/ASSUMPTIONS_AND_LIMITATIONS.md](docs/ASSUMPTIONS_AND_LIMITATIONS.md)**

```
you  > Show me total sales in 2025
bot  > Total sales in 2025 were USD 4,693,492.00 (confirmed orders, excluding drafts/cancelled/credit-blocked).
you  > Break that down by customer        -> 50 customers, same period and definition
you  > Only show top 5                    -> same report, LIMIT 5
you  > What about last year?              -> top 5 for 2024
you  > Compare it with 2025               -> those 5 customers, 2024 vs 2025 side by side
you  > Which customer had the biggest increase?
bot  > None of them grew; Volume Customer 49 had the smallest decline (-22,522.00).
```

## Results

Accuracy is **execution accuracy**: the bot's result set must equal the result set of
hand-verified gold SQL on the live database (see [docs/TESTING.md](docs/TESTING.md)).

| Set | Turns | Accuracy |
|---|---|---|
| **Hold-out sets, blind first runs** (not used for tuning at that point) | 67 | **95.5 %** |
| Hold-out A, final code, 5 runs | 37 | **100 %** in all 5 runs |
| Hold-out B, final code, 5 runs | 30 | **96.7 %** (93.3 % in one run; 1 ambiguous case) |
| Dev: 64 single-turn + 10 conversations (51 turns) + 12 security, 5 runs | 127 | **99.2 / 99.2 / 100 / 100 / 100 %** |
| Security scenarios | 12 | **100 %** |
| Original PromptQuery pipeline, same 64 questions | 64 | 43.8 % |
| pytest (unit + live-DB integration + schema drift + auth) | 137 tests | all pass |

Multi-turn accuracy does not decay with depth (turns 1–6: 10/10, 10/10, 10/10, 10/10, 8/8, 3/3),
and the confidence gate keeps cost down at **1.00–1.17 SQL statements per turn**.

## Requirements

- Python 3.10+ ([uv](https://github.com/astral-sh/uv) recommended)
- Docker, to run Atlas
- An LLM API key: OpenAI (tested with `gpt-6-luna` and `gpt-4.1-mini`) or Anthropic

## Setup

```bash
# 0. Get both repositories side by side
git clone https://github.com/Taha-Mahmoodi/atlas-erp.git
git clone <this repository> promptquery && cd promptquery

# 1. Install the bot
make install                       # uv venv + pip install -e '.[dev,openai]'
cp .env.example .env               # then set LLM_API_KEY (and DSNs if not using the defaults)

# 2. Start Atlas and load data. Host ports 55433 (Postgres) and 18000 (API) avoid clashes.
make atlas-up                      # docker compose: Atlas postgres:16 + backend (runs migrations)
make atlas-seed                    # demo seed (7 tenants) + high-volume tenant + edge cases

# 3. Create the read-only reporting role and the tenant-scoped views (one-time, idempotent)
make setup-db                      # = atlas-bot setup-db && atlas-bot build-catalog
```

**When Atlas changes** (new table, new column, a migration): run `make setup-db` again — views
are rediscovered from the live schema. If the migration ALTERs or DROPs columns, use
`make atlas-migrate`, which drops the views, migrates, and recreates them.
`tests/test_schema_drift.py` fails if a migration exposes sensitive data or leaves the semantic
layer stale. Details: [docs/ASSUMPTIONS_AND_LIMITATIONS.md](docs/ASSUMPTIONS_AND_LIMITATIONS.md).

**Already have an Atlas database?** Skip step 2. Put its owner DSN in `ATLAS_ADMIN_DSN` and the
reporting DSN (same host/db, user `atlas_reporting`) in `ATLAS_REPORTING_DSN`, then run
`make setup-db`.

## Run

```bash
atlas-bot chat                                   # interactive multi-turn REPL (/reset, /tenant <slug>, /sql, /exit)
atlas-bot chat --tenant volume
atlas-bot ask "Total sales in 2025" "Break that down by customer" "Only top 5"   # one conversation, non-interactive
atlas-bot ask --json "How much do customers owe us?"                            # machine-readable
atlas-bot serve --port 8088                      # HTTP API + minimal web chat at http://127.0.0.1:8088
atlas-bot tenants                                # list companies
atlas-bot drop-db-views --yes                    # remove rpt views (needed before Atlas migrations)
```

(`atlas-bot` lives in `.venv/bin/`. Activate the venv or use `make chat` / `make serve`.)

### HTTP API

```bash
curl -s localhost:8088/chat -H 'content-type: application/json' \
     -d '{"message": "What is the outstanding receivables per customer?", "tenant": "acme"}'
# -> {"session_id": "...", "answer": "...", "sql": "...", "columns": [...], "rows": [...],
#     "standalone_question": "...", "intent": "report", "turn_number": 1, ...}

curl -s localhost:8088/chat -H 'content-type: application/json' \
     -d '{"message": "Which of them is overdue the longest?", "session_id": "<from above>"}'
```

| Endpoint | Purpose |
|---|---|
| `POST /chat` | `{message, session_id?, tenant?}`: runs one turn. Omitting `session_id` starts a new conversation. |
| `GET /sessions/{id}` | Conversation history. |
| `POST /sessions/{id}/reset` | Clears the context. |
| `GET /tenants` | Lists companies. |
| `GET /health` | Health check. |

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER`, `LLM_API_KEY` | – | `openai` or `anthropic`, plus its key |
| `STRONG_MODEL` / `FAST_MODEL` | – | planning + SQL model / answer-writing model |
| `ATLAS_LLM`, `ATLAS_FAST_LLM` | – | explicit specs such as `openai/gpt-6-luna` or `anthropic/claude-sonnet-5` (override the above) |
| `ATLAS_REPORTING_DSN` | `postgresql://atlas_reporting:reporting_pw@localhost:55433/atlas` | least-privilege DSN used for every query |
| `ATLAS_ADMIN_DSN` | – | owner DSN, used only by `setup-db` |
| `ATLAS_TENANT` | `acme` | company slug(s), or `all` |
| `ATLAS_TODAY` | real date | pins "today" for relative dates (evaluation uses `2026-09-27`) |
| `ATLAS_API_TOKEN` | – | bearer token for the API/web UI; **set this if not on localhost** |
| `ATLAS_AUDIT_LOG` | – | path for one JSON line per turn (question, SQL, confidence, timings) |
| `ATLAS_CONSENSUS` | `adaptive` | `adaptive` = second SQL only on LOW confidence; `always` = 2 SQL every turn; `off` = 1 SQL |
| `ATLAS_MAX_ROWS` / `ATLAS_MAX_REPAIR` / `ATLAS_STATEMENT_TIMEOUT_MS` / `ATLAS_MAX_TURNS` | 1000 / 3 / 20000 / 6 | safety and behaviour limits |

## Security

1. **The database enforces it.** The bot connects as `atlas_reporting`:
   - `SELECT` only, on `rpt` views only;
   - read-only by default, with a statement timeout and `search_path=rpt`.
2. **Tenant isolation.** Views filter on the tenant set by the bot process. If no tenant is set,
   no rows are returned.
3. **Nothing sensitive is visible.** Secrets (users, API keys, sessions) are absent, and so are
   masked HR columns (salary, national id, tax id, date of birth, bank account).
4. **Every query:**
   - is parsed with sqlglot: a single read-only statement, no DML/DDL anywhere, function
     deny/allow-lists (`dblink`, `pg_sleep`, `pg_read_file`, `set_config`, …), a table
     allow-list, no schema prefixes, and an injected LIMIT;
   - is checked with `EXPLAIN`;
   - runs in a READ ONLY transaction that is always rolled back, with a capped fetch.
5. **Write requests and requests for personal data** are refused before any SQL is generated.
6. **Default-deny masking.** Table discovery is automatic, so any column whose name suggests
   personal or secret data (`salary`, `iban`, `national_id`, `password`, `token`, `card_number`,
   …) is dropped from the views on **every** table unless individually reviewed. A future Atlas
   migration that adds such a column cannot leak it.
7. **API auth.** `ATLAS_API_TOKEN` enables bearer-token auth on every endpoint except
   `/health`. Without it the server is for localhost only.
8. **Audit log.** `ATLAS_AUDIT_LOG` records one JSON line per turn for observability.

## Testing

```bash
make test            # 121 pytest tests (guard, comparator, engine with a scripted LLM, live-DB security)
make eval            # execution-accuracy eval: dev single-turn + multi-turn + security (~12 min)
make eval-holdout    # held-out set
python eval/atlas_eval.py --check-gold           # execute and print every gold query
python eval/atlas_eval.py --ids mt01,s03         # run selected cases
```

Details and results: [docs/TESTING.md](docs/TESTING.md).

## Project layout

```
src/promptquery/
  reporting/            NEW conversational reporting engine
    engine.py             plan -> retrieve -> generate -> guard -> explain/execute -> repair
                          -> result validation / confidence gate -> answer; sessions
    prompts.py            planner / generator / repair / empty-review / answer prompts
    retrieval.py          TF-IDF + metric + example + history signals, table index, FK name expansion
    guard.py              static SQL validation + LIMIT injection
    executor.py           read-only, tenant-scoped execution
    catalog.py            rpt introspection + FKs + virtual FKs + enums + descriptions (JSON cache)
    server.py, cli.py     FastAPI app + web chat; `atlas-bot` CLI
  atlas/
    setup_reporting.py    DB guard rails: role + security_barrier tenant views
    data/semantic.yaml    business rules, metrics, glossary, table/column descriptions, virtual FKs
    data/examples.yaml    34 verified question->SQL examples (few-shot bank)
    data/enum_values.json status values for 78 columns, extracted from Atlas' code
  safety.py, llm.py, retrieval.py, schema.py ...   original PromptQuery modules (hardened)
eval/atlas_eval.py      execution-accuracy harness;  eval/atlas/*.yaml  dev, security, hold-out sets
infra/                  docker-compose port override, Atlas edge-case enrichment script, psql helper
tests/                  pytest suite (incl. test_schema_drift.py: fails when an Atlas migration
                        exposes sensitive data or leaves the semantic layer stale)
```

The original PromptQuery CLI (`promptquery <dsn>`) still works unchanged.
