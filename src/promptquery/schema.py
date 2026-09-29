from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    nullable: bool
    is_primary_key: bool
    comment: str | None = None
    # For enum-typed columns: every legal label, in enum order. The generator must see
    # these — an enum value it can't see is an enum value it will invent.
    enum_values: tuple[str, ...] | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "data_type": self.data_type,
            "nullable": self.nullable,
            "is_primary_key": self.is_primary_key,
            "comment": self.comment,
            "enum_values": list(self.enum_values) if self.enum_values else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Column":
        enum_values = data.get("enum_values")
        return cls(
            name=data["name"],
            data_type=data["data_type"],
            nullable=bool(data["nullable"]),
            is_primary_key=bool(data["is_primary_key"]),
            comment=data.get("comment"),
            enum_values=tuple(enum_values) if enum_values else None,
        )


@dataclass(frozen=True)
class ForeignKey:
    column: str
    referenced_schema: str
    referenced_table: str
    referenced_column: str

    def to_dict(self) -> dict:
        return {
            "column": self.column,
            "referenced_schema": self.referenced_schema,
            "referenced_table": self.referenced_table,
            "referenced_column": self.referenced_column,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ForeignKey":
        return cls(
            column=data["column"],
            referenced_schema=data["referenced_schema"],
            referenced_table=data["referenced_table"],
            referenced_column=data["referenced_column"],
        )


@dataclass
class Table:
    schema: str
    name: str
    comment: str | None = None
    columns: list[Column] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    @property
    def qualified_name(self) -> str:
        if self.schema == "public":
            return self.name
        return f"{self.schema}.{self.name}"

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "name": self.name,
            "comment": self.comment,
            "columns": [c.to_dict() for c in self.columns],
            "foreign_keys": [fk.to_dict() for fk in self.foreign_keys],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Table":
        return cls(
            schema=data["schema"],
            name=data["name"],
            comment=data.get("comment"),
            columns=[Column.from_dict(c) for c in data.get("columns", [])],
            foreign_keys=[ForeignKey.from_dict(fk) for fk in data.get("foreign_keys", [])],
        )


@dataclass
class Schema:
    tables: list[Table]

    def by_qualified_name(self) -> dict[str, Table]:
        return {t.qualified_name: t for t in self.tables}

    def to_dict(self) -> dict:
        return {"tables": [t.to_dict() for t in self.tables]}

    @classmethod
    def from_dict(cls, data: dict) -> "Schema":
        return cls(tables=[Table.from_dict(t) for t in data["tables"]])


INTROSPECT_TABLES = """
SELECT n.nspname AS schema,
       c.relname AS name,
       obj_description(c.oid) AS comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v', 'm', 'p', 'f')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'
  AND n.nspname NOT LIKE 'pg_temp%'
ORDER BY n.nspname, c.relname
"""

INTROSPECT_COLUMNS = """
SELECT n.nspname AS schema,
       c.relname AS table_name,
       a.attname AS name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       NOT a.attnotnull AS nullable,
       EXISTS (
           SELECT 1 FROM pg_index i
           WHERE i.indrelid = c.oid
             AND i.indisprimary
             AND a.attnum = ANY(i.indkey)
       ) AS is_primary_key,
       col_description(c.oid, a.attnum) AS comment,
       CASE WHEN t.typtype = 'e' THEN (
           SELECT array_agg(e.enumlabel ORDER BY e.enumsortorder)
           FROM pg_enum e WHERE e.enumtypid = t.oid
       ) END AS enum_values
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_type t ON t.oid = a.atttypid
WHERE a.attnum > 0
  AND NOT a.attisdropped
  AND c.relkind IN ('r', 'v', 'm', 'p', 'f')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'
  AND n.nspname NOT LIKE 'pg_temp%'
ORDER BY n.nspname, c.relname, a.attnum
"""

INTROSPECT_FKS = """
SELECT
    ns.nspname  AS schema,
    cls.relname AS table_name,
    att.attname AS column_name,
    fns.nspname AS referenced_schema,
    fcls.relname AS referenced_table,
    fatt.attname AS referenced_column
FROM pg_constraint con
JOIN pg_class cls         ON cls.oid = con.conrelid
JOIN pg_namespace ns      ON ns.oid = cls.relnamespace
JOIN pg_class fcls        ON fcls.oid = con.confrelid
JOIN pg_namespace fns     ON fns.oid = fcls.relnamespace
JOIN LATERAL unnest(con.conkey)  WITH ORDINALITY AS k(attnum, ord)  ON TRUE
JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS fk(attnum, ord) ON fk.ord = k.ord
JOIN pg_attribute att     ON att.attrelid = con.conrelid  AND att.attnum  = k.attnum
JOIN pg_attribute fatt    ON fatt.attrelid = con.confrelid AND fatt.attnum = fk.attnum
WHERE con.contype = 'f'
  AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY ns.nspname, cls.relname, con.conname, k.ord
"""


def introspect(db: "Database") -> Schema:
    tables_rows = db.fetch_dicts(INTROSPECT_TABLES)
    columns_rows = db.fetch_dicts(INTROSPECT_COLUMNS)
    fks_rows = db.fetch_dicts(INTROSPECT_FKS)

    tables: dict[tuple[str, str], Table] = {}
    for row in tables_rows:
        key = (row["schema"], row["name"])
        tables[key] = Table(
            schema=row["schema"],
            name=row["name"],
            comment=row["comment"],
        )

    for row in columns_rows:
        key = (row["schema"], row["table_name"])
        table = tables.get(key)
        if table is None:
            continue
        enum_values = row.get("enum_values")
        table.columns.append(Column(
            name=row["name"],
            data_type=row["data_type"],
            nullable=bool(row["nullable"]),
            is_primary_key=bool(row["is_primary_key"]),
            comment=row.get("comment"),
            enum_values=tuple(enum_values) if enum_values else None,
        ))

    for row in fks_rows:
        key = (row["schema"], row["table_name"])
        table = tables.get(key)
        if table is None:
            continue
        table.foreign_keys.append(ForeignKey(
            column=row["column_name"],
            referenced_schema=row["referenced_schema"],
            referenced_table=row["referenced_table"],
            referenced_column=row["referenced_column"],
        ))

    return Schema(tables=list(tables.values()))
