from promptquery.schema import Column, ForeignKey, Schema, Table


def _example_schema() -> Schema:
    users = Table(
        schema="public",
        name="users",
        comment="end-user accounts",
        columns=[
            Column("id", "bigint", False, True),
            Column("email", "text", True, False),
        ],
    )
    orders = Table(
        schema="public",
        name="orders",
        columns=[
            Column("id", "bigint", False, True),
            Column("user_id", "bigint", False, False),
        ],
        foreign_keys=[ForeignKey("user_id", "public", "users", "id")],
    )
    return Schema(tables=[users, orders])


def test_schema_roundtrip_preserves_structure():
    original = _example_schema()
    restored = Schema.from_dict(original.to_dict())

    assert len(restored.tables) == len(original.tables)
    assert [t.name for t in restored.tables] == [t.name for t in original.tables]

    orders = next(t for t in restored.tables if t.name == "orders")
    assert orders.foreign_keys[0].referenced_table == "users"
    assert orders.foreign_keys[0].referenced_column == "id"


def test_column_roundtrip_preserves_flags():
    c = Column("amount", "numeric(10,2)", nullable=False, is_primary_key=False)
    restored = Column.from_dict(c.to_dict())
    assert restored == c


def test_table_with_no_comment_serializes():
    t = Table(schema="public", name="t", columns=[Column("id", "int", False, True)])
    restored = Table.from_dict(t.to_dict())
    assert restored.comment is None
    assert restored.columns[0].is_primary_key is True
