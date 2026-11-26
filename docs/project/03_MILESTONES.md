# Milestones

Each milestone has exit criteria and the evidence that they were met. Dates are from Git commits, migrations, the changelog or recorded runs. Where a date isn't recorded, it says so.

| # | Milestone | Date | Exit criteria | Evidence | Status |
|---|---|---|---|---|---|
| M1 | MVP published on GitHub | 2026-07-01 | Question → SQL → answer works on the sample database | Commit `176b43c` "release QuerySure MVP" | Done |
| M2 | Production-safe baseline (Sprint 1) | Not recorded | Production refuses unsafe settings; migrations run before the app starts | `config.py` guards; `test_sprint5_docs.py` | Done |
| M3 | Background jobs and AI control plane (Sprints 2–3) | Not recorded | Uploads and reports run as jobs; structured output; prompt-injection tests; AI budgets | `test_sprint2_jobs.py`, `test_sprint3_prompt_injection.py` | Done |
| M4 | Observability and operations (Sprint 4) | 2026-10-04 (migration) | Logs without secrets, metrics, alerts, health checks, backup and restore, load test | Migration `5c1e0a4d9b21`; `loadtests/results/` | Done |
| M5 | v1.0.7 release | 2026-10-23 | Live evaluation on 5 real files, held-out set, negative cases; 0 known dependency vulnerabilities | CHANGELOG v1.0.7; [03_EVALUATION.md](../03_EVALUATION.md) section 3 | Done |
| M6 | Public demo on Render (PR #1) | 2026-10-27 | Runs on Render's free plan without a worker; auth e-mail over HTTPS; CI smoke tests green | Merge commit `a47f924` | Done |
| M7 | Team platform foundations (Sprints 0–6) | Not recorded | Workspaces, roles, audit trail, connectors, incremental sync, scheduler, data quality | [SPRINT_HISTORY.md](../SPRINT_HISTORY.md) | Done |
| M8 | AI features (Sprints 7–11) | Not recorded | Cited knowledge answers, assistant, agent with approvals, investigations, evaluation suites, AI usage report | Sprint 7–11 tests; `docs/baseline/platform_eval.txt` | Done |
| M9 | Launch readiness (Sprints 12–13) | 2026-11-07 (rehearsal) | Route authentication matrix, launch checklist, production-mode rehearsal passes | `scripts/launch_rehearsal.sh`; [LAUNCH_CHECKLIST.md](../LAUNCH_CHECKLIST.md) section 7 | Done |
| M10 | Redesign, audit and merge (PR #2) | 2026-11-18 | Journey navigation (Overview → Connect → Explore → Investigate → Analyze → Query → Knowledge); every page tested in a browser; full audit; CI green | Commit `ee949f6`, merge `57467bb` | Done |
| M11 | Live accuracy ≥ 70% | Not started | Full live run on the 62-case benchmark and the 5-file suite | Backlog B-01 | **Open** |
| M12 | Paid production hosting | Not started | Paid database and Key Value, S3, own e-mail domain, smoke test on the deployed URL | Backlog B-04, B-12, B-13 | **Open** |

## Slippage and what caused it

| Milestone | What happened | Response |
|---|---|---|
| M5 / M11 | The free Groq quota ran out during live runs, so live accuracy is measured on partial runs only | Built `scripts/live_quality.sh` so a full run is one command once a paid quota is available |
| M6 / M12 | The Render free PostgreSQL expired after 30 days (2026-11-25). A new database was created and the data reset | Risk R-08 had predicted this. Paid plan moved to "Now" on the roadmap |
