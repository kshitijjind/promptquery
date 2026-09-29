"""Schema-drift guards: fail when Atlas evolves in a way the reporting layer does not cover.

`atlas-bot setup-db` discovers reportable tables from the live schema, so a new Atlas table or
column is picked up automatically. That is convenient but it is a DENY-list model: anything new
is exposed unless it is excluded. These tests make every drift case loud instead of silent:

  * a new table/column with a sensitive-looking name gets exposed        -> test_no_sensitive...
  * a new table has no business description (weaker retrieval)          -> test_every_reportable...
  * a status column has no legal-value list (the model may invent one)  -> test_status_columns...
  * a table was renamed/dropped, so semantic layer or exclusions go stale
                                              -> test_semantic..., test_excluded..., test_examples...

When Atlas legitimately adds something, the fix is to update the data files (semantic.yaml,
enum_values.json) or the lists in atlas/setup_reporting.py, then extend the baseline below.
"""
from __future__ import annotations

import json

import psycopg
import pytest
import yaml

from promptquery.atlas import DATA_DIR, FREE_TEXT_CATEGORICALS
from promptquery.atlas.setup_reporting import EXCLUDED_TABLES, MASKED_COLUMNS
from promptquery.reporting.retrieval import load_examples

from .test_atlas_integration import SETTINGS, _reachable

pytestmark = pytest.mark.skipif(not _reachable(), reason="Atlas reporting DB not reachable")

# Column-name fragments that imply personal or secret data.
SENSITIVE_FRAGMENTS = (
    "salary", "wage", "iban", "ssn", "national_id", "tax_id", "bank_account", "date_of_birth",
    "birth_date", "password", "secret", "token", "api_key", "key_hash", "credit_card",
    "card_number", "passport", "compensation",
)

# Names that match a fragment above but are provably not sensitive: opaque UUID references to a
# bank-account *record*, not an account number. Anything else must be masked or excluded.
SENSITIVE_ALLOWED = {
    "fin_bank_statements.bank_account_id",
    "fin_customer_receipts.bank_account_id",
    "fin_vendor_payments.bank_account_id",
}

# Reportable tables that have no description in semantic.yaml yet. They still work (retrieval
# falls back to column names) but get no business guidance. This is a frozen BASELINE: a newly
# added Atlas table must either be described in semantic.yaml or added here deliberately.
UNDESCRIBED_BASELINE = {
    "fin_allocation_rule_targets", "fin_allocation_rules", "fin_allocation_runs",
    "fin_depreciation_runs", "fin_fx_revaluation_runs", "fin_posting_defaults",
    "hr_leave_accruals", "hsp_menu_availability", "hsp_menu_item_tags", "hsp_menu_placements",
    "hsp_menu_sections", "hsp_order_ticket_lines", "hsp_service_slots",
    "inv_layer_consumptions", "inv_uom_conversions", "mfg_capacity_loads", "mfg_mrp_runs",
    "mfg_planned_orders", "mfg_production_order_components", "mfg_production_order_operations",
    "mfg_routing_operations", "mfg_routings", "pm_maintenance_plans", "proc_approval_rules",
    "proc_invoice_match_lines", "proc_match_tolerances", "proc_requisition_lines",
    "proc_rfq_lines", "proc_vendor_approved_items",
}

STATUS_SUFFIXES = ("_status", "_type", "_mode", "_stage")
STATUS_NAMES = {"status", "stage", "move_type", "account_type", "normal_balance", "partner_type"}


@pytest.fixture(scope="module")
def live():
    with psycopg.connect(SETTINGS.reporting_dsn) as conn:
        views = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.views WHERE table_schema = 'rpt'")}
        columns = conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'rpt' ORDER BY 1, 2").fetchall()
    return {"views": views, "columns": columns}


@pytest.fixture(scope="module")
def semantic():
    return yaml.safe_load((DATA_DIR / "semantic.yaml").read_text())


def test_no_sensitive_columns_exposed(live):
    """A new Atlas table/column holding personal or secret data must be masked or excluded."""
    offenders = sorted(
        f"{t}.{c}" for t, c, _ in live["columns"]
        if any(frag in c.lower() for frag in SENSITIVE_FRAGMENTS)
        and f"{t}.{c}" not in SENSITIVE_ALLOWED
    )
    assert not offenders, (
        "sensitive-looking columns are reportable: " + ", ".join(offenders)
        + "\nAdd the table to EXCLUDED_TABLES or the columns to MASKED_COLUMNS in "
          "atlas/setup_reporting.py, re-run `atlas-bot setup-db`, or allow-list them in "
          "SENSITIVE_ALLOWED if they are provably harmless."
    )


def test_masked_columns_are_actually_absent(live):
    """Every column named in MASKED_COLUMNS must really be gone from its view."""
    present = {(t, c) for t, c, _ in live["columns"]}
    leaked = sorted(f"{table}.{col}" for table, cols in MASKED_COLUMNS.items()
                    for col in cols if (table, col) in present)
    assert not leaked, "masked columns are visible: " + ", ".join(leaked)


