"""Provision the database-side guard rails the reporting bot runs behind.

Creates, idempotently, using an owner/admin connection:

* schema ``rpt`` holding one ``security_barrier`` view per reportable Atlas table. Each view
  - drops ``tenant_id`` and every masked/sensitive column (Atlas' own D-009/D-052/D-059 policy),
  - filters rows to the tenants listed in the session GUC ``atlas.tenant_ids``. When the GUC is
    unset the view returns nothing, so a misconfigured session fails closed.
* role ``atlas_reporting`` (LOGIN) that can only ``SELECT`` from ``rpt``, with a read-only
  default transaction mode, a statement timeout and ``search_path = rpt``.

Atlas enforces tenancy only in its ORM session (core/tenancy.py) and has no row-level security,
so without this layer a raw SQL query would read every tenant's data. With it, tenant isolation
and column masking are guaranteed by Postgres, whatever SQL the model writes.
"""
from __future__ import annotations

import psycopg
from psycopg import sql

REPORTING_SCHEMA = "rpt"
REPORTING_ROLE = "atlas_reporting"
TENANT_GUC = "atlas.tenant_ids"

# Tables that are never reportable: auth/session secrets, infrastructure and bookkeeping.
EXCLUDED_TABLES = {
    "alembic_version",
    "adm_tenants",            # exposed separately as rpt.tenants (id, slug, name only)
    "adm_tenant_settings",
    "core_users",             # password hashes, token versions
    "core_roles",
    "core_permissions",
    "core_role_permissions",
    "core_user_roles",
    "core_refresh_sessions",
    "core_api_keys",
    "core_audit_log",
    "core_idempotency_keys",
    "core_jobs",
    "core_number_sequences",
    "core_number_sequence_counters",
    "core_custom_field_defs",
    "ind_tenant_industry_configs",
    "hsp_reservation_settings",
    "hr_payroll_run_lines",   # per-employee pay = compensation, masked in Atlas
}

# Masked/sensitive columns (mirrors atlas-erp reporting/report_registry.py and hr/constants.py).
MASKED_COLUMNS = {
    "hr_employees": {"base_salary", "national_id", "tax_id", "date_of_birth",
                     "bank_account", "currency_code"},
}

# DEFAULT-DENY for personal and secret data. Table discovery is a deny-list (a new Atlas table
# with tenant_id becomes reportable automatically), so a migration that adds, say,
# hr_bank_details.iban would expose it. Any column whose name matches one of these fragments is
# masked on EVERY table unless it is listed in SENSITIVE_ALLOWED below. This makes the failure
# mode "a harmless column got hidden" instead of "personal data leaked".
SENSITIVE_COLUMN_FRAGMENTS = (
    "salary", "wage", "compensation", "payslip",
    "iban", "bank_account", "account_number", "routing_number", "swift", "bic",
    "credit_card", "card_number", "cvv", "cardholder",
    "ssn", "social_security", "national_id", "tax_id", "passport", "driver_license",
    "date_of_birth", "birth_date", "dob",
    "password", "secret", "token", "api_key", "key_hash", "private_key", "credential",
    "otp", "mfa", "session_id", "recovery_code",
)

# Columns whose NAME matches a fragment above but which are provably not sensitive: opaque UUID
# references to a record, not the secret itself. Reviewed individually; everything else is masked.
SENSITIVE_ALLOWED = {
    "fin_bank_statements.bank_account_id",
    "fin_customer_receipts.bank_account_id",
    "fin_vendor_payments.bank_account_id",
}

# Reference/lookup tables that legitimately have no tenant_id and are safe to share across
# tenants. They get an UNFILTERED view (no tenant predicate). Add only non-sensitive, global
# reference data here; anything tenant-owned must carry tenant_id and be filtered.
GLOBAL_TABLES: set[str] = set()

# Columns never exposed on any table.
ALWAYS_HIDDEN = {"tenant_id"}


def is_sensitive(table: str, column: str) -> bool:
    """True when a column must be masked by the default-deny name policy."""
    if f"{table}.{column}" in SENSITIVE_ALLOWED:
        return False
    lowered = column.lower()
    return any(frag in lowered for frag in SENSITIVE_COLUMN_FRAGMENTS)


def _reportable_tables(cur) -> dict[str, list[str]]:
    cur.execute(
        """
        SELECT c.table_name, array_agg(c.column_name::text ORDER BY c.ordinal_position)
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
        GROUP BY c.table_name
        HAVING bool_or(c.column_name = 'tenant_id')
        ORDER BY c.table_name
        """
    )
    out: dict[str, list[str]] = {}
    for table, cols in cur.fetchall():
        if table in EXCLUDED_TABLES:
            continue
        masked = MASKED_COLUMNS.get(table, set())
        out[table] = [c for c in cols if c not in ALWAYS_HIDDEN and c not in masked
                      and not is_sensitive(table, c)]
    return out


def _global_tables(cur) -> dict[str, list[str]]:
    """Reference tables explicitly allowed to be shared across tenants (no tenant_id)."""
    if not GLOBAL_TABLES:
        return {}
    cur.execute(
        """
        SELECT c.table_name, array_agg(c.column_name::text ORDER BY c.ordinal_position)
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
          AND c.table_name = ANY(%s)
        GROUP BY c.table_name
        HAVING NOT bool_or(c.column_name = 'tenant_id')
        """,
        (sorted(GLOBAL_TABLES),),
    )
    return {t: [c for c in cols if not is_sensitive(t, c)] for t, cols in cur.fetchall()}


