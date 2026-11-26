# Project management pack

How QuerySure was planned, run, tested and closed. Every number here is the same number used in the rest of the repository, and each one points to its source. If two documents ever disagree, the code, the tests and the recorded runs in [docs/baseline/](../baseline/) are the source of truth.

| # | Document | Answers |
|---|---|---|
| 00 | [Project charter](00_PROJECT_CHARTER.md) | Why the project exists, objectives, scope, stakeholders, constraints |
| 01 | [PRD](01_PRD.md) | Problem, users, functional and non-functional requirements, out of scope, success metrics |
| 02 | [Roadmap](02_ROADMAP.md) | Phases delivered; Now / Next / Later |
| 03 | [Milestones](03_MILESTONES.md) | Dates, exit criteria, evidence, slippage |
| 04 | [Product backlog](04_PRODUCT_BACKLOG.md) | Open user stories with acceptance criteria; delivered epics |
| 05 | [Risk register](05_RISK_REGISTER.md) | Likelihood, impact, mitigation, status, including risks that occurred |
| 06 | [Test and QA report](06_TEST_QA_REPORT.md) | Test strategy, current results, defects found, what wasn't tested |
| 07 | [Project closure report](07_PROJECT_CLOSURE_REPORT.md) | Objectives vs outcome, schedule, budget, lessons learned, handover |

**Related records:** [Sprint history](../SPRINT_HISTORY.md) (every user story with acceptance criteria and evidence), [Decisions](../DECISIONS.md), [Capability map](../CAPABILITY_MAP.md), [Changelog](../../CHANGELOG.md).

## Project at a glance

| | |
|---|---|
| Owner | Shreya Joshi (solo: product, engineering, QA), using AI coding assistance |
| Duration | 2026-07-01 → 2026-11-18 |
| Delivery | 7 MVP sprints, 14 platform sprints, 2 merged pull requests |
| Requirements | 19 of 21 done, all P0s |
| Quality | 1,504 tests passing; 36/36 safety cases; deterministic pipeline 100% on 212 runs |
| Known gap | Live model accuracy 57.5–60.9% exact against a 70% release target |
