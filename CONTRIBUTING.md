# Contributing to PromptQuery

Thanks for considering a contribution. PromptQuery is small on purpose — PRs that keep it small,
honest, and Postgres-focused are the most welcome.

## Setup

```bash
git clone https://github.com/Cyberfilo/promptquery
cd promptquery
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev,openai]"
.venv/bin/pytest        # 49 tests — no database or API key needed
```

## Ground rules (these are load-bearing)

- **Never weaken the safety suite.** `tests/test_safety.py` encodes queries the validator *must*
  reject. Add cases when you find a new attack vector; don't delete them in a refactor.
- **Both read-only layers stay.** The Postgres session (`default_transaction_read_only = on`) *and*
  the `sqlglot` guard are both load-bearing. Removing either is a regression even if tests pass.
- **Honesty in benchmarks.** If you touch the eval, commit the receipts — including the unfavourable
  ones. Don't benchmark on toy schemas to inflate a number; the value here is *real* schemas.
- **Stay dependency-light.** New runtime dependencies need a good reason.

## Good first issues

See the [`good first issue`](https://github.com/Cyberfilo/promptquery/labels/good%20first%20issue)
label. A few well-scoped ones from the roadmap:

- A `--temperature` flag (default `0`) to make determinism configurable.
- An `ollama` / local-LLM provider in `llm.py`.
- Schema-anonymisation mode (`--anonymize`) so prompts can be shared without leaking table/column names.
- MySQL / SQLite schema adapters (needs a small adapter abstraction in `schema.py` first).

## Opening a PR

Run `pytest` first. Keep PRs small and focused, and say what changed and why. If it changes behaviour,
update the README in the same PR.

## Branching & releases
- **`main`** is protected: CI (lint + tests + build) must pass and changes land via PR; no
  force-pushes or deletions. Releases are tagged from `main` (a `v*` tag triggers the PyPI publish).
- **`staging`** is the integration branch. Branch features off `staging`, PR into `staging` (CI runs
  on both `main` and `staging`), then promote `staging` → `main` via PR for a release.
- Quick flow: `git switch staging && git pull && git switch -c my-feature` → push → PR to `staging`.
