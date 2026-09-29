"""Generate synthetic noise tables to inflate a schema for scaling tests.

We DO NOT load these into the live database. They exist purely to bloat the
schema dict that goes into the LLM prompt (the naive pipeline) and into the
retriever (the PromptQuery pipeline). Tables we actually query against
remain the original `shop` tables.
"""
from __future__ import annotations

import hashlib
import random

from promptquery.schema import Column, Schema, Table


# Realistic-sounding prefixes/suffixes for plausible-looking noise tables.
_NOUNS = [
    "event", "log", "metric", "snapshot", "archive", "report", "feed",
    "queue", "job", "task", "lock", "cache", "blob", "key", "token",
    "session", "request", "trace", "span", "alert", "incident",
    "config", "flag", "setting", "preference", "permission", "role",
    "campaign", "experiment", "variant", "audit", "notification",
    "webhook", "import", "export", "batch", "pipeline", "stream",
]
_QUALIFIERS = [
    "raw", "staging", "tmp", "old", "v2", "legacy", "deprecated",
    "internal", "external", "public", "test", "qa", "demo", "sandbox",
    "shadow", "mirror", "replica", "backup",
]
_PERIODS = [
    "daily", "hourly", "weekly", "monthly", "quarterly", "yearly",
    "2019", "2020", "2021", "2022", "2023_q1", "2023_q2",
    "2023_q3", "2023_q4", "2024", "2025",
]


def _gen_table_name(rng: random.Random, used: set[str]) -> str:
    """Generate a plausible-but-irrelevant table name not yet used."""
    for _ in range(200):
        parts = [rng.choice(_NOUNS)]
        if rng.random() < 0.55:
            parts.insert(0, rng.choice(_QUALIFIERS))
        if rng.random() < 0.35:
            parts.append(rng.choice(_PERIODS))
        name = "_".join(parts)
        if name not in used:
            return name
    # fallback: hash-based unique
    h = hashlib.sha1(str(rng.random()).encode()).hexdigest()[:8]
    return f"noise_{h}"


def _gen_columns(rng: random.Random) -> list[Column]:
    n = rng.randint(3, 8)
    cols = [Column("id", "bigint", False, True)]
    type_pool = ["text", "integer", "bigint", "numeric(10,2)",
                 "timestamp with time zone", "boolean", "jsonb"]
    used_names = {"id"}
    for _ in range(n - 1):
        for _ in range(20):
            name = f"col_{rng.randint(1, 999)}"
            if name not in used_names:
                used_names.add(name)
                break
        cols.append(Column(
            name=name,
            data_type=rng.choice(type_pool),
            nullable=rng.random() < 0.6,
            is_primary_key=False,
        ))
    return cols


def pad_schema(base: Schema, extra: int, seed: int = 42) -> Schema:
    """Return a new Schema with `extra` synthetic noise tables appended."""
    if extra <= 0:
        return base
    rng = random.Random(seed)
    used = {t.name for t in base.tables}
    noise_tables: list[Table] = []
    for _ in range(extra):
        name = _gen_table_name(rng, used)
        used.add(name)
        noise_tables.append(Table(
            schema="public",
            name=name,
            comment=None,
            columns=_gen_columns(rng),
            foreign_keys=[],
        ))
    return Schema(tables=list(base.tables) + noise_tables)
