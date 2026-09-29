"""Read-only, tenant-scoped query execution against the `rpt` reporting views.

Every query runs on a fresh connection as the `atlas_reporting` role, inside a READ ONLY
transaction with a statement timeout, with the tenant scope set by this process (never by
generated SQL), and is always rolled back. Rows are fetched with a hard cap.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import psycopg

TENANT_GUC = "atlas.tenant_ids"


class TenantError(ValueError):
    pass


@dataclass
class QueryOutput:
    columns: list[str]
    rows: list[tuple]
    truncated: bool


class ReportingExecutor:
    def __init__(self, dsn: str, tenant: str = "acme", statement_timeout_ms: int = 20000):
        self.dsn = dsn
        self.statement_timeout_ms = statement_timeout_ms
        self.tenants = self._resolve_tenants(tenant)

    # -- tenancy ---------------------------------------------------------------
    def directory(self) -> list[dict]:
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT id::text, slug, name FROM rpt.tenant_directory() ORDER BY slug")
            return [{"id": i, "slug": s, "name": n} for i, s, n in cur.fetchall()]

    def _resolve_tenants(self, spec: str) -> list[dict]:
        directory = self.directory()
        if spec.strip().lower() in {"all", "*"}:
            return directory
        wanted = [s.strip().lower() for s in spec.split(",") if s.strip()]
        by_slug = {t["slug"].lower(): t for t in directory}
        missing = [w for w in wanted if w not in by_slug]
        if missing or not wanted:
            raise TenantError(
                f"unknown tenant(s) {missing or spec!r}; available: "
                + ", ".join(sorted(by_slug)))
        return [by_slug[w] for w in wanted]

    def set_tenant(self, spec: str) -> None:
        self.tenants = self._resolve_tenants(spec)

    @property
    def tenant_label(self) -> str:
        return ", ".join(t["slug"] for t in self.tenants)

    # -- execution -------------------------------------------------------------
    def _session(self) -> psycopg.Connection:
        conn = psycopg.connect(self.dsn, autocommit=False)
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('statement_timeout', %s, true)",
                        (str(self.statement_timeout_ms),))
            cur.execute("SELECT set_config('search_path', 'rpt', true)")
            cur.execute("SELECT set_config(%s, %s, true)",
                        (TENANT_GUC, ",".join(t["id"] for t in self.tenants)))
        return conn

    def explain(self, sql: str) -> None:
        """Plan the query without running it: surfaces unknown tables/columns and type
        errors cheaply, before execution."""
        with self._session() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("EXPLAIN " + sql)
            finally:
                conn.rollback()

    def run(self, sql: str, max_rows: int = 1000) -> QueryOutput:
        with self._session() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    if cur.description is None:
                        return QueryOutput([], [], False)
                    cols = [d.name for d in cur.description]
                    rows = cur.fetchmany(max_rows + 1)
            finally:
                conn.rollback()
        truncated = len(rows) > max_rows
        return QueryOutput(cols, [tuple(r) for r in rows[:max_rows]], truncated)

    def scalar_rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Internal trusted queries (data profiling). Never used for generated SQL."""
        with self._session() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    return list(cur.fetchall())
            finally:
                conn.rollback()


def to_jsonable(value):
    if isinstance(value, Decimal):
        # Keep money exact-ish but JSON friendly.
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return "<binary>"
    return value