def test_tenant_id_never_exposed(live):
    """tenant_id must not appear in any view: scoping is the database's job, not the model's."""
    leaked = sorted(t for t, c, _ in live["columns"] if c == "tenant_id")
    assert not leaked, "tenant_id exposed on: " + ", ".join(leaked)


def test_every_reportable_table_is_described_or_acknowledged(live, semantic):
    """A newly reportable Atlas table must be described in semantic.yaml (or acknowledged)."""
    undescribed = live["views"] - set(semantic["tables"]) - {"tenants"} - UNDESCRIBED_BASELINE
    assert not undescribed, (
        "new reportable tables without a business description: " + ", ".join(sorted(undescribed))
        + "\nDescribe them in atlas/data/semantic.yaml (and add a metric if they are reportable "
          "subjects), or add them to UNDESCRIBED_BASELINE deliberately."
    )


def test_status_columns_have_legal_values(live):
    """A status-like column with no value list lets the model invent values, and the SQL
    validator cannot check it."""
    enums = json.loads((DATA_DIR / "enum_values.json").read_text())
    missing = sorted(
        f"{t}.{c}" for t, c, dtype in live["columns"]
        if dtype.startswith("character")
        and (c in STATUS_NAMES or c.endswith(STATUS_SUFFIXES))
        and f"{t}.{c}" not in enums and f"{t}.{c}" not in FREE_TEXT_CATEGORICALS
    )
    assert not missing, (
        "status-like columns with no legal-value list: " + ", ".join(missing)
        + "\nExtract the values from the Atlas enum in backend/app/modules/*/constants.py into "
          "atlas/data/enum_values.json, or list the column in FREE_TEXT_CATEGORICALS."
    )


def test_semantic_layer_has_no_stale_tables(live, semantic):
    """A renamed or dropped Atlas table must not linger in the semantic layer."""
    stale = sorted(set(semantic["tables"]) - live["views"] - {"tenants"})
    assert not stale, "semantic.yaml describes tables that no longer exist: " + ", ".join(stale)


def test_metrics_and_virtual_fks_reference_real_tables(live, semantic):
    """Metric table lists and virtual FKs must point at tables that exist."""
    bad = sorted({t for m in semantic["metrics"].values() for t in m.get("tables", [])}
                 - live["views"])
    assert not bad, "metrics reference missing tables: " + ", ".join(bad)
    for fk in semantic["virtual_foreign_keys"]:
        assert fk["table"] in live["views"], f"virtual FK on missing table {fk['table']}"
        assert fk["ref_table"] in live["views"], f"virtual FK to missing table {fk['ref_table']}"


def test_examples_reference_real_tables(live):
    """The few-shot bank must not teach the model a table that no longer exists."""
    bad = sorted({t for ex in load_examples() for t in ex.tables} - live["views"])
    assert not bad, "examples.yaml references missing tables: " + ", ".join(bad)


def test_exclusions_are_not_stale():
    """If an excluded (secret) table is renamed, the exclusion silently stops protecting it, and
    the new name becomes reportable. Every exclusion must still match a real table."""
    with psycopg.connect(SETTINGS.admin_dsn or SETTINGS.reporting_dsn) as conn:
        public = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")}
    stale = sorted(EXCLUDED_TABLES - public)
    assert not stale, (
        "EXCLUDED_TABLES entries no longer exist (renamed?): " + ", ".join(stale)
        + "\nIf a secret table was renamed, its new name is now REPORTABLE. Update "
          "EXCLUDED_TABLES in atlas/setup_reporting.py and re-run `atlas-bot setup-db`."
    )


def test_default_deny_masks_new_sensitive_columns():
    """The name policy, not a hardcoded table list, is what protects a table Atlas adds later."""
    from promptquery.atlas.setup_reporting import is_sensitive

    for column in ("iban", "salary_grade", "employee_salary", "password_hash", "api_key",
                   "national_id", "date_of_birth", "card_number", "recovery_code"):
        assert is_sensitive("some_new_table", column), column
    for column in ("id", "name", "total_amount", "status", "order_date", "quantity"):
        assert not is_sensitive("sales_orders", column), column
    # reviewed exceptions stay visible
    assert not is_sensitive("fin_customer_receipts", "bank_account_id")
    assert is_sensitive("other_table", "bank_account_id")


def test_every_table_has_a_description_or_domain_fallback(live):
    """Even an undescribed table must tell the model which business area it belongs to."""
    from promptquery.reporting.catalog import fallback_description

    missing = sorted(t for t in live["views"]
                     if t != "tenants" and fallback_description(t) is None)
    assert not missing, ("tables whose prefix maps to no module domain: " + ", ".join(missing)
                         + "\nAdd the prefix to MODULE_DOMAINS in reporting/catalog.py")
