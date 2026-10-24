"""Result-set equivalence: the heart of 'don't fake accuracy with string matching'."""
from __future__ import annotations

import pytest

from evaluation.compare import compare_results


def ok(gold, cand, **kw):
    return compare_results(gold, cand, **kw).match


def test_row_order_is_ignored_for_unordered_cases():
    assert ok([(1, "a"), (2, "b")], [(2, "b"), (1, "a")])


def test_row_order_matters_for_ordered_cases():
    assert ok([(1, "a"), (2, "b")], [(1, "a"), (2, "b")], ordered=True)
    r = compare_results([(1, "a"), (2, "b")], [(2, "b"), (1, "a")], ordered=True)
    assert not r.match and "order" in r.reason


def test_column_order_and_names_do_not_matter():
    # SELECT id, label   vs   SELECT label AS x, id AS y
    assert ok([(1, "a"), (2, "b")], [("a", 1), ("b", 2)])


def test_duplicates_are_counted():
    assert not ok([(1,), (1,)], [(1,)])
    assert not ok([(1,)], [(1,), (1,)])
    assert ok([(5,), (5,)], [(5,), (5,)])


def test_a_wrong_pairing_of_columns_is_a_mismatch():
    # same values in each column, but rows are paired differently (wrong join/grouping)
    r = compare_results([(1, "a"), (2, "b")], [(1, "b"), (2, "a")])
    assert not r.match and "line up" in r.reason


def test_different_row_counts_fail_fast():
    r = compare_results([(1,), (2,)], [(1,)])
    assert not r.match and "row count" in r.reason


def test_extra_columns_are_strict_mismatch_but_superset_match():
    strict = compare_results([(1, "a")], [(1, "a", 9)])
    assert not strict.match and "extra columns" in strict.reason
    loose = compare_results([(1, "a")], [(1, "a", 9)], allow_extra_columns=True)
    assert loose.match and loose.kind == "superset_columns"
    # ...but a missing column can never be tolerated
    assert not ok([(1, "a")], [(1,)], allow_extra_columns=True)


def test_missing_expected_column_values_are_a_mismatch():
    assert not compare_results([(1, "a")], [(1, "zzz", 9)], allow_extra_columns=True).match


@pytest.mark.parametrize("a, b, expected", [
    (1, 1.0, True),                 # int vs float
    (0.12345, 0.123449999, True),   # float noise
    (1.00005, 1.00004, True),       # straddles the 4dp rounding boundary; abs-tol fallback
    (1.0, 1.001, False),            # a genuinely different number
    ("2023", 2023, False),          # text is NOT a number: deliberately strict
    (None, None, True),
    (None, 0, False),               # NULL is not zero
    (None, "", False),
])
def test_scalar_comparison_rules(a, b, expected):
    assert ok([(a,)], [(b,)]) is expected


def test_bool_and_int_are_interchangeable():
    assert ok([(1,)], [(True,)])


def test_two_empty_results_are_equivalent_but_empty_vs_nonempty_is_not():
    assert ok([], [])
    assert not ok([], [(1,)])
    assert not ok([(1,)], [])


def test_duplicate_output_columns_can_still_be_mapped():
    # SELECT id, id  -- two identical gold columns must both find a home
    assert ok([(1, 1), (2, 2)], [(1, 1), (2, 2)])
    assert not ok([(1, 1), (2, 2)], [(1, 2), (2, 1)])


def test_many_identical_columns_do_not_blow_up():
    gold = [(1,) * 8 for _ in range(3)]
    cand = [(1,) * 12 for _ in range(3)]
    assert compare_results(gold, cand, allow_extra_columns=True).match


def test_sql_text_is_never_consulted():
    """Different SQL, same answer -> match. (Behavioural statement of the design goal.)"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE TABLE t(a INT, b TEXT); INSERT INTO t VALUES (1,'x'),(2,'y'),(3,'x');")
    left = conn.execute("SELECT b, COUNT(*) FROM t GROUP BY b").fetchall()
    right = conn.execute("SELECT n, k FROM (SELECT COUNT(a) AS n, b AS k FROM t GROUP BY 2) ORDER BY k DESC").fetchall()
    assert compare_results(left, right).match
