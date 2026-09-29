"""Tests for enum-aware schema serialization (schema.Column + prompts.format_schema)."""
from __future__ import annotations

from promptquery.prompts import SYSTEM_PROMPT, format_schema
from promptquery.schema import Column, Table


def _orders_table() -> Table:
    return Table(
        schema="sales",
        name="orders",
        comment="customer orders",
        columns=[
            Column("id", "bigint", False, True),
            Column(
                "status", "order_status", False, False,
                comment="current order state",
                enum_values=("pending", "paid", "shipped", "refunded"),
            ),
            Column("placed_at", "timestamptz", True, False),
        ],
    )


def test_format_schema_lists_enum_values():
    out = format_schema([_orders_table()])
    assert "one of: 'pending', 'paid', 'shipped', 'refunded'" in out


def test_format_schema_renders_column_comment():
    out = format_schema([_orders_table()])
    assert "current order state" in out


def test_format_schema_plain_column_unchanged():
    out = format_schema([_orders_table()])
    assert "placed_at timestamptz" in out
    # No note suffix on a column without comment or enum:
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("placed_at"))
    assert "--" not in line


def test_column_roundtrip_preserves_enum_values_and_comment():
    c = Column(
        "status", "order_status", False, False,
        comment="state", enum_values=("a", "b"),
    )
    restored = Column.from_dict(c.to_dict())
    assert restored == c


def test_column_from_dict_tolerates_old_payloads():
    # Payloads serialized before 0.3.0 carry neither field.
    c = Column.from_dict(
        {"name": "x", "data_type": "int", "nullable": True, "is_primary_key": False}
    )
    assert c.comment is None
    assert c.enum_values is None


def test_system_prompt_carries_answer_shape_rules():
    assert "exactly the columns the question asks for" in SYSTEM_PROMPT
    assert "Never invent enum values" in SYSTEM_PROMPT
    assert "INNER JOIN" in SYSTEM_PROMPT
    assert "speculative conditions" in SYSTEM_PROMPT
