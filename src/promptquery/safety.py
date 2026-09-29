from __future__ import annotations

import sqlglot
from sqlglot import exp


class UnsafeQuery(Exception):
    """Raised when a generated SQL statement is not a pure read-only SELECT."""


_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Command,
)

_ALLOWED_ROOTS: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
    exp.With,
    exp.Subquery,
)

# Postgres functions that can mutate state, escape the session, read the server filesystem,
# run arbitrary SQL strings or stall the server -- even from inside a SELECT. `dblink(...)`
# opens a NEW session that is not read-only, so every dblink_* entry point is blocked, not
# just dblink_exec.
_DANGEROUS_FUNCTIONS = {
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "pg_promote",
    "pg_switch_wal",
    "pg_create_restore_point",
    "lo_import",
    "lo_export",
    "lo_create",
    "lo_unlink",
    "lo_put",
    "lo_from_bytea",
    "set_config",
    "current_setting",
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "pg_ls_logdir",
    "pg_ls_waldir",
    "pg_notify",
    "pg_advisory_lock",
    "pg_advisory_xact_lock",
    "pg_try_advisory_lock",
    "pg_try_advisory_xact_lock",
    "query_to_xml",
    "query_to_xml_and_xmlschema",
    "query_to_xmlschema",
    "table_to_xml",
    "cursor_to_xml",
    "nextval",
    "setval",
    "txid_current",
    "pg_current_xact_id",
}
_DANGEROUS_PREFIXES = ("dblink", "lo_", "pg_logical_", "pg_replication_", "pg_file_")


def validate_select_only(sql: str) -> None:
    sql = (sql or "").strip()
    if not sql:
        raise UnsafeQuery("empty SQL")

    try:
        statements = sqlglot.parse(sql, read="postgres")
    except Exception as e:
        raise UnsafeQuery(f"could not parse SQL: {e}") from e

    statements = [s for s in statements if s is not None]
    if not statements:
        raise UnsafeQuery("empty SQL")
    if len(statements) > 1:
        raise UnsafeQuery("multiple statements are not allowed")

    stmt = statements[0]

    if not isinstance(stmt, _ALLOWED_ROOTS):
        raise UnsafeQuery(
            f"only SELECT queries are allowed (got {type(stmt).__name__})"
        )

    for forbidden in _FORBIDDEN_NODES:
        node = stmt.find(forbidden)
        if node is not None:
            raise UnsafeQuery(
                f"{type(node).__name__.upper()} statements are not allowed"
            )

    # SELECT ... INTO creates a table; FOR UPDATE/SHARE takes row locks.
    if stmt.find(exp.Into) is not None:
        raise UnsafeQuery("SELECT ... INTO is not allowed")
    if stmt.find(exp.Lock) is not None:
        raise UnsafeQuery("row-locking clauses (FOR UPDATE/SHARE) are not allowed")

    for name in function_names(stmt):
        if name in _DANGEROUS_FUNCTIONS or name.startswith(_DANGEROUS_PREFIXES):
            raise UnsafeQuery(f"function {name!r} is not allowed")


def function_names(stmt: exp.Expression) -> set[str]:
    """Lower-case names of every function call in the tree (typed and anonymous)."""
    names: set[str] = set()
    for func in stmt.find_all(exp.Anonymous):
        names.add((func.name or "").lower())
    for func in stmt.find_all(exp.Func):
        if isinstance(func, exp.Anonymous):
            continue
        names.add((func.sql_name() or "").lower())
    names.discard("")
    return names
