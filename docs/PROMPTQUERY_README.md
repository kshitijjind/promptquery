<p align="center">
  <a href="https://prq.menghi.dev">
    <img src="https://raw.githubusercontent.com/Cyberfilo/promptquery/main/docs/brand/banner.png" alt="PromptQuery — SQL is engineering, not conversation" width="820">
  </a>
</p>

<p align="center"><strong>Natural-language SQL for production-scale Postgres schemas.</strong></p>

<p align="center">
  <a href="https://pypi.org/project/promptquery/"><img src="https://img.shields.io/pypi/v/promptquery?color=4ee07a&labelColor=0a0b10" alt="PyPI"></a>
  <a href="https://github.com/Cyberfilo/promptquery/actions/workflows/ci.yml"><img src="https://github.com/Cyberfilo/promptquery/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-4ee07a?labelColor=0a0b10" alt="License: Apache 2.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.10%2B-4ee07a?labelColor=0a0b10" alt="Python: 3.10+"></a>
</p>

PromptQuery is an open-source CLI that lets you query Postgres in plain English — engineered for **real production schemas with hundreds of tables**, not toy demos. It introspects your schema, generates SQL, shows it for confirmation, and runs it read-only.

<p align="center">
  <img src="https://raw.githubusercontent.com/Cyberfilo/promptquery/main/docs/demo.gif" alt="PromptQuery turning the plain-English question 'orders over 1000 euros with the customer name and status' into a correct multi-table JOIN — showing the SQL, asking for confirmation, then printing the result rows" width="820">
</p>
<p align="center"><sub>Plain English in → the SQL it's about to run → your rows. Read-only, with a confirmation step. Here it even reads <em>"euros"</em> as a currency join across three tables. <a href="#try-it-without-setting-up-a-database">Try it yourself on a live 216-table database, no setup ↓</a></sub></p>

---

## The numbers

### Execution accuracy — 211-table benchmark, v0.3.0

