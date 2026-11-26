# Project charter

| | |
|---|---|
| **Project** | QuerySure: verified AI answers over company data |
| **Owner** | Shreya Joshi: product owner, engineer and QA (solo project) |
| **Repository** | `github.com/shreyajoshi144/querysure` |
| **Period** | 2026-07-01 (first public release) to 2026-11-18 (AI Operations Platform merged to `main`, PR #2) |
| **Status** | Closed. Delivered and handed to the backlog. See the [closure report](07_PROJECT_CLOSURE_REPORT.md) |
| **Way of working** | Sprints with a goal, user stories and acceptance criteria per item, tests as evidence, AI coding assistance (Claude Code) under the owner's direction (visible as `Co-Authored-By` lines in the Git history) |

## 1. Purpose

People who don't write SQL keep asking for numbers they can't get themselves. "Chat with your data" tools remove the wait, but a wrong answer sounds exactly as confident as a right one. QuerySure answers in plain English **and shows the evidence**: the SQL, every check it passed, and whether the result was independently recalculated.

## 2. Objectives

| # | Objective | Measure of success | Result |
|---|---|---|---|
| O1 | Answer data questions without letting the model change or leak data | 0 writes or validator escapes in the safety suite | **Met.** 36/36 attacks blocked, run in CI |
| O2 | Make every answer checkable | Each answer carries SQL, validation findings, a verification status and a trace | **Met** |
| O3 | Measure AI quality honestly before claiming it | Held-out test set, answers computed independently, confidence intervals, limits published | **Met** for the deterministic pipeline and retrieval. Live model accuracy measured only on partial runs (57.5–60.9% exact), **below the 70% release target** (long-term goal 99%) |
| O4 | Grow from a single-user tool into a team platform | Workspaces, roles, connectors, knowledge hub, controlled agent, audit trail | **Met** (AI Operations Platform, Sprints 0–13) |
| O5 | Be deployable and operable | CI gates, migrations on boot, launch checklist, production-mode rehearsal | **Met** on one machine (rehearsal passed 2026-11-07). A Render free-plan demo exists. Paid production hosting is not set up |

## 3. Scope

**In scope:** Text-to-SQL over uploaded files and connected sources, workspaces and roles, knowledge hub with cited answers, controlled agent with human approval, investigations, AI provider layer with budgets, evaluation harness, deployment tooling, documentation.

**Out of scope:** payments with a live provider, MFA, clarifying questions, vector search, autonomous agent loops, multi-table independent verification. The reasons for each are in the [PRD](01_PRD.md#7-out-of-scope).

## 4. Stakeholders

| Stakeholder | Interest | How they were represented |
|---|---|---|
| Operations / business analyst at a small company (primary user) | Fast answers from their own files | Personas and user journeys in the [PRD](01_PRD.md) |
| Founder or manager | Knowing whether a number is safe to report | "Verified / not independently verified" label on every answer |
| Reviewer who signs off an AI number | The exact SQL and checks | SQL, validation, trace and History snapshots |
| Workspace admin | Control over who sees and changes data | Roles, audit trail, approval gates, AI usage report |
| Operator | A deploy that fails loudly instead of running unsafely | Production guards, launch checklist, rehearsal script |

No real users have used the product yet. User needs come from the problem analysis, not from interviews.

## 5. Constraints and assumptions

| Type | Item |
|---|---|
| Budget | Free tiers only: Groq free quota for live AI runs, Render free plan for the demo. The free quota stopped full live evaluations |
| People | One person in every role. Reviews were done through tests, CI and written acceptance criteria |
| Technology | Python/FastAPI and vanilla JS. Model providers retire models without notice |
| Assumption | Users can export their data to a file, or give read-only database credentials |

## 6. Governance

- **Definition of done:** acceptance criteria met, a test proving it, CI green (lint, tests on SQLite and PostgreSQL, migrations, dependency audit, Docker build), docs updated.
- **Evidence labels:** every claim in these documents is *Verified* (backed by code, a test or a recorded run), *Reconstructed* (inferred from evidence) or *Not done*.
- **Decision record:** [DECISIONS.md](../DECISIONS.md).
- **Change control:** pull requests into `main` (PR #1 Render demo, PR #2 AI Operations Platform).
