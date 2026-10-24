"""
Result-set equivalence ("execution accuracy").

Two queries are considered equivalent for a benchmark case when they return
the same data -- NOT when their SQL text is the same. Concretely:

  * Row order is ignored unless the case is `ordered` (a top-N/ranking
    question), in which case the row *sequence* must match.
  * Column order and column names are ignored: columns are matched by their
    contents. (`SELECT a, b` and `SELECT b AS x, a AS y` are equivalent.)
  * Duplicate rows matter (multiset comparison), so a missing DISTINCT/GROUP
    BY that changes the row count is a mismatch.
  * Numbers: ints and floats compare equal (1 == 1.0); floats are compared
    at 4 decimal places, with a small absolute tolerance as a fallback.
  * NULLs match NULLs.
  * Types are otherwise strict: the text '2023' does NOT equal the integer
    2023. This is a deliberate, documented choice -- loosening it would only
    ever raise the score.
  * Extra columns in the candidate are a mismatch in the strict metric. A
    second, clearly separate "superset" verdict tolerates them.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

_FLOAT_DECIMALS = 4
_ABS_TOL = 1e-4
_MAX_MAPPINGS = 5000

Row = Sequence[Any]


@dataclass
class Comparison:
    match: bool
    kind: Optional[str] = None      # "exact" | "superset_columns" | None
    reason: str = ""

    def to_dict(self):
        return {"match": self.match, "kind": self.kind, "reason": self.reason}


def _canon(v: Any) -> Tuple[int, Any]:
    if v is None:
        return (0, 0)
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, (int, float)):
        return (1, round(float(v), _FLOAT_DECIMALS) + 0.0)
    if isinstance(v, bytes):
        return (3, v)
    return (2, str(v))


def _column_signature(rows: Sequence[Row], idx: int) -> Tuple:
    return tuple(sorted(_canon(r[idx]) for r in rows))


def _rows_equal(gold: List[Tuple], cand: List[Tuple], ordered: bool) -> bool:
    if ordered:
        return gold == cand
    return Counter(gold) == Counter(cand)


def _tolerant_equal(gold: Sequence[Row], cand: Sequence[Row], ordered: bool) -> bool:
    """Fallback for floats that straddle a rounding boundary. Identity column
    mapping only; compares row-by-row after a stable canonical sort."""
    def key(r):
        return tuple(_canon(x) for x in r)
    g = list(gold) if ordered else sorted(gold, key=key)
    c = list(cand) if ordered else sorted(cand, key=key)
    for gr, cr in zip(g, c):
        for a, b in zip(gr, cr):
            ca, cb = _canon(a), _canon(b)
            if ca[0] != cb[0]:
                return False
            if ca[0] == 1:
                if not math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=_ABS_TOL):
                    return False
            elif ca != cb:
                return False
    return True


def _candidate_mappings(gold_sigs: List[Tuple], cand_sigs: List[Tuple]):
    """Yield injective gold_col -> candidate_col maps where the column
    contents agree. Bounded so pathological (many identical columns) inputs
    cannot blow up."""
    n = len(gold_sigs)
    used: set = set()
    current: List[int] = []
    count = 0

    def rec(i: int):
        nonlocal count
        if i == n:
            count += 1
            yield list(current)
            return
        for j, sig in enumerate(cand_sigs):
            if j in used or sig != gold_sigs[i]:
                continue
            used.add(j)
            current.append(j)
            yield from rec(i + 1)
            current.pop()
            used.discard(j)
            if count >= _MAX_MAPPINGS:
                return

    yield from rec(0)


def compare_results(
    gold_rows: Sequence[Row],
    cand_rows: Sequence[Row],
    ordered: bool = False,
    allow_extra_columns: bool = False,
) -> Comparison:
    """Compare two result sets given as sequences of tuples/lists."""
    gold_rows = [tuple(r) for r in gold_rows]
    cand_rows = [tuple(r) for r in cand_rows]

    if len(gold_rows) != len(cand_rows):
        return Comparison(False, None, f"row count differs: expected {len(gold_rows)}, got {len(cand_rows)}")

    gold_width = len(gold_rows[0]) if gold_rows else None
    cand_width = len(cand_rows[0]) if cand_rows else None
    if not gold_rows:
        # Both empty: nothing to compare column-wise beyond "returns no rows".
        return Comparison(True, "exact", "both results are empty")

    if cand_width < gold_width:
        return Comparison(False, None, f"column count differs: expected {gold_width}, got {cand_width}")
    if cand_width > gold_width and not allow_extra_columns:
        return Comparison(False, None, f"column count differs: expected {gold_width}, got {cand_width} (extra columns)")

    gold_sigs = [_column_signature(gold_rows, i) for i in range(gold_width)]
    cand_sigs = [_column_signature(cand_rows, j) for j in range(cand_width)]
    gold_canon = [tuple(_canon(v) for v in r) for r in gold_rows]

    mappings = list(_candidate_mappings(gold_sigs, cand_sigs))
    for mapping in mappings:
        projected = [tuple(_canon(r[j]) for j in mapping) for r in cand_rows]
        if _rows_equal(gold_canon, projected, ordered):
            kind = "exact" if cand_width == gold_width else "superset_columns"
            note = "identical rows" if kind == "exact" else "expected columns present; candidate has extra columns"
            return Comparison(True, kind, note)

    if mappings:
        # Every expected column has a same-content counterpart, yet no mapping
        # produced matching rows.
        if ordered:
            for mapping in mappings:
                projected = [tuple(_canon(r[j]) for j in mapping) for r in cand_rows]
                if _rows_equal(gold_canon, projected, False):
                    return Comparison(False, None, "same rows but in a different order (this case requires the ranking order)")
        return Comparison(False, None, "columns hold the same values but rows do not line up (wrong grouping/join)")

    # Float-boundary fallback (identity mapping, equal widths only).
    if cand_width == gold_width and _tolerant_equal(gold_rows, cand_rows, ordered):
        return Comparison(True, "exact", "equal within floating-point tolerance")

    # Explain the mismatch a bit: which expected column has no counterpart?
    missing = [i for i, s in enumerate(gold_sigs) if s not in cand_sigs]
    if missing:
        return Comparison(False, None, f"values differ: expected column(s) {missing} have no matching column in the result")
    return Comparison(False, None, "values differ")
