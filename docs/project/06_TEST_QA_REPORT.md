# Test and QA report

| | |
|---|---|
| **Build under test** | `main` at merge `57467bb` (PR #2, AI Operations Platform) |
| **Report date** | 2026-11-25 |
| **Environment** | macOS, Python 3.12, SQLite and PostgreSQL 16, local Chrome for browser tests |
| **Verdict** | **Pass**, with the open quality risks in section 6 |

The detailed evaluation method and history are in [03_EVALUATION.md](../03_EVALUATION.md). This report is the current summary.

## 1. Test strategy

| Level | What it proves | How |
|---|---|---|
| Unit and integration | Each module does what its acceptance criteria say | pytest, 101 test files |
| Security | The model can't write, leak or be steered | SQL guardrail tests, prompt-injection tests, route authentication matrix, 36-case safety suite |
| Database | Works on the production database, not only SQLite | Full suite against PostgreSQL 16; migrations up, down and up |
| Browser end-to-end | The real UI works in a real browser | `e2e/e2e.mjs` with Chrome, on source and minified assets |
| AI evaluation | How good the AI parts are | Deterministic pipeline on 5 real files with answers computed independently in pandas; held-out set; negative cases; retrieval recall with 95% intervals; live model runs |
| Operations | It deploys, restarts and restores | Production-mode rehearsal: image build, migrations on boot, smoke test, backup, verified restore |
| Exploratory | Every page and workflow by hand | Browser walkthrough of all seven pages with a test account |

## 2. Results

| Check | Command | Result |
|---|---|---|
| Lint | `ruff check .` | No findings |
| Tests, SQLite | `pytest -q` (with `CHROME_PATH` for browser tests) | **1,504 passed, 0 failed, 47 skipped** |
| Tests, PostgreSQL 16 | `TEST_DATABASE_URL=… pytest -q` | **1,520 passed, 0 failed, 32 skipped (MySQL-source tests skipped: no MySQL server in this run)** |
| Browser end-to-end | `tests/test_sprint5_e2e.py` | 94/94 checks, source and minified assets, in 3 separate runs. One run of the browser and XSS tests together had 1 intermittent failure that did not reproduce in 3 reruns |
| Safety suite | `python -m evaluation.platform --suite safety` | **36/36 blocked** |
| Retrieval | `python -m evaluation.platform --suite retrieval` | Keyword recall@3 1.00, reworded 0.72, off-topic false positives 12.5%; vector index "not justified" |
| Migrations (25) | `alembic upgrade head`, `alembic check`, `downgrade -1`, `upgrade head` on PostgreSQL 16 | Clean (2026-11-25) |
| Dependency audit | `pip-audit -r requirements.txt` | No known vulnerabilities |
| Production-mode rehearsal | `scripts/launch_rehearsal.sh` | Passed 2026-11-07 |

After these runs, one user-facing message in `assistant.py` was reworded (it pointed to the old "Ask-data" panel); its test file was re-run: 7 passed.

## 3. AI quality

| Measure | Result |
|---|---|
| Deterministic pipeline, 180 tuned runs over 5 real files | 94.44% → **100%** exactly correct after fixes; false alarms 18.89% → **0%** |
| Held-out runs (32), written after the fixes | **100%** correct after 2 defects found and fixed |
| Deliberately wrong SQL (24) | **20/20** detectable cases caught; 1 of 4 "hard" cases caught |
| Retrieval, keyword questions (24) | recall@3 **1.00** (95% CI 0.86–1.00) |
| Retrieval, reworded questions (60) | recall@3 **0.72** (95% CI 0.59–0.82) |
| Off-topic questions that retrieve text (16) | 12.5% (cap 25%); answered "insufficient" or withheld |
| Live model (Groq `openai/gpt-oss-120b`), answered questions | **57.5%** (tuned, 80 answered) and **60.9%** (held-out, 23 answered) exact across 5 formats; about **63%** on the Stats NZ CSV after the hierarchy fix. Partial runs, small samples |

## 4. Defects found and fixed

| Defect | Found by | Fix | Regression test |
|---|---|---|---|
| Verifier flagged 3 of 44 correct answers as wrong | Oracle evaluation | Verification logic corrected | `test_v107_final.py::test_a_real_mismatch_is_still_caught` |
| "Low confidence" on 81.8% of correct answers | Oracle evaluation | Threshold 0.15 → 0.05 | Oracle run |
| 10 correct queries rejected; Northwind retrieval recall 8.8% | Multi-format evaluation | Validator and retrieval fixes | `test_v108_accuracy_regressions.py` |
| Default models retired by the provider | Live run | New defaults plus fallback chain | `test_default_models_are_not_the_retired_ones` |
| SQL posted directly to `/api/execute-sql` skipped validation | Security review | Re-validated on every call | `test_sprint3_prompt_injection.py` |
| 14 known dependency vulnerabilities | `pip-audit` | Upgrades | `pip-audit` in CI |
| Server deadlocked at 200 simultaneous users | Load test | Admission control | `loadtests/results/` |
| Agent rejected every plan (prompt didn't state argument ranges) | Browser walkthrough | Ranges added to the system prompt (`agent._param_type`) | None yet; re-checked in the browser |
| Analyses could be started with no dataset selected | Browser walkthrough | Guard added | None yet; re-checked in the browser |
| Errors when adding a team member weren't shown | Browser walkthrough | Inline status message | None yet; re-checked in the browser |
| The developer's real `.env` could leak into the test run | Audit | Tests ignore `.env` | `tests/conftest.py` |

## 5. Not tested

- A full live evaluation (free quota ran out). Backlog B-01.
- Real e-mail deliverability, real Sentry and alert webhook, real S3 restore, multi-core load.
- Gemini and xAI providers (tested against stubs only).

## 6. Open quality risks

| Risk | Impact | Tracked as |
|---|---|---|
| Live accuracy 57.5–60.9%, below the 70% release target | Wrong answers on harder questions | R-04, B-01, B-09 |
| Only single-table answers are independently verified | Most multi-table answers say "not independently verified" | B-10 |
| Reworded questions miss documents about 28% of the time | Some answers say "insufficient" | R-10, B-11 |

## 7. Sign-off

Released to `main` on 2026-11-18 with all build gates green. Re-run on 2026-11-25 for this report: every result in section 2 except the rehearsal (2026-11-07) comes from that day.
