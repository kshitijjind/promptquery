# PromptQuery — Architecture

This document explains how PromptQuery is structured internally. It's written for contributors and the curious. For usage, see the [README](README.md).

## The request lifecycle

When you type a question into the REPL, here is what happens:

```
question
   │
   ▼
┌──────────────────┐
│ retrieval.py     │  Rank tables by TF-IDF over their name, comment, column
│ TfIdfRetriever   │  names, and FK targets. Question is tokenized with
│                  │  snake_case + camelCase splitting and stopword filtering.
└────────┬─────────┘  Returns top-K tables.
         │
         ▼
┌──────────────────┐
│ retrieval.py     │  Walk the FK graph one hop outward (referenced tables)
│ expand_via_fks   │  AND one hop inward (tables that reference the seed).
│                  │  Capped at --max-tables to keep prompts compact.
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ prompts.py       │  Render the chosen tables into the system prompt:
│ format_schema    │  TABLE name, columns with PK/NOT NULL flags, column
│                  │  comments, the legal values of every enum column, FKs.
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ llm.py           │  Send to the configured provider (Anthropic by default,
│ Client.generate  │  OpenAI as fallback). Response is a markdown ```sql block.
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ llm.extract_sql  │  Pull the SQL out of the code fence.
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ safety.py        │  Parse with sqlglot in Postgres dialect. Reject anything
│ validate_select  │  that is not a single SELECT / WITH / UNION /
│ _only            │  INTERSECT / EXCEPT. Reject CTEs that hide DML
│                  │  (WITH x AS (DELETE ... RETURNING ...) SELECT ...).
│                  │  Reject calls to dangerous functions
│                  │  (pg_terminate_backend, set_config, lo_export, ...).
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ render.py        │  Pretty-print the SQL with syntax highlighting.
│ render_sql       │
└────────┬─────────┘
         │
         ▼
   "Run? [y/N]"
         │
         ▼
┌──────────────────┐
│ db.py            │  Execute. The Postgres session was opened with
│ Database.execute │  default_transaction_read_only = on and a 60s
│                  │  statement_timeout, so even if safety.py failed
│                  │  the database itself would refuse a write.
└────────┬─────────┘
         │ on a database error: repair.py feeds the failed SQL plus the
         │ database's own error message back to the model for up to
         │ --max-repair rounds (default 1). Every repaired query goes
         │ through validate_select_only — and the confirm prompt, in the
         │ REPL — before it is executed. Empty results never trigger a
         │ repair: an empty result is often the right answer.
         ▼
