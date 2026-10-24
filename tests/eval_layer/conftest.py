"""Fixtures for the evaluation-layer tests. The parent tests/conftest.py has
already set the development-mode env and put the repo root on sys.path."""
from __future__ import annotations

import pytest

from evaluation.benchmark import load_benchmark
from evaluation.db import build_sample_db
from evaluation.runner import build_environment


@pytest.fixture(scope="session")
def eval_db(tmp_path_factory):
    """The sample retail DB, built once per session by the repo's create_retail.py."""
    return build_sample_db(str(tmp_path_factory.mktemp("eval_retail")))


@pytest.fixture(scope="session")
def env(eval_db):
    return build_environment(eval_db)


@pytest.fixture(scope="session")
def benchmark():
    return load_benchmark()


@pytest.fixture(scope="session")
def oracle_report(eval_db):
    from evaluation.harness import run_evaluation
    return run_evaluation(mode="oracle", db_path=eval_db)


@pytest.fixture(scope="session")
def synthetic_report(eval_db):
    from evaluation.harness import run_evaluation
    return run_evaluation(mode="synthetic", db_path=eval_db)


def by_id(report):
    return {c["id"]: c for c in report["cases"]}
