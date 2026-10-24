"""
Benchmark case model + validation.

A benchmark file is JSON:

    {"version": "...", "description": "...", "reference_db_fingerprint": "...",
     "cases": [ {...}, ... ]}

Validation is strict on purpose: a typo in a hand-edited case (a misspelled
key, an answerable case with no gold SQL, a rejection case that carries one)
should fail loudly at load time rather than silently skew a metric.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


class DatasetError(ValueError):
    """The benchmark file or one of its cases is malformed."""


CATEGORIES = (
    "simple_filtering",
    "aggregation",
    "group_by",
    "joins",
    "date_time",
    "multi_step",
    "ambiguous",
    "impossible",
    "adversarial",
)
DIFFICULTIES = ("easy", "medium", "hard")
BEHAVIORS = ("answer", "reject", "clarify")
DECLINE_REASONS = ("ambiguous", "nonexistent_data", "adversarial", "invalid_input")

# category -> behavior the pipeline is expected to show
_CATEGORY_BEHAVIOR = {
    "ambiguous": "clarify",
    "impossible": "reject",
    "adversarial": "reject",
}
_CATEGORY_REASONS = {
    "ambiguous": ("ambiguous",),
    "impossible": ("nonexistent_data",),
    "adversarial": ("adversarial", "invalid_input"),
}

# Structural SQL features a case may declare as *required* of any correct
# answer. Only declare ones that are inherent to the question (a "per region"
# question needs a GROUP BY); leave out approach-dependent ones (a join vs a
# subquery can answer the same question).
CHARACTERISTIC_KEYS = (
    "aggregation", "group_by", "having", "order_by", "limit",
    "date_function", "join", "subquery_or_cte", "distinct",
)

_ID_RE = re.compile(r"^[a-z]+-\d{3}$")
_ALLOWED_KEYS = {
    "id", "category", "difficulty", "question", "expected_behavior", "gold_sql",
    "expected_tables", "expected_columns", "expected_characteristics", "ordered",
    "order_by_column", "allow_empty_result", "decline_reason", "notes",
}


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    category: str
    difficulty: str
    question: str
    expected_behavior: str  # answer | reject | clarify
    gold_sql: Optional[str] = None
    expected_tables: Tuple[str, ...] = ()
    expected_columns: Tuple[str, ...] = ()
    expected_characteristics: Dict[str, bool] = field(default_factory=dict)
    ordered: bool = False
    order_by_column: Optional[str] = None
    allow_empty_result: bool = False
    decline_reason: Optional[str] = None
    notes: str = ""

    @property
    def should_decline(self) -> bool:
        return self.expected_behavior != "answer"

    @classmethod
    def from_dict(cls, d: Dict[str, Any], position: int = 0) -> "BenchmarkCase":
        label = d.get("id", f"<case #{position}>") if isinstance(d, dict) else f"<case #{position}>"

        def fail(msg: str):
            raise DatasetError(f"{label}: {msg}")

        if not isinstance(d, dict):
            fail("case must be a JSON object")
        unknown = set(d) - _ALLOWED_KEYS
        if unknown:
            fail(f"unknown field(s): {sorted(unknown)}")
        for key in ("id", "category", "difficulty", "question", "expected_behavior"):
            if key not in d:
                fail(f"missing required field '{key}'")

        if not isinstance(d["id"], str) or not _ID_RE.match(d["id"]):
            fail("id must look like 'group-001'")
        if d["category"] not in CATEGORIES:
            fail(f"category must be one of {CATEGORIES}")
        if d["difficulty"] not in DIFFICULTIES:
            fail(f"difficulty must be one of {DIFFICULTIES}")
        if d["expected_behavior"] not in BEHAVIORS:
            fail(f"expected_behavior must be one of {BEHAVIORS}")
        if not isinstance(d["question"], str) or not d["question"].strip():
            fail("question must be a non-empty string")

        cat_behavior = _CATEGORY_BEHAVIOR.get(d["category"], "answer")
        if d["expected_behavior"] != cat_behavior:
            fail(f"category '{d['category']}' requires expected_behavior '{cat_behavior}'")

        tables = tuple(d.get("expected_tables", ()) or ())
        columns = tuple(d.get("expected_columns", ()) or ())
        chars = dict(d.get("expected_characteristics", {}) or {})
        bad_chars = [k for k, v in chars.items() if k not in CHARACTERISTIC_KEYS or not isinstance(v, bool)]
        if bad_chars:
            fail(f"expected_characteristics must map {CHARACTERISTIC_KEYS} to booleans; bad: {bad_chars}")

        gold = d.get("gold_sql")
        reason = d.get("decline_reason")
        ordered = bool(d.get("ordered", False))
        order_col = d.get("order_by_column")

        if d["expected_behavior"] == "answer":
            if not isinstance(gold, str) or not gold.strip():
                fail("answerable case needs gold_sql")
            if not tables:
                fail("answerable case needs expected_tables")
            if reason is not None:
                fail("answerable case must not set decline_reason")
            if ordered and not order_col:
                fail("ordered case needs order_by_column (used by the tie-safety check)")
        else:
            if gold:
                fail("reject/clarify case must not have gold_sql")
            if tables or columns or chars:
                fail("reject/clarify case must not declare expected tables/columns/characteristics")
            if reason not in _CATEGORY_REASONS[d["category"]]:
                fail(f"decline_reason for '{d['category']}' must be one of {_CATEGORY_REASONS[d['category']]}")
            if ordered or order_col:
                fail("reject/clarify case cannot be ordered")

        return cls(
            id=d["id"], category=d["category"], difficulty=d["difficulty"],
            question=d["question"].strip(), expected_behavior=d["expected_behavior"],
            gold_sql=gold.strip() if isinstance(gold, str) else None,
            expected_tables=tuple(t.lower() for t in tables),
            expected_columns=tuple(c.lower() for c in columns),
            expected_characteristics=chars, ordered=ordered, order_by_column=order_col,
            allow_empty_result=bool(d.get("allow_empty_result", False)),
            decline_reason=reason, notes=str(d.get("notes", "")),
        )


@dataclass(frozen=True)
class Benchmark:
    version: str
    description: str
    cases: Tuple[BenchmarkCase, ...]
    sha256: str
    reference_db_fingerprint: Optional[str] = None
    path: Optional[str] = None


def parse_benchmark(raw_text: str, path: Optional[str] = None) -> Benchmark:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise DatasetError(f"benchmark is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise DatasetError("benchmark must be an object with a 'cases' list")
    if not data["cases"]:
        raise DatasetError("benchmark has no cases")
    cases = tuple(BenchmarkCase.from_dict(c, i) for i, c in enumerate(data["cases"]))
    ids = [c.id for c in cases]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise DatasetError(f"duplicate case ids: {dupes}")
    questions = [c.question.lower() for c in cases]
    dupq = sorted({q for q in questions if questions.count(q) > 1})
    if dupq:
        raise DatasetError(f"duplicate questions: {dupq}")
    return Benchmark(
        version=str(data.get("version", "unversioned")),
        description=str(data.get("description", "")),
        cases=cases,
        sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        reference_db_fingerprint=data.get("reference_db_fingerprint"),
        path=path,
    )


def select_cases(
    cases: Sequence[BenchmarkCase],
    categories: Optional[Sequence[str]] = None,
    difficulties: Optional[Sequence[str]] = None,
    ids: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
) -> List[BenchmarkCase]:
    """Filter cases. Unknown filter values raise instead of silently matching nothing."""
    if categories:
        bad = set(categories) - set(CATEGORIES)
        if bad:
            raise DatasetError(f"unknown categories: {sorted(bad)}")
    if difficulties:
        bad = set(difficulties) - set(DIFFICULTIES)
        if bad:
            raise DatasetError(f"unknown difficulties: {sorted(bad)}")
    if ids:
        missing = set(ids) - {c.id for c in cases}
        if missing:
            raise DatasetError(f"unknown case ids: {sorted(missing)}")
    out = [
        c for c in cases
        if (not categories or c.category in categories)
        and (not difficulties or c.difficulty in difficulties)
        and (not ids or c.id in ids)
    ]
    if limit is not None:
        if limit < 1:
            raise DatasetError("limit must be >= 1")
        out = out[:limit]
    return out
