# Developer guide

## Setup
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # add GROQ_API_KEY to answer questions; tests need no key
python start.py                 # http://localhost:5000, SQLite, jobs run inline, mail goes to .dev_mail/
```
Docker (PostgreSQL + Redis + worker + scheduler): see the README. `config.py` is the only module that reads the environment; every variable must be listed in [ENVIRONMENT.md](ENVIRONMENT.md) (a test enforces it).

## Layout
| Area | Files |
|---|---|
| HTTP | `api.py` (auth, sessions, Text-to-SQL pipeline) and one `*_api.py` per feature, wired in `api.py` |
| Tenancy | `workspaces.py` (`require_role` is the only permission check), `audit.py` |
| Data | `ingest*.py`, `connectors/`, `quality.py`, `scheduler.py`, `storage.py` |
| AI features | `modules/` (Text-to-SQL), `knowledge.py`, `assistant.py`, `agent.py`, `investigation.py`, `ai/` (providers, router, trace) |
| State | `db.py` (models), `migrations/versions/` |
| Evaluation | `evaluation/` (SQL benchmark), `evaluation/platform/` (retrieval, agent, answers, safety, perf) |

## Conventions
- **One model job per feature.** Prompts put untrusted data inside delimiters; replies are parsed and validated by code before use. Never feed tool output or retrieved text back as instructions.
- **Every workspace route calls `workspaces.require_role`** (non-member 404, low role 403). `tests/test_s12_hardening.py` fails if a new `/api` route answers anonymous requests without being on the reviewed public list.
- **Audit details are allow-listed scalars**; never log question text, results or secrets.
- **Reuse the shared paths:** model calls go through `knowledge_api.generate_answer` or `sql_generator._run_provider_chain` (budget, key rules, trace); retrieval through `modules/retrieval_engine`. Do not add a second provider or retrieval system.
- New limits and switches go in `config.py` with a line in ENVIRONMENT.md.

## Tests
```bash
python -m pytest -q                       # SQLite, about 8 minutes
ruff check .
python -m evaluation.platform             # retrieval, agent (oracle), answers (oracle), safety
```
PostgreSQL/MySQL: start containers, set `DATABASE_URL`, `TEST_DATABASE_URL`, `TEST_SOURCE_POSTGRES_URL`, `TEST_SOURCE_MYSQL_URL`, run `alembic upgrade head && alembic check`, then pytest. Browser tests need `npm ci`; off Linux, set `CHROME_PATH` to a local Chrome. `tests/test_sprint5_docs.py` checks API.md and ENVIRONMENT.md against the code, so update them in the same change.

## Recipes
**Add a migration:** edit the model in `db.py`; `alembic revision --autogenerate -m "..."`; review it; then `alembic upgrade head`, `alembic check`, `alembic downgrade -1`, `alembic upgrade head`. Keep migrations additive.

**Add an agent tool:** add `Tool(name, description, params, "read"|"write", min_role, fn)` to `agent.TOOLS`. Validate every argument in `params`; a write tool must re-check authority itself. Add cases to `evaluation/platform/agent_eval.py` and `safety.py`, and tests in `tests/test_s9_agent.py`.

**Add a connector:** subclass the base in `connectors/base.py`, register it in `connectors/registry.py`, and send network access through `connectors/netguard.py` / `httpclient.py`.

**Change retrieval or prompts:** run `python -m evaluation.platform --suite retrieval`. The vector-index decision rule in `evaluation/platform/rag.py` is fixed beforehand and must not be edited to fit results.

**Add a route:** implement it, add its row to `API.md`, add role tests.

## Release checks
Follow [LAUNCH_CHECKLIST.md](LAUNCH_CHECKLIST.md); `scripts/launch_rehearsal.sh` rehearses the production stack with Docker. Package with `scripts/package_release.py`, never by zipping the folder.
