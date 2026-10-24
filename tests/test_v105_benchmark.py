"""v1.0.5 held-out benchmark wrappers. Deterministic layers only (no LLM): grounding, planner, validators, verifier.
Separate numbers for CSV and SQLite; >=99% asserted only on what is measured here (see docs/03_EVALUATION.md, deterministic benchmarks)."""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "fixtures"))
import build_hard_csv as hc  # noqa: E402
import build_northwind_small as bnw  # noqa: E402
import format_benchmark_v105 as b  # noqa: E402
from modules.ingestion import ingest_to_sqlite  # noqa: E402


@pytest.fixture(scope="module")
def csv_env(tmp_path_factory):
    d = tmp_path_factory.mktemp("bench105")
    p = d / "hard.csv"
    hc.write_csv(str(p), hc.make_rows())
    db = d / "hard.db"
    ingest_to_sqlite(str(p), ".csv", str(db), "subs")
    return str(p), str(db)


@pytest.mark.parametrize("heldout", [False, True])
def test_csv_benchmark_at_least_99(csv_env, heldout):
    rep = b.run_csv(csv_env[0], csv_env[1], heldout)
    assert rep["accuracy"] >= 99.0, [c for c in rep["cases"] if not c["correct"]]
    # the planner's own reference answers must equal pandas on every phrasing it claims to cover
    assert rep["planner_exact"] == rep["planner_covered"] and rep["planner_covered"] > 0


def test_sqlite_cte_subquery_benchmark_at_least_99(tmp_path):
    db = tmp_path / "nw.sqlite"
    bnw.build(str(db))
    rep = b.run_sqlite(str(db))
    assert rep["accuracy"] >= 99.0, [c for c in rep["cases"] if not c["correct"]]
