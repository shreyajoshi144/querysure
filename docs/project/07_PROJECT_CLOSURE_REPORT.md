# Project closure report

| | |
|---|---|
| **Project** | QuerySure |
| **Owner** | Shreya Joshi |
| **Period** | 2026-07-01 to 2026-11-18 (closure written 2026-11-25) |
| **Final state** | `main` at merge `57467bb` (PR #2). CI green. Open work handed to the [product backlog](04_PRODUCT_BACKLOG.md) |
| **Overall result** | **Delivered.** Four of five objectives fully met. The live-accuracy target was not met; it was measured and published honestly |

## 1. Objectives vs outcome

| Objective (from the [charter](00_PROJECT_CHARTER.md)) | Outcome | Met? |
|---|---|---|
| O1 Answer data questions without letting the model change or leak data | Four safety layers; 36/36 attacks blocked in CI | Yes |
| O2 Make every answer checkable | SQL, validation findings, verification status and trace on every answer; History snapshots | Yes |
| O3 Measure AI quality honestly | Deterministic pipeline 100% on 212 runs (180 tuned, 32 held-out) after fixes; retrieval with confidence intervals; live model 57.5% (tuned) and 60.9% (held-out) exact on answered questions (release target 70%) | Partly: method met, live target not met |
| O4 Grow into a team platform | Workspaces, roles, connectors, knowledge hub, agent, investigations, governance | Yes |
| O5 Be deployable and operable | CI with 6 jobs, migrations on boot, launch checklist, production-mode rehearsal passed, Render free demo | Yes on one machine and the free demo; paid hosting is open |

## 2. Scope delivered

- **19 of 21** functional requirements done (all P0s). Not done: clarifying questions (FR-20) and live payments (FR-21). See the [PRD](01_PRD.md#5-functional-requirements).
- 7 MVP sprints, release finalisation, 14 platform sprints (0–13), a journey redesign and a pre-merge audit.
- Out of scope by decision: vector database, autonomous agent loop, writing back to data.

## 3. Schedule

| Planned milestone | Actual |
|---|---|
| MVP published | 2026-07-01 |
| v1.0.7 release | 2026-10-23 |
| Public demo (PR #1) | 2026-10-27 |
| Launch rehearsal | 2026-11-07 |
| Platform merged (PR #2) | 2026-11-18 |
| Live accuracy ≥ 70% | Not reached (blocked by free quota) |
| Paid production hosting | Not started |

Full list: [03_MILESTONES.md](03_MILESTONES.md).

## 4. Quality

Summary of the [QA report](06_TEST_QA_REPORT.md): 1,504 passed, 0 failed, 47 skipped on SQLite; 1,520 passed, 0 failed, 32 skipped (MySQL-source tests skipped: no MySQL server in this run) on PostgreSQL 16; 94/94 browser end-to-end checks; 36/36 safety cases; Ruff and `pip-audit` clean; migrations up, down and up clean.

## 5. Budget

| Item | Spend |
|---|---|
| Hosting | $0 (Render free plan) |
| AI | Free Groq quota; the largest recorded live run cost $0.05 |
| Tools | Free tiers (GitHub, GitHub Actions) |

The budget constraint shaped the project: the free AI quota is the reason live accuracy is measured only on partial runs.

## 6. Lessons learned

| What happened | Lesson | What I'd repeat or change |
|---|---|---|
| Measuring found real defects: 3 of 44 correct answers flagged as wrong, "Low confidence" on 81.8% of correct answers, 10 correct queries rejected | Without independent expected answers, these would have shipped as "working" | Repeat: write the evaluation before claiming a result |
| The vector-database rule was written before the numbers | Pre-committing stops you from moving the goalposts | Repeat |
| Held-out test cases found 2 defects the tuned set missed | Testing only on what you tuned on overstates quality | Repeat |
| Groq retired the default models; the free database expired; e-mail went to spam | Free and third-party dependencies fail on their own schedule | Change: budget for paid infrastructure before a public demo |
| The agent rejected every plan because the prompt didn't state the allowed argument ranges | Prompt quality matters, but validation is what makes the failure visible | Repeat: validate model output strictly, then fix the prompt |
| Scope grew from a single tool to a platform | A capability map classifying every feature as Working / Partial / Missing kept claims honest | Change: set the platform scope at the start instead of mid-project |

## 7. Handover

| Item | Where |
|---|---|
| Open work | [04_PRODUCT_BACKLOG.md](04_PRODUCT_BACKLOG.md) |
| Open risks | [05_RISK_REGISTER.md](05_RISK_REGISTER.md) |
| How to run, deploy and operate | [DEVELOPMENT.md](../DEVELOPMENT.md), [LAUNCH_CHECKLIST.md](../LAUNCH_CHECKLIST.md), [OPERATIONS.md](../OPERATIONS.md) |
| Design decisions | [DECISIONS.md](../DECISIONS.md) |
| Evidence | [03_EVALUATION.md](../03_EVALUATION.md), [docs/baseline/](../baseline/) |

## 8. Sign-off

Closed by the owner on 2026-11-25. Next step: paid hosting and a full live evaluation (roadmap "Now").
