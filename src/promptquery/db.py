from __future__ import annotations

import psycopg
from psycopg.rows import dict_row


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.conn: psycopg.Connection | None = None

    def connect(self) -> None:
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        with self.conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
            cur.execute("SET statement_timeout = '60s'")

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _require_conn(self) -> psycopg.Connection:
        if self.conn is None:
            raise RuntimeError("Database is not connected")
        return self.conn

    def fetch_dicts(self, sql: str) -> list[dict]:
        conn = self._require_conn()
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            return list(cur.fetchall())

    def execute(self, sql: str) -> tuple[list[str], list[tuple]]:
        conn = self._require_conn()
        with conn.cursor() as cur:
            cur.execute(sql)
            if cur.description is None:
                return [], []
            cols = [d.name for d in cur.description]
            rows = list(cur.fetchall())
        return cols, rows

    def __enter__(self) -> "Database":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
