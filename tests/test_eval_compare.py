"""Execution-accuracy comparator used by eval/atlas_eval.py."""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from atlas_eval import results_match  # noqa: E402


def test_extra_columns_and_row_order_ignored():
    gold = [("A", Decimal("1.00")), ("B", Decimal("2"))]
    assert results_match(gold, ["n", "code", "v"], [["B", "x", 2.0], ["A", "y", 1.0]])[0]


def test_order_enforced_when_requested():
    gold = [("A", 1), ("B", 2)]
    assert not results_match(gold, ["n", "v"], [["B", 2], ["A", 1]], ordered=True)[0]
    assert results_match(gold, ["n", "v"], [["A", 1], ["B", 2]], ordered=True)[0]


def test_numeric_tolerance_and_types():
    assert results_match([(Decimal("5569200.00"),)], ["r"], [[5569200.004]])[0]
    assert results_match([(Decimal("2.5"),)], ["r"], [["2.50"]])[0]
    assert not results_match([(Decimal("2.5"),)], ["r"], [[2.6]])[0]


def test_row_mismatch_detected():
    assert not results_match([("A", 1)], ["n", "v"], [["A", 2]])[0]
    assert not results_match([("A", 1)], ["n"], [["A"], ["B"]])[0]


def test_columns_must_align_row_wise():
    # both columns exist but the pairing is wrong
    gold = [("A", 1), ("B", 2)]
    assert not results_match(gold, ["n", "v"], [["A", 2], ["B", 1]])[0]


def test_dates_compared_as_iso():
    import datetime as dt
    assert results_match([(dt.date(2025, 3, 1),)], ["m"], [["2025-03-01"]])[0]
