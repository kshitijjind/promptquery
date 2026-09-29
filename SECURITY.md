# Security Policy

PromptQuery generates SQL from natural language and executes it **read-only** against your database,
with two independent guards (a `sqlglot` validator and a `default_transaction_read_only` session).

## Reporting a vulnerability
Please report security issues privately via GitHub Security Advisories
("Report a vulnerability" on the Security tab) or by email to the maintainer — **not** a public issue.
We'll acknowledge within a few days.

## In scope (high interest)
- Any input that makes the safety guard accept a non-read-only statement (INSERT/UPDATE/DELETE/DDL,
  DML hidden in a CTE, or a dangerous function call).
- Any way a generated query can mutate data despite the read-only session.

If you find a `tests/test_safety.py`-style bypass, that's exactly what we want to hear about.