┌──────────────────┐
│ render.py        │  Format as a rich.Table. NULLs styled, large blobs
│ render_results   │  summarized, truncate after 100 rows by default.
└──────────────────┘
```

## File inventory (`src/promptquery/`)

| File              | Purpose                                                                   |
|-------------------|---------------------------------------------------------------------------|
| `__init__.py`     | Package version.                                                          |
| `__main__.py`     | `python -m promptquery` entry point.                                      |
| `cli.py`          | Click command and prompt-toolkit REPL. Orchestrates the whole pipeline.   |
| `db.py`           | psycopg3 connection wrapper. Sets the read-only session.                  |
| `schema.py`       | Dataclasses + the `pg_catalog` queries that introspect them.              |
| `retrieval.py`    | Tokenizer, TF-IDF ranker, FK-graph expander.                              |
| `llm.py`          | Provider clients (Anthropic, OpenAI), SQL extractor, provider factory.    |
| `prompts.py`      | System prompt template and schema-to-prompt formatter.                    |
| `repair.py`       | Execution-guided repair: bounded retry loop fed by the database's errors. |
| `safety.py`       | The sqlglot-based query guard.                                            |
| `render.py`       | SQL syntax rendering and result-table rendering with rich.                |

## File relationships

- **`schema.py` ↔ `retrieval.py`** — the ranker reads `Table.name`, `Table.comment`, `Table.columns`, and `Table.foreign_keys`. Adding a field to `Table` should prompt a decision about weighting it in `_table_terms`.
- **`schema.py` ↔ `prompts.py`** — `format_schema` walks the same dataclasses. Schema additions usually need a prompt update.
- **`cli.py` ↔ everything else** — the only file that knows the full pipeline. New stages (query history, post-execution feedback) wire in here.
- **`safety.py` ↔ `llm.py`** — `extract_sql` runs first; `validate_select_only` runs second. Together they handle the case where the model returns malformed output.
- **`db.py` ↔ `safety.py`** — two layers, intentionally redundant. Either alone is insufficient; both together make a write impossible.
- **`repair.py` ↔ `safety.py`** — every repaired query is re-validated before execution. The repair loop widens what the model can fix; it never widens what can run.

## Design bets

### Why TF-IDF, not embeddings (yet)

TF-IDF works the moment you connect to a database. No model to download, no GPU, no API call to compute embeddings for hundreds of tables. The cost is that it cannot reason about synonyms — "customer" and "user" are different tokens to TF-IDF. Since 0.2 the LLM table-selector covers that gap (it sees the TF-IDF candidates and picks semantically), and measured retrieval recall on a 211-table benchmark sits at 98–100% without embeddings. Embeddings stay off the default path until the data shows a gap they would close.

### Why a separate FK-expansion pass

Many natural-language questions reference one core entity but require joins through several others. Asking "what's the average order value per country" needs `orders` (the main entity), but also `users` and `countries` (join targets). TF-IDF alone is unlikely to rank `countries` highly because the word "country" only appears once in its schema. The FK-graph walk catches join targets that pure relevance would miss.

### Why two safety layers

`safety.py` is the primary guard. It parses every statement and rejects anything other than a SELECT. The Postgres session-level `default_transaction_read_only = on` is the fallback: if a malicious prompt somehow produces SQL that the validator misclassifies (a parser bug, an unknown construct), Postgres itself refuses the write.

This redundancy is not paranoia. AI-generated SQL is, by construction, less predictable than human-written SQL. The cost of one accidental `DELETE` is high enough that doubling up is the only sensible default.

### Why introspect via `pg_catalog` instead of `information_schema`

`pg_catalog` is faster, more complete, and lets us read table comments via `obj_description`. We use `LATERAL unnest WITH ORDINALITY` to join `pg_constraint.conkey` with `pg_constraint.confkey` by ordinal position — the only correct way to handle composite foreign keys.

## What's intentionally not here

These are out of scope for now. They are tracked in the [roadmap](README.md#roadmap).

- MySQL / SQLite support — needs an adapter abstraction first.
- Multi-database sessions in one REPL.
- Data visualization (charts, plots).
- Query-history persistence between sessions.
- Embedding-based retrieval.
- MCP server mode.

## Testing

Run the test suite:

```bash
pytest
```

All tests are pure Python — no live database or API key required. The repair loop, prompt serialization, safety guard, retrieval, and CLI outcome logic are covered with in-memory fakes.

The most safety-critical file is `tests/test_safety.py`. Cases there encode "things the validator MUST reject." Add cases when you discover new attack vectors; do not delete cases during refactors.

## Project layout

```
PromptQuery/
├── ARCHITECTURE.md          this file
├── LICENSE                  Apache-2.0
├── README.md                user-facing intro and install/usage
├── pyproject.toml           hatchling, scripts: promptquery + prq
├── src/promptquery/         the package
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── db.py
│   ├── schema.py
│   ├── retrieval.py
│   ├── llm.py
│   ├── prompts.py
│   ├── safety.py
│   └── render.py
└── tests/                   pytest suite
    ├── test_safety.py
    └── test_retrieval.py
```

## Contributing

PRs welcome. Run `pytest` before opening one. Keep the safety test suite untouched unless you're adding cases.