def drop_reporting(admin_dsn: str, drop_role: bool = False) -> None:
    """Remove the whole `rpt` schema (and optionally the role).

    The views depend on Atlas' tables, so an Atlas migration that ALTERs or DROPs a column fails
    while they exist ("cannot drop column ... because other objects depend on it"). Call this
    before running Atlas migrations, then `setup_reporting` again afterwards.
    """
    with psycopg.connect(admin_dsn, autocommit=True) as conn, conn.cursor() as cur:
        role = sql.Identifier(REPORTING_ROLE)
        cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(REPORTING_SCHEMA)))
        if drop_role:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (REPORTING_ROLE,))
            if cur.fetchone() is not None:
                cur.execute("SELECT current_database()")
                (dbname,) = cur.fetchone()
                cur.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(
                    sql.Identifier(dbname), role))
                cur.execute(sql.SQL("DROP ROLE {}").format(role))


def setup_reporting(admin_dsn: str, reporting_password: str, statement_timeout: str = "20s") -> list[str]:
    """Create/refresh the ``rpt`` schema, views and read-only role. Returns the view names."""
    with psycopg.connect(admin_dsn, autocommit=False) as conn, conn.cursor() as cur:
        schema = sql.Identifier(REPORTING_SCHEMA)
        role = sql.Identifier(REPORTING_ROLE)

        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema))
        # Tenant scope helper: parses the GUC; unset/empty -> empty array -> no rows.
        cur.execute(sql.SQL(
            """
            CREATE OR REPLACE FUNCTION {}.scope_tenant_ids() RETURNS uuid[]
            LANGUAGE sql STABLE AS $$
              SELECT COALESCE(
                string_to_array(NULLIF(current_setting({guc}, true), ''), ',')::uuid[],
                ARRAY[]::uuid[])
            $$
            """
        ).format(schema, guc=sql.Literal(TENANT_GUC)))

        tables = _reportable_tables(cur)
        globals_ = _global_tables(cur)
        # Drop views that are no longer reportable (e.g. a table newly excluded).
        cur.execute(
            "SELECT table_name FROM information_schema.views WHERE table_schema = %s",
            (REPORTING_SCHEMA,),
        )
        for (existing,) in cur.fetchall():
            if existing not in tables and existing not in globals_ and existing != "tenants":
                cur.execute(sql.SQL("DROP VIEW {}.{}").format(schema, sql.Identifier(existing)))

        for table, cols in tables.items():
            cur.execute(sql.SQL("DROP VIEW IF EXISTS {}.{}").format(schema, sql.Identifier(table)))
            cur.execute(sql.SQL(
                "CREATE VIEW {}.{} WITH (security_barrier) AS "
                "SELECT {} FROM public.{} WHERE tenant_id = ANY ({}.scope_tenant_ids())"
            ).format(
                schema, sql.Identifier(table),
                sql.SQL(", ").join(sql.Identifier(c) for c in cols),
                sql.Identifier(table), schema,
            ))

        for table, cols in globals_.items():
            cur.execute(sql.SQL("DROP VIEW IF EXISTS {}.{}").format(schema, sql.Identifier(table)))
            cur.execute(sql.SQL(
                "CREATE VIEW {}.{} WITH (security_barrier) AS SELECT {} FROM public.{}"
            ).format(schema, sql.Identifier(table),
                     sql.SQL(", ").join(sql.Identifier(c) for c in cols),
                     sql.Identifier(table)))

        cur.execute(sql.SQL("DROP VIEW IF EXISTS {}.tenants").format(schema))
        cur.execute(sql.SQL(
            "CREATE VIEW {}.tenants WITH (security_barrier) AS "
            "SELECT id, slug, name FROM public.adm_tenants WHERE id = ANY ({}.scope_tenant_ids())"
        ).format(schema, schema))

        # Tenant directory for the bot process to map a slug to its id before scoping the
        # session. SECURITY DEFINER exposes only (id, slug, name); generated SQL can never call
        # it because the SQL guard allow-lists functions.
        cur.execute(sql.SQL(
            """
            CREATE OR REPLACE FUNCTION {}.tenant_directory()
            RETURNS TABLE (id uuid, slug text, name text)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
              SELECT id, slug::text, name::text FROM public.adm_tenants WHERE is_active
            $$
            """
        ).format(schema))

        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (REPORTING_ROLE,))
        if cur.fetchone() is None:
            cur.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                role, sql.Literal(reporting_password)))
        else:
            cur.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                role, sql.Literal(reporting_password)))
        cur.execute(sql.SQL("ALTER ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(role))
        cur.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role))
        cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(
            role, sql.Literal(statement_timeout)))
        cur.execute(sql.SQL("ALTER ROLE {} SET search_path = {}").format(role, schema))
        cur.execute(sql.SQL("ALTER ROLE {} SET idle_in_transaction_session_timeout = '60s'").format(role))

        # Least privilege: nothing on public, SELECT on rpt only.
        cur.execute("SELECT current_database()")
        (dbname,) = cur.fetchone()
        cur.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(sql.Identifier(dbname), role))
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(dbname), role))
        cur.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(role))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, role))
        cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(schema, role))
        cur.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {}.scope_tenant_ids() TO {}").format(schema, role))
        cur.execute(sql.SQL("REVOKE ALL ON FUNCTION {}.tenant_directory() FROM PUBLIC").format(schema))
        cur.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {}.tenant_directory() TO {}").format(schema, role))
        conn.commit()
    return sorted(set(tables) | set(globals_)) + ["tenants"]
