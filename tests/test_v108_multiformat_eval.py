"""Runs the multi-format evaluation (evaluation/multiformat) when its source files are present.

Files are looked up in $QUERYSURE_EVAL_DATA or tests/fixtures/user/ (they are not generated, so CI without them skips).
Every metric is asserted exactly as measured: a failing case fails this test, it is never filtered out.
"""
import pytest

from evaluation.multiformat.runner import run, run_negatives
from evaluation.multiformat.sources import DATASETS, Sources, resolve_data_dir

_SRC = Sources(resolve_data_dir(None))
_MISSING = [k for k in DATASETS if not _SRC.available(k)]
pytestmark = pytest.mark.skipif(len(_MISSING) == len(DATASETS), reason="multi-format source files not supplied")


def _failures(report):
    return [(r["id"], r["lang"], r.get("compare_reason"), r.get("semantic_issues"), r.get("verification"), r.get("error"))
            for r in report["results"] if not r.get("correct") or not r.get("clean")]


def test_tuned_cases_are_exactly_correct_and_clean():
    report = run()
    assert report["metrics"]["overall"]["cases"] > 0
    assert not _failures(report), _failures(report)
    for key, f in report["ingestion_fidelity"].items():
        assert f["row_counts_match"] and not f["mismatches"], (key, f)


def test_held_out_cases_are_exactly_correct_and_clean():
    report = run(holdout=True)
    assert not _failures(report), _failures(report)


def test_every_detectable_wrong_query_is_flagged():
    neg = run_negatives()
    missed = [r for r in neg["results"] if r["detectable"] and not r["detected"]]
    assert not missed, missed
