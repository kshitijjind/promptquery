# Changelog

## 0.3.0 — 2026-06-10

Generation-quality release. The retrieval side has been measuring at 98–100% recall on a 211-table
benchmark for a while; the misses were in the SQL itself. This release attacks the three error
classes that benchmarking surfaced.

**Measured result** (100-question NL→SQL suite, 211-table Postgres schema, gpt-4o, temperature 0,
single-state execution accuracy, same harness and conditions as the 0.2.2 run): **EX 58% → 72%**,
hard errors **7 → 0**, row-level Soft-F1 60.2 → 73.9, Set-Recall 98% → 99%. Cost of the new
context: +10% prompt tokens (4,257 → 4,689 per query) and ~+200 ms per query for repair rounds
on failing queries.

- **Enum-aware schema prompts.** The schema sent to the model now includes column comments and the
  full legal value list of every enum column (read from `pg_catalog`, cached with the schema). The
  single biggest failure class in our benchmarking was the model inventing enum values
  (`'overdue'`, `'churned'`) or dodging a status column it couldn't see into timestamp guesses
  (`delivered_at IS NOT NULL` instead of `status = 'delivered'`). Now it sees the real vocabulary.
  (`src/promptquery/schema.py`, `src/promptquery/prompts.py`)
- **Execution-guided self-repair.** When the database rejects a generated query, the SQL plus the
  database's own error message go back to the model for a corrected attempt — `--max-repair` rounds,
  default 1, `0` disables. Repaired SQL passes through the same sqlglot safety validator (and the
  REPL confirm prompt) before it ever runs. Empty results deliberately do *not* trigger repair: an
  empty result is often the correct answer, and "fixing" it risks replacing a right answer with a
  wrong one. (`src/promptquery/repair.py`, new)
- **Tighter generation rules.** The system prompt now pins down answer shape: return exactly the
  columns asked for, no speculative filters (`deleted_at IS NULL` nobody asked about), INNER JOIN
  unless the question implies otherwise, and filter state via the status column when one exists.
  (`src/promptquery/prompts.py`)
- **Fix:** bare `o4-*` model names now infer the OpenAI provider, matching `o1`/`o3`
  (`--model o4-mini` works without the `openai/` prefix). (`src/promptquery/llm.py`)
- **Tests** 48 → 66: repair-loop behavior (including "unsafe repairs never execute" and
  "declined repairs never run"), enum serialization round-trips, provider inference.
- **Docs**: ARCHITECTURE.md pipeline updated for the repair stage and enum-aware prompts; stale
  v0.1-era claims (embeddings "queued for v0.2", old test counts) reconciled with reality.

## 0.2.2 — 2026-06-04

- **Deterministic by default.** SQL generation now runs at `temperature = 0` (plus a fixed `seed` on
  standard OpenAI chat models). Ask the same question twice, get the same SQL. Reasoning models
  (gpt-5 / o-series) are left to sample on their own, since they reject those parameters.
  (`src/promptquery/llm.py`)
- **Honest token benchmark.** New `eval/token_bench.py` measures the SQL-generator prompt size with
  `tiktoken` and commits the receipts (`eval/results_odoo_tokens.json`,
  `eval/results_rnacentral_tokens.json`). The README now reports the *measured* numbers — **~12–17×**
  fewer tokens than stuffing the whole schema into the prompt (previously under-stated as "5–10×").
- **README**: spelled out exactly what the benchmark "accuracy" measures (table-grounding, parsed
  with `sqlglot` — not execution-equality), added an honest comparison table, and added a
  `CONTRIBUTING.md`.
- **Docs**: fixed a stale "MIT" reference (the project is Apache-2.0).

## 0.2.1 — 2026-05-27

- Ship the `pquery` command alias on PyPI (third alias alongside `promptquery` and `prq`).

## 0.2.0 — 2026-05-27

- LLM-assisted table selector (TF-IDF top-50 → cheap-model selector → FK-graph expansion).
- Full end-to-end eval suite with committed receipts.
