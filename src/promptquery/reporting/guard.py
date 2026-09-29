"""Static SQL guard: safety + schema-aware validation + row cap, before anything executes.

Layers (the DB role/views are the last line of defence; this catches problems earlier and
produces precise errors the repair loop can act on):
1. `safety.validate_select_only`: one read-only statement, no DML/DDL anywhere in the tree,
   no dangerous functions, no SELECT INTO / FOR UPDATE.
2. Table allow-list: every table must be a reporting view (or a CTE); no schema-qualified
   access to public / pg_catalog / information_schema.
3. Function allow-list for functions sqlglot does not model natively.
4. Row cap: a LIMIT is added to the outermost query when it has none.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from ..safety import UnsafeQuery, validate_select_only
from .catalog import Catalog

# Functions sqlglot parses as `Anonymous` that are safe for reporting. Anything else that is
# not a natively-modelled SQL function is rejected (and reported back for repair).
SAFE_ANONYMOUS_FUNCTIONS = {
    "date_part", "date_trunc", "to_char", "to_date", "to_number", "to_timestamp", "age",
    "make_date", "make_interval", "justify_days", "justify_interval", "extract", "isfinite",
    "generate_series", "string_agg", "array_agg", "array_to_string", "unnest",
    "initcap", "split_part", "regexp_replace", "regexp_match", "btrim", "ltrim", "rtrim",
    "lpad", "rpad", "left", "right", "strpos", "position", "format", "concat_ws", "concat",
    "reverse", "translate", "md5", "chr", "ascii", "length", "char_length", "octet_length",
    "nullif", "coalesce", "greatest", "least", "abs", "ceil", "ceiling", "floor", "round",
    "trunc", "sign", "mod", "power", "sqrt", "exp", "ln", "log", "div", "width_bucket",
    "percentile_cont", "percentile_disc", "mode", "stddev", "stddev_pop", "stddev_samp",
    "variance", "var_pop", "var_samp", "corr", "covar_pop", "covar_samp", "regr_slope",
    "bool_and", "bool_or", "every", "count", "sum", "avg", "min", "max",
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile", "lag", "lead",
    "first_value", "last_value", "nth_value",
    "json_build_object", "jsonb_build_object", "json_agg", "jsonb_agg", "to_json", "to_jsonb",
    "json_object_agg", "jsonb_object_agg", "jsonb_extract_path_text", "json_extract_path_text",
    "now", "current_date", "localtimestamp", "date", "timezone", "cast", "numeric",
    "array_length", "cardinality", "array_position", "upper", "lower", "trim", "substring",
    "substr", "replace", "starts_with", "encode", "decode", "string_to_array", "array_remove",
    "date_bin", "extract_epoch",
}

DISALLOWED_SCHEMAS = {"public", "pg_catalog", "information_schema", "pg_toast"}


class GuardError(ValueError):
    """The SQL was rejected; `str(e)` is written for the repair model."""


@dataclass
class GuardedSQL:
    sql: str           # SQL that will be executed (possibly with an added LIMIT)
    tables: list[str]  # reporting views referenced


def check_sql(sql: str, catalog: Catalog, max_rows: int = 1000) -> GuardedSQL:
    try:
        validate_select_only(sql)
    except UnsafeQuery as e:
        raise GuardError(f"unsafe or invalid query: {e}") from e

    tree = sqlglot.parse_one(sql, read="postgres")

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables: list[str] = []
    for t in tree.find_all(exp.Table):
        name = (t.name or "").lower()
        db = (t.db or "").lower()
        if not name:
            continue  # e.g. table-valued functions
        if db and db != "rpt":
            if db in DISALLOWED_SCHEMAS:
                raise GuardError(f"access to schema '{db}' is not allowed; use the bare table name")
            raise GuardError(f"unknown schema '{db}'; reference tables by bare name")
        if name in cte_names and not db:
            continue
        if catalog.table(name) is None:
            close = difflib.get_close_matches(name, sorted(catalog.table_names), n=3, cutoff=0.5)
            hint = f"; did you mean {', '.join(close)}?" if close else ""
            raise GuardError(f"table '{name}' does not exist{hint}")
        if name not in tables:
            tables.append(name)

    # Natively modelled functions are standard SQL (dangerous ones were denied above);
    # anything sqlglot does not model must be on the allow-list.
    for func in tree.find_all(exp.Anonymous):
        name = (func.name or "").lower()
        if name and name not in SAFE_ANONYMOUS_FUNCTIONS:
            raise GuardError(f"function '{name}' is not allowed in reporting queries")

    return GuardedSQL(add_limit(tree, sql, max_rows), tables)


def add_limit(tree: exp.Expression, original_sql: str, max_rows: int) -> str:
    """Append LIMIT max_rows+1 to the outermost query if it has no LIMIT (the executor fetches
    at most max_rows and uses the extra row to detect truncation)."""
    target = tree
    if isinstance(target, exp.Subquery):
        return original_sql
    if target.args.get("limit") is not None or target.args.get("fetch") is not None:
        return original_sql
    if not isinstance(target, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        return original_sql
    return f"{original_sql.rstrip().rstrip(';')}\nLIMIT {max_rows + 1}"
