# Baseline (Sprint 0)

Measured on tag `v1-querysure` (commit `a47f924`, identical to `main` before Sprint 1), in a clean checkout of that tag,
on macOS 15 (Darwin 24.6) with Python 3.12.15 and Node 26.3. Nothing below calls an LLM: the evaluations score the
deterministic pipeline against candidate SQL supplied with each case.

## Static checks and tests

| Check | Result |
|---|---|
| `ruff check .` | All checks passed |
| `pytest -q` (SQLite) | **1232 passed, 8 skipped, 2 failed** |

Both failures are `tests/test_sprint5_e2e.py::test_critical_user_flows_in_real_browser[source-assets|minified-assets]`.
The cause is the environment, not the code: `@sparticuz/chromium` ships a Linux-only Chromium binary, and on macOS
`spawn` fails with `ENOEXEC` before any check runs. In GitHub Actions (ubuntu-latest) the same test launches Chromium.

The 8 skips are tests whose dependencies are absent locally: `playwright` (1), `sentry_sdk` (1), `pg_dump`/`pg_restore`
with `RESTORE_ADMIN_URL` (1), `TEST_POSTGRES_URL` (1), and files that are not in the repository (`northwind_small.sqlite`,
`sales.xlsx`, `sales.csv`, `sales.json` in `tests/fixtures/user/`) (4).

## Evaluation

Data: the five files expected by `evaluation/multiformat/sources.py`, supplied with `QUERYSURE_EVAL_DATA`.

| Run | Command | Result |
|---|---|---|
| Multi-format, tuned set | `python -m evaluation.multiformat` | 180 cases: exact 100.0%, semantic 100.0%, clean 100.0%, retrieval recall 100.0% ([output](multiformat.txt)) |
| Multi-format, held-out set | `python -m evaluation.multiformat --holdout` | 32 cases: exact 100.0%, semantic 100.0%, clean 100.0% ([output](multiformat_holdout.txt)) |
| Negative cases (known-wrong SQL) | `python -m evaluation.multiformat --negatives` | 24 cases: detectable 20/20 detected, hard 1/4 detected ([output](multiformat_negatives.txt)) |
| Retail benchmark, oracle mode | `python -m evaluation run --mode oracle` | 62 cases: behaviour accuracy 100.0%, execution accuracy 100.0% of 44 answerable, rejection accuracy 100.0% of 18 ([output](oracle.txt)) |

These figures show that the scoring harness and deterministic pipeline agree with the gold answers. They are **not**
end-to-end accuracy with a real model writing the SQL: that needs `--live` runs with a provider key, which were not
part of this baseline.
