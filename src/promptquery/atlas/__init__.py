"""Atlas ERP profile: DB guard-rail provisioning plus the semantic layer and example bank."""
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

# Categorical-looking columns whose values are genuinely free text in Atlas, so no legal-value
# list exists or should exist. Shared by tests/test_schema_drift.py and
# infra/extract_enum_values.py so the two never disagree.
FREE_TEXT_CATEGORICALS = {
    "crm_leads.source",                  # String(60), no enum (models.py)
    "mfg_mrp_runs.demand_source",        # String(1000) description
    "mfg_planned_orders.source_notes",   # free-text notes
    "fin_bank_statements.source_filename",  # uploaded file name
    "hsp_menu_availability.source",
    "qm_inspection_lots.source",
}
