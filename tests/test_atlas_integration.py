"""Integration tests against a live Atlas database (skipped when it is unreachable).

Set ATLAS_REPORTING_DSN / ATLAS_ADMIN_DSN (or a .env file) and run `atlas-bot setup-db` first.
"""
from __future__ import annotations

import psycopg
import pytest
import yaml

from promptquery.atlas import DATA_DIR
from promptquery.reporting.catalog import load_catalog
from promptquery.reporting.config import Settings
from promptquery.reporting.executor import ReportingExecutor, TenantError
from promptquery.reporting.guard import check_sql

SETTINGS = Settings.from_env()


def _reachable() -> bool:
    try:
        with psycopg.connect(SETTINGS.reporting_dsn, connect_timeout=2) as c:
            c.execute("SELECT 1 FROM rpt.tenant_directory() LIMIT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="Atlas reporting DB not reachable")


@pytest.fixture(scope="module")
def acme():
    return ReportingExecutor(SETTINGS.reporting_dsn, "acme")


@pytest.fixture(scope="module")
def catalog(acme):
    return load_catalog(acme, None)


# ---------------------------------------------------------------- DB guard rails
def test_writes_are_rejected_by_the_database(acme):
    for sql in ["DELETE FROM sales_customers", "UPDATE sales_orders SET status='X'",
                "INSERT INTO sales_customer_groups(id) VALUES (gen_random_uuid())",
                "CREATE TABLE x(a int)"]:
        with pytest.raises(psycopg.Error):
            acme.run(sql)


def test_base_tables_and_secrets_are_invisible(acme):
    for sql in ["SELECT * FROM public.sales_customers", "SELECT * FROM core_users",
                "SELECT * FROM public.core_api_keys", "SELECT * FROM public.adm_tenants"]:
        with pytest.raises(psycopg.Error):
            acme.run(sql)


def test_masked_hr_columns_do_not_exist(acme):
    for col in ["base_salary", "national_id", "tax_id", "date_of_birth", "bank_account"]:
        with pytest.raises(psycopg.errors.UndefinedColumn):
            acme.run(f"SELECT {col} FROM hr_employees")


def test_tenant_isolation():
    a = ReportingExecutor(SETTINGS.reporting_dsn, "acme").run("SELECT id FROM sales_customers").rows
    r = ReportingExecutor(SETTINGS.reporting_dsn, "retail").run("SELECT id FROM sales_customers").rows
    both = ReportingExecutor(SETTINGS.reporting_dsn, "acme,retail").run("SELECT id FROM sales_customers").rows
    assert a and r and not (set(a) & set(r))
    assert set(both) == set(a) | set(r)


def test_unscoped_session_sees_nothing():
    with psycopg.connect(SETTINGS.reporting_dsn) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rpt.sales_orders").fetchone()[0] == 0


def test_unknown_tenant_rejected():
    with pytest.raises(TenantError):
        ReportingExecutor(SETTINGS.reporting_dsn, "no-such-company")


def test_statement_timeout_enforced():
    ex = ReportingExecutor(SETTINGS.reporting_dsn, "volume", statement_timeout_ms=200)
    with pytest.raises(psycopg.errors.QueryCanceled):
        ex.run("SELECT COUNT(*) FROM fin_journal_lines a CROSS JOIN fin_journal_lines b")


def test_row_cap_and_truncation_flag(acme):
    out = acme.run("SELECT generate_series(1, 50)", max_rows=10)
    assert len(out.rows) == 10 and out.truncated


# ---------------------------------------------------------------- catalog / semantic layer
def test_catalog_has_semantics_and_virtual_fks(catalog):
    inv = catalog.table("fin_customer_invoices")
    assert inv is not None and "credit notes" in inv.comment
    assert any(fk.column == "partner_id" and fk.referenced_table == "sales_customers" for fk in inv.foreign_keys)
    status = next(c for c in catalog.table("sales_orders").columns if c.name == "status")
    assert "CANCELLED" in status.enum_values
    assert all(c.name != "tenant_id" for t in catalog.schema.tables for c in t.columns)


def test_every_few_shot_example_passes_guard_and_runs(catalog):
    examples = yaml.safe_load((DATA_DIR / "examples.yaml").read_text())
    for tenant in ("acme", "volume"):
        ex = ReportingExecutor(SETTINGS.reporting_dsn, tenant)
        for e in examples:
            guarded = check_sql(e["sql"].replace("{today}", "2026-09-27"), catalog)
            ex.run(guarded.sql)