Measured on [heron](https://github.com/Cyberfilo/heron), an independent open benchmark: a seeded
211-table / 14-schema Postgres database (4.4M rows) with 100 audited questions, scored by
**execution-equality** (the query runs and returns the right rows). Conditions: `gpt-4o`,
temperature 0, single-state EX@1. Receipts: [`results_prq_v031.json` (heron PR #1)](https://github.com/Cyberfilo/heron/pull/1).

| | v0.2.2 | **v0.3.0** |
|---|---:|---:|
| Execution accuracy (EX@1) | 58% | **72%** |
| Hard DB errors | 7/100 | **0/100** |
| Schema-retrieval recall | 98% | 99% |
| Tokens / query | 4,257 | 4,689 |

Same benchmark, same model, same questions — only the package version changed. Where it still
falls short, on purpose and in the open: window-function questions sit at 20%, multi-join at 50%.

### Retrieval accuracy — two more production-scale schemas (v0.2)

SQL generation: `gpt-4o`. Table selection: `gpt-4o-mini`.

**What "accuracy" means here:** a question passes only if the generated SQL references *every* table the question needs and *invents none* (parsed with `sqlglot`). These two schemas ship without seeded data, so queries are parsed, not executed — execution-equality is measured separately on the seeded `shop` fixture (see [Benchmark](#benchmark)). Finding the right handful of tables out of hundreds is the hard part, and this measures exactly that. "Tokens / query" is the SQL-generator prompt size, measured with `tiktoken`.

### Odoo 18 ERP — 675 tables ([`eval/fixtures/odoo.schema.json`](eval/fixtures/odoo.schema.json))

| Pipeline | Accuracy | Tokens / query | Latency |
|---|---:|---:|---:|
| Naive (full schema in prompt) | 84.0 % | ~85,600 | 3.4 s |
| PromptQuery v0.1 *(TF-IDF only)* | 76.0 % | ~4,500 | 2.0 s |
| **PromptQuery v0.2 *(TF-IDF + LLM selector)*** | **100.0 %** | **~7,100** | 5.6 s |

### EMBL-EBI RNAcentral — 216 tables, biology domain, [public read-only DB](https://rnacentral.org/help/public-database)

| Pipeline | Accuracy | Tokens / query | Latency |
|---|---:|---:|---:|
| Naive (full schema in prompt) | 82.0 % | ~28,200 | 3.0 s |
| PromptQuery v0.1 *(TF-IDF only)* | 74.0 % | ~2,600 | 1.9 s |
| **PromptQuery v0.2 *(TF-IDF + LLM selector)*** | **94.0 %** | **~1,600** | 4.8 s |

Pattern across both benchmarks: PromptQuery v0.2 wins by **+12 to +16 percentage-points** over the naive "stuff the whole schema into a prompt" baseline, at **~12–17× lower per-query token cost** (12.1× on Odoo, 17.4× on RNAcentral), validated independently on two different production schemas and domains.

*Accuracy receipts: [`eval/results_odoo_v2.json`](eval/results_odoo_v2.json), [`eval/results_rnacentral.json`](eval/results_rnacentral.json). Token receipts: [`eval/results_odoo_tokens.json`](eval/results_odoo_tokens.json), [`eval/results_rnacentral_tokens.json`](eval/results_rnacentral_tokens.json) — measured with [`eval/token_bench.py`](eval/token_bench.py). Reproduce everything with one command each — see [Benchmark](#benchmark) below.*

---

## Quick start

```bash
pip install promptquery

# Set ONE of these (PromptQuery auto-detects):
export OPENAI_API_KEY=...
export ANTHROPIC_API_KEY=...

# Connect and start asking:
prq postgresql://localhost/mydb
```

`prq` and `pquery` are short aliases for `promptquery`. All three commands work identically.

### One-shot mode (scripting / CI)

`--query` skips the REPL and returns machine-readable output. Progress messages go to stderr, results to stdout — pipe-friendly:

```bash
prq --query "how many users in Italy" postgresql://localhost/mydb         # JSON to stdout
prq --query "top 10 orders by total" --out csv postgresql://... > out.csv
prq --query "..." --out table postgresql://...                            # rich-formatted table
```

Exit codes: `0` success · `1` LLM/connection error · `2` safety-guard rejection · `3` execution error.

### Try it without setting up a database

EMBL-EBI publishes a **public read-only Postgres** with real biological RNA-sequence data (216 tables). Install PromptQuery and try it in under a minute:

```bash
pip install promptquery
export OPENAI_API_KEY=...

prq --query "for each Rfam clan, show the clan name and how many Rfam models belong to it, top 10" \
  --selector-model gpt-4o-mini --model gpt-4o --out table \
  postgresql://reader:NWDMCE5xdipIjRrp@hh-pgsql-public.ebi.ac.uk:5432/pfmegrnargs
```

That's the exact query in the demo above — a real `JOIN` + `GROUP BY` across a 216-table schema you've never seen. (Credentials above are EMBL-EBI's public read-only — published for tutorial use.)

**More to try** — swap the `--query` string for any of these. Same database, no schema knowledge required, each a different query shape — and each one verified to return real rows:

```text
which 10 source databases contribute the most RNA sequences          # GROUP BY over millions of rows (ENA, SILVA, Rfam …)
how many Rfam families are there in total                            # a one-number COUNT
list the 10 Rfam families with the longest consensus sequences, with their length
```

---

## How it works

```
question
   │
   ▼
┌───────────────────┐
│ TF-IDF (stemmed)  │  Microseconds. Free. Surfaces ~50 candidate tables
│ retriever         │  by lexical match on names, columns, and comments.
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│ LLM table         │  One small LLM call. Handles semantic mismatches
│ selector          │  TF-IDF cannot: "invoice" → `account_move`,
│ (cheap model)     │  "shipment" → `stock_picking`. Picks ~15 tables.
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│ FK-graph          │  One hop outward + inward to pick up join targets
│ expansion         │  the question didn't name explicitly. Cap at 25.
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│ SQL generator     │  Your real LLM call. Receives ~25 tables, not 675 —
│ (frontier model)  │  with column comments and every enum's legal values,
│                   │  so it filters on real states instead of guessing them.
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│ Safety guard      │  sqlglot validator: rejects anything that isn't a
│ (sqlglot)         │  pure SELECT/CTE/UNION. Catches CTEs that hide DML.
└────────┬──────────┘
         │
         ▼
   "Run? [y/N]" → execute against a read-only Postgres session
         │
         ▼
┌───────────────────┐
│ Self-repair       │  If the database rejects the query, the error message
│ (on error only)   │  goes back to the model for one corrected attempt
│                   │  (--max-repair). Repaired SQL is re-validated and
│                   │  re-confirmed before it runs. Empty results are never
│                   │  "repaired" — empty is often the right answer.
└───────────────────┘
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the deep dive (file inventory, design bets, the patent-landmine non-goals).

---

## Configuration

| Flag | Default | Description |
|---|---|---|
| `--model` | auto-detect | LLM for SQL generation (e.g. `gpt-4o`, `claude-sonnet-4-6`, `anthropic/claude-opus-4-7`) |
| `--selector-model` | same as `--model` | LLM for the table-selector step. **A cheaper model is recommended** (e.g. `gpt-4o-mini`) |
| `--top-k` | 50 | TF-IDF candidates passed to the LLM selector |
| `--select` | 15 | Tables the LLM selector picks from those candidates |
| `--max-tables` | 25 | Cap after FK expansion — what the SQL generator actually sees |
| `--max-repair` | 1 | Repair rounds when the database rejects a query (0 disables) |
| `--no-selector` | — | Skip the LLM selector (v0.1 behaviour: TF-IDF + FK only) |
| `-y, --yes` | — | Skip the confirmation prompt before running |

### Environment

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Use OpenAI as the LLM provider |
| `ANTHROPIC_API_KEY` | Use Anthropic as the LLM provider |

If both are set, Anthropic is preferred. Override either with `--model anthropic/<name>` or `--model openai/<name>`.

---

## Safety

PromptQuery has **two independent layers** so a write is impossible, even if one layer fails:

1. **Session-level**: every Postgres session opens with `default_transaction_read_only = on` and a 60-second `statement_timeout`. The database itself refuses non-SELECT operations.
2. **Pre-execution**: every generated query is parsed with `sqlglot` and rejected unless it's a single `SELECT` / `WITH` / `UNION` / `INTERSECT` / `EXCEPT`. The validator also catches CTEs that hide DML (`WITH x AS (DELETE …) SELECT * FROM x`) and dangerous-function calls (`pg_terminate_backend`, `set_config`, `lo_export`, `dblink_exec`).

Every query is also shown to you before it runs. Confirm with `y`.

---

## How it compares

PromptQuery is deliberately narrow. [Vanna](https://github.com/vanna-ai/vanna) and [WrenAI](https://github.com/Canner/WrenAI) are mature, popular, and good at what they do — a trainable chat library and a governed BI layer respectively. PromptQuery isn't trying to be either. It's for engineers who live in `psql` and want one correct, read-only query from the terminal, right now, without setting anything up.

| | **PromptQuery** | Vanna | WrenAI |
|---|---|---|---|
| Interface | CLI / REPL | Python library | Web app |
| Setup | connect & go — live schema introspection | train on your DDL/docs/SQL (vector store) | model a semantic layer (MDL) |
| Scale approach | FK-graph retrieval over 100s of tables | RAG over example SQL | semantic layer |
| Determinism | **temperature 0 by default** | model default | model default |
| Writes | **refused — two read-only layers** | your responsibility | your responsibility |
| Sweet spot | a correct read-only query from the terminal | embedding NL→SQL in an app | self-serve BI for analysts |
| License | Apache-2.0 | MIT | Apache-2.0 |

It's also not an IDE assistant (DataGrip AI and friends) — those live in your editor and write freely; PromptQuery lives in your terminal and won't write at all. *Details as of June 2026; corrections welcome via an issue.*

---

## Benchmark

The eval suite is part of the repo and reproducible:

```bash
# End-to-end (real Postgres + execution-equality scoring on the shop schema):
docker compose -f eval/docker-compose.yml up -d
PGPASSWORD=promptquery psql -h 127.0.0.1 -p 55432 -U promptquery -d shop \
    -f eval/fixtures/shop.sql \
    -f eval/fixtures/shop_seed.sql
python -m eval.end_to_end --model gpt-4o --pad 0 --pad 200

# Parsing-mode on Odoo 18 (675 tables):
python -m eval.parsing_bench \
    --fixture eval/fixtures/odoo.schema.json \
    --questions eval.questions.odoo \
    --model gpt-4o --selector-model gpt-4o-mini

# Parsing-mode on EMBL-EBI's public RNAcentral (216 tables, real biology data):
python -m eval.parsing_bench \
    --fixture eval/fixtures/rnacentral.schema.json \
    --questions eval.questions.rnacentral \
    --model gpt-4o --selector-model gpt-4o-mini
```

The committed [`eval/results_*.json`](eval/) files are receipts of every bench we've run — including unfavourable ones, on purpose.

See [`eval/END_TO_END.md`](eval/END_TO_END.md) for the harness internals.

---

## What PromptQuery does NOT do (yet)

- **No writes.** `SELECT` only, by design and by belt-and-suspenders.
- **Postgres only.** MySQL and SQLite are on the v0.4 roadmap.
- **One database at a time.** No multi-DB sessions.
- **No data visualisation.** Rows out, that's it. Pipe to `csv` / `jq` / your tool of choice.

---

## Roadmap

- **v0.2 (shipped)** — LLM-assisted table selector, stemmed TF-IDF.
- **v0.3 (shipped)** — enum-aware schema prompts, execution-guided self-repair (`--max-repair`),
  answer-shape generation rules. EX 58% → 72% on the 100-question benchmark.
- **v0.4** — value/literal linking (match question terms to actual cell values), opt-in
  multi-candidate generation (`--thorough`), window-function/composition work.
- **Later** — MySQL + SQLite adapters, MCP server mode, local LLMs (Ollama), schema anonymisation,
  query-history-as-few-shot.

---

## Development

```bash
git clone https://github.com/Cyberfilo/promptquery
cd promptquery
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev,openai]"

# Run the unit tests:
.venv/bin/pytest

# Run the retrieval eval (no API key needed, no DB needed):
.venv/bin/python -m eval.retrieval
```

66 tests, all pure-Python — no live database or API key required for the core suite.

---

## License

[Apache-2.0](LICENSE). Apache-2.0 was chosen over MIT specifically for its **explicit patent grant** and **automatic termination** clauses, which matter for a tool that operates in an active NL-to-SQL patent landscape.
