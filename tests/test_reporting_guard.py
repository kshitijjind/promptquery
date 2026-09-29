"""SQL guard: safety, table allow-list, function allow-list, row cap. No database needed."""
import pytest

from promptquery.reporting.catalog import Catalog, SemanticLayer
from promptquery.reporting.guard import GuardError, check_sql
from promptquery.schema import Column, Schema, Table


def _catalog() -> Catalog:
    def t(name, *cols):
        return Table("public", name, columns=[Column(c, "text", True, c == "id") for c in cols])
    schema = Schema([t("sales_orders", "id", "customer_id", "total_amount", "order_date", "status"),
                     t("sales_customers", "id", "name")])
    return Catalog(schema, SemanticLayer())


@pytest.mark.parametrize("sql", [
    "DELETE FROM sales_orders",
    "UPDATE sales_orders SET status = 'X'",
    "DROP TABLE sales_orders",
    "SELECT 1; DELETE FROM sales_orders",
    "WITH x AS (DELETE FROM sales_orders RETURNING *) SELECT * FROM x",
    "SELECT * INTO copy FROM sales_orders",
    "SELECT * FROM sales_orders FOR UPDATE",
    "SELECT pg_sleep(5)",
    "SELECT * FROM dblink('host=x', 'DELETE FROM t') AS t(a int)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT set_config('atlas.tenant_ids', 'x', false)",
    "SELECT current_setting('atlas.tenant_ids')",
    "SELECT * FROM public.sales_orders",
    "SELECT * FROM pg_catalog.pg_authid",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM core_users",
    "SELECT lo_import('/etc/passwd')",
    "SELECT query_to_xml('select 1', true, true, '')",
    "SELECT made_up_function(total_amount) FROM sales_orders",
])
def test_rejects_unsafe_or_unknown(sql):
    with pytest.raises(GuardError):
        check_sql(sql, _catalog())


def test_unknown_table_suggests_close_match():
    with pytest.raises(GuardError, match="did you mean sales_orders"):
        check_sql("SELECT * FROM sales_order", _catalog())


@pytest.mark.parametrize("sql", [
    "SELECT c.name, SUM(so.total_amount) FROM sales_orders so JOIN sales_customers c ON c.id = so.customer_id GROUP BY c.name",
    "WITH t AS (SELECT customer_id, SUM(total_amount) v FROM sales_orders GROUP BY 1) SELECT * FROM t ORDER BY v DESC LIMIT 5",
    "SELECT date_trunc('month', order_date)::date, to_char(order_date, 'YYYY') FROM sales_orders",
    "SELECT SUM(total_amount) FILTER (WHERE status = 'CONFIRMED') FROM rpt.sales_orders",
    "SELECT status FROM sales_orders UNION SELECT name FROM sales_customers",
])
def test_accepts_reporting_queries(sql):
    assert check_sql(sql, _catalog()).sql


def test_row_cap_added_only_when_missing():
    assert check_sql("SELECT id FROM sales_orders", _catalog(), max_rows=100).sql.endswith("LIMIT 101")
    assert check_sql("SELECT id FROM sales_orders LIMIT 5", _catalog(), max_rows=100).sql.endswith("LIMIT 5")


def test_reports_referenced_tables():
    g = check_sql("SELECT 1 FROM sales_orders so JOIN sales_customers c ON c.id = so.customer_id", _catalog())
    assert g.tables == ["sales_orders", "sales_customers"]
