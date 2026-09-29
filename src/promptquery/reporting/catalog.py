"""Schema catalog = introspected reporting views + Atlas semantic layer, cached as JSON.

The `rpt` views carry no FK constraints, so relationships are read from the underlying
`public` tables (dropping the tenant_id half of Atlas' composite keys) and merged with the
semantic layer's "virtual" FKs for references Atlas stores without constraints (partner_id).
Status value lists come from Atlas' code (enum_values.json) because the columns are plain
VARCHAR with no CHECK constraints.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..atlas import DATA_DIR
from ..schema import Column, ForeignKey, Schema, Table
from .executor import ReportingExecutor

REPORTING_SCHEMA = "rpt"

# Atlas prefixes its tables by module. When semantic.yaml has no curated description for a table
# (e.g. one added by a later Atlas migration), fall back to the module domain so retrieval and
# the SQL writer still know the business area.
MODULE_DOMAINS = {
    "fin_": "Finance / accounting", "sales_": "Sales and customers",
    "inv_": "Inventory and items", "proc_": "Procurement and vendors",
    "mfg_": "Manufacturing / production", "hr_": "Human resources",
    "crm_": "CRM (leads, opportunities)", "ps_": "Projects", "qm_": "Quality management",
    "pm_": "Plant maintenance", "hsp_": "Hospitality (restaurant)", "core_": "Core / documents",
    "adm_": "Administration", "ind_": "Industry configuration",
}


def fallback_description(table: str) -> str | None:
    for prefix, domain in MODULE_DOMAINS.items():
        if table.startswith(prefix):
            subject = table[len(prefix):].replace("_", " ")
            return f"{domain}: {subject} (no curated description; infer from the column names)"
    return None

_COLUMNS_SQL = """
SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relkind = 'v' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY c.relname, a.attnum
"""

_BASE_NULLABILITY_SQL = """
SELECT c.relname, a.attname, NOT a.attnotnull
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
"""

_BASE_PK_SQL = """
SELECT cls.relname, att.attname
FROM pg_index i
JOIN pg_class cls ON cls.oid = i.indrelid
JOIN pg_namespace ns ON ns.oid = cls.relnamespace
JOIN pg_attribute att ON att.attrelid = cls.oid AND att.attnum = ANY(i.indkey)
WHERE i.indisprimary AND ns.nspname = 'public'
"""

_BASE_FKS_SQL = """
SELECT cls.relname, att.attname, fcls.relname, fatt.attname
FROM pg_constraint con
JOIN pg_class cls   ON cls.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = cls.relnamespace
JOIN pg_class fcls  ON fcls.oid = con.confrelid
JOIN LATERAL unnest(con.conkey)  WITH ORDINALITY AS k(attnum, ord)  ON TRUE
JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS fk(attnum, ord) ON fk.ord = k.ord
JOIN pg_attribute att  ON att.attrelid = con.conrelid  AND att.attnum  = k.attnum
JOIN pg_attribute fatt ON fatt.attrelid = con.confrelid AND fatt.attnum = fk.attnum
WHERE con.contype = 'f' AND ns.nspname = 'public'
ORDER BY cls.relname, con.conname, k.ord
"""


@dataclass
class Metric:
    name: str
    keywords: list[str]
    definition: str
    tables: list[str]


@dataclass
class SemanticLayer:
    global_rules: list[str] = field(default_factory=list)
    metrics: dict[str, Metric] = field(default_factory=dict)
    tables: dict[str, dict] = field(default_factory=dict)
    virtual_fks: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> "SemanticLayer":
        data = yaml.safe_load((path or DATA_DIR / "semantic.yaml").read_text())
        metrics = {
            name: Metric(name, [k.lower() for k in m.get("keywords", [])],
                         " ".join(m["definition"].split()), m.get("tables", []))
            for name, m in (data.get("metrics") or {}).items()
        }
        return cls(
            global_rules=data.get("global_rules", []),
            metrics=metrics,
            tables=data.get("tables", {}) or {},
            virtual_fks=data.get("virtual_foreign_keys", []) or [],
        )


def load_enum_values(path: Path | None = None) -> dict[str, list[str]]:
    p = path or DATA_DIR / "enum_values.json"
    return json.loads(p.read_text()) if p.is_file() else {}


def introspect_reporting_schema(executor: ReportingExecutor) -> Schema:
    """Build the raw Schema of the rpt views (columns + PKs + FKs, no semantics yet)."""
    col_rows = executor.scalar_rows(_COLUMNS_SQL, (REPORTING_SCHEMA,))
    base_nullable = {(t, c): n for t, c, n in executor.scalar_rows(_BASE_NULLABILITY_SQL)}
    base_pk = {(t, c) for t, c in executor.scalar_rows(_BASE_PK_SQL)}
    fk_rows = executor.scalar_rows(_BASE_FKS_SQL)

    tables: dict[str, Table] = {}
    for tname, cname, dtype, nullable in col_rows:
        base_t = "adm_tenants" if tname == "tenants" else tname
        t = tables.setdefault(tname, Table(schema="public", name=tname))
        t.columns.append(Column(
            name=cname, data_type=dtype,
            nullable=bool(base_nullable.get((base_t, cname), nullable)),
            is_primary_key=(base_t, cname) in base_pk and cname != "tenant_id",
        ))

    for src, col, ref, ref_col in fk_rows:
        if col == "tenant_id" or ref_col == "tenant_id":
            continue
        if src not in tables or ref not in tables:
            continue
        if not any(c.name == col for c in tables[src].columns):
            continue
        fk = ForeignKey(column=col, referenced_schema="public",
                        referenced_table=ref, referenced_column=ref_col)
        if fk not in tables[src].foreign_keys:
            tables[src].foreign_keys.append(fk)
    return Schema(tables=sorted(tables.values(), key=lambda t: t.name))


def apply_semantics(schema: Schema, semantic: SemanticLayer,
                    enum_values: dict[str, list[str]]) -> Schema:
    """Attach descriptions, column notes, status value lists and virtual FKs."""
    by_name = {t.name: t for t in schema.tables}
    for vfk in semantic.virtual_fks:
        t = by_name.get(vfk["table"])
        if t is None or vfk["ref_table"] not in by_name:
            continue
        fk = ForeignKey(column=vfk["column"], referenced_schema="public",
                        referenced_table=vfk["ref_table"], referenced_column=vfk["ref_column"])
        if fk not in t.foreign_keys:
            t.foreign_keys.append(fk)

    out: list[Table] = []
    for t in schema.tables:
        meta = semantic.tables.get(t.name, {})
        notes = meta.get("columns", {}) or {}
        cols = []
        for c in t.columns:
            values = enum_values.get(f"{t.name}.{c.name}")
            cols.append(Column(
                name=c.name, data_type=c.data_type, nullable=c.nullable,
                is_primary_key=c.is_primary_key,
                comment=notes.get(c.name) or c.comment,
                enum_values=tuple(values) if values else c.enum_values,
            ))
        out.append(Table(schema=t.schema, name=t.name,
                         comment=meta.get("description") or t.comment or fallback_description(t.name),
                         columns=cols, foreign_keys=list(t.foreign_keys)))
    return Schema(tables=out)


@dataclass
class Catalog:
    schema: Schema
    semantic: SemanticLayer

    def __post_init__(self) -> None:
        self._by_name = {t.name: t for t in self.schema.tables}

    def table(self, name: str) -> Table | None:
        return self._by_name.get(name.lower().removeprefix("rpt."))

    @property
    def table_names(self) -> set[str]:
        return set(self._by_name)


def load_catalog(executor: ReportingExecutor, cache: Path | None = None,
                 refresh: bool = False) -> Catalog:
    """Load the raw schema from the JSON cache (or introspect and write it), then apply the
    semantic layer. Semantics are always re-applied so YAML edits need no cache rebuild."""
    raw: Schema | None = None
    if cache is not None and cache.is_file() and not refresh:
        raw = Schema.from_dict(json.loads(cache.read_text()))
    if raw is None:
        raw = introspect_reporting_schema(executor)
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(raw.to_dict(), indent=1))
    semantic = SemanticLayer.load()
    return Catalog(apply_semantics(raw, semantic, load_enum_values()), semantic)
