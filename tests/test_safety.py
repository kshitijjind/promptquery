import pytest

from promptquery.safety import UnsafeQuery, validate_select_only


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "SELECT * FROM users",
    "SELECT u.id, COUNT(*) FROM users u JOIN orders o ON o.user_id = u.id GROUP BY u.id",
    "WITH recent AS (SELECT id FROM orders WHERE created_at > NOW() - INTERVAL '7 days') SELECT * FROM recent",
    "SELECT 1 UNION ALL SELECT 2",
    "SELECT 1 INTERSECT SELECT 2",
    "SELECT 1 EXCEPT SELECT 2",
    "SELECT EXISTS (SELECT 1 FROM users WHERE id = 1) AS has_user",
])
def test_accepts_read_only_queries(sql):
    validate_select_only(sql)


@pytest.mark.parametrize("sql", [
    "INSERT INTO users (id) VALUES (1)",
    "UPDATE users SET name = 'x' WHERE id = 1",
    "DELETE FROM users WHERE id = 1",
    "DROP TABLE users",
    "CREATE TABLE x (id int)",
    "ALTER TABLE users ADD COLUMN name text",
    "TRUNCATE TABLE users",
    "GRANT SELECT ON users TO public",
])
def test_rejects_writes_and_ddl(sql):
    with pytest.raises(UnsafeQuery):
        validate_select_only(sql)


def test_rejects_multiple_statements():
    with pytest.raises(UnsafeQuery):
        validate_select_only("SELECT 1; DELETE FROM users")


def test_rejects_empty():
    with pytest.raises(UnsafeQuery):
        validate_select_only("")
    with pytest.raises(UnsafeQuery):
        validate_select_only("   ")


def test_rejects_dangerous_functions():
    with pytest.raises(UnsafeQuery):
        validate_select_only("SELECT pg_terminate_backend(123)")
    with pytest.raises(UnsafeQuery):
        validate_select_only("SELECT set_config('search_path', 'x', false)")


def test_rejects_cte_with_dml():
    with pytest.raises(UnsafeQuery):
        validate_select_only(
            "WITH deleted AS (DELETE FROM users RETURNING id) SELECT * FROM deleted"
        )
