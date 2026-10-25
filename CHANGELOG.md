# Changelog

## Unreleased — final release package

### Fixed
- **Compose worker healthcheck.** The `worker` service probed `http://localhost:5000/api/health` with `curl`, but the image has
  no `curl` and the worker serves no HTTP, so it could never be healthy. It now checks for its own Redis heartbeat key
  (`qs:g:worker:<hostname>:*`, published by `worker.py` every 2 s with a 30 s TTL); `start_period: 20s` added.
  `tests/test_v103_render_blockers.py` pins this and runs the exact command against an in-memory Redis.
- **Compose `ANTHROPIC_FALLBACK_MODEL` default** changed from the retired `claude-3-5-haiku-20241022` to
  `claude-haiku-4-5-20251001`, matching `config.py`.

### Accuracy and reliability (found by the new multi-format evaluation, `python -m evaluation.multiformat`)
- **Retrieval showed the model the wrong tables.** Tokens were lower-cased before splitting and never stemmed, so
  `OrderDetail`, `CustomerId` and "orders" never met; only the 5 best text matches were sent. Retrieval recall (every table
  the correct SQL needs is in the prompt) was 8.8% on Northwind and 48% on Chinook. Tokens are now CamelCase-split and
  plural-stemmed; schemas of at most 150 columns are shown in full; larger ones get the tables that connect the selection.
- **Short words grounded as filter values.** "is", "de", "es", "no", "by", German "war" matched stored codes (`IS`, `DE`, ...)
  and a track titled "Seconds"; the plan then demanded `code = 'IS'` and asked the model to "repair" correct SQL. Values of
  3 characters or less, and one-word values in columns with more than 200 distinct values, must appear as stored.
- **Planner:** CamelCase-aware column matching; the date range uses the column the question names (`shipped` ->
  `ShippedDate`), never just the first date column; "which industries" no longer forces `LIMIT 1`; "most customers" and
  frequency questions ("appears most") are counts; "number of days required" sums the measure; "50 million" is 50,000,000;
  "average progress below 20%" is a HAVING (not a row filter); "top-level" is not "top N"; median and per-month/per-year
  questions are not checked by the single-value reference query; a value found in several columns accepts any of them;
  a column name shared by several tables is ambiguous for grouping.
- **Checks no longer flag correct SQL:** `SUM(CASE WHEN ... THEN 1 ELSE 0 END)` counts; conditional aggregation may compare
  one column with several values; only the first ORDER BY key sets the ranking direction; counting words in Spanish,
  French, German, Portuguese, Italian, Dutch and Hindi. New check: "top 5 genres by revenue" answered with `COUNT(*)` only.
- **Validator:** `SELECT *` is accepted with a warning (read-only; was rejected). `<table>Id -> <table>.<key>` joins are
  inferred with up to 20% orphan rows (Northwind `Order.CustomerId`: 29 of 830), and the validator warns that an inner join
  drops them; a column named after a table (`ShipVia` -> `Shipper.Id`) is inferred when every value matches.
- **Validator:** two CTEs or subqueries joined on their only GROUP BY key (per-industry profit joined to per-industry sales)
  are accepted as one-to-one; they were rejected as an unknown relationship. Self-joins and other undeclared joins are
  still rejected.
- **New check:** a column selected next to GROUP BY that is neither grouped nor aggregated is flagged; SQLite returns an
  arbitrary row's value for it (seen live: each project's "finish date"). `MIN`/`MAX` queries and groups keyed by a
  primary or unique column are left alone.
- **Evaluation:** `python -m evaluation.multiformat` (tuned, `--holdout`, `--negatives`, `--live --yes`, `--replay`) over
  CSV, Excel, JSON and two SQLite databases, with answers computed independently with pandas; live reports separate calls
  the provider refused from wrong answers.
- **Deterministic repair:** values written in double quotes (`= "Unadjusted"`) become string literals when they are not a
  table, column or alias, before any LLM repair round.

### Slow sign-up and missing verification e-mails on Render's free plan
- **Cause:** verification and reset e-mails were sent only by the background worker (a paid Render service), over SMTP, which
  Render blocks on free instances; each blocked SMTP step waited 15 s and a failed e-mail was retried three times with
  sleeps, even for permanent errors (bad credentials, missing settings).
- `EMAIL_PROVIDER=brevo` or `resend` sends over HTTPS (port 443) with `EMAIL_API_KEY`; one bounded request
  (`EMAIL_TIMEOUT_SECONDS`, default 10, also used for each SMTP step).
- `AUTH_EMAIL_DELIVERY=auto` (default): e-mails are queued when a worker is alive, otherwise sent by the web process right
  after the response, so sign-up never waits for the mail server and works without a worker. Tokens, expiry and one-time use
  are unchanged.
- Permanent e-mail failures are no longer retried; transient ones (timeouts, 429, 5xx) still are.
- Logs record sign-up timings (hashing, dispatch, total) and each send's provider, outcome and duration, never the address,
  link or token. `render.yaml` takes `EMAIL_PROVIDER` and `EMAIL_API_KEY` from the dashboard.

### Hierarchical statistics files (Stats NZ style)
- **Cause of 22 of 35 wrong live answers:** in the Stats NZ CSV every figure exists at two industry levels (NZSIOC Level 1
  and Level 2, which add up to the same totals; 9 industries such as Mining appear at both) and in three versions
  (unadjusted, seasonally adjusted, trend). The model summed across them, grouped by the level column, or wrote
  `suppressed <> 'Y'` on a column that is empty on 99.8% of rows (which drops every row).
- **`modules/long_format.py`** detects such tables from the data alone: no row identifier, a key of few-valued columns, a
  column whose values hold the same totals at different granularities (hierarchy level; top level = fewest entities),
  a column whose values give the same per-series totals over the common periods (versions), and 1:1 relabelled
  columns (aliases). Nothing is keyed to column names or this dataset.
- The prompt describes the structure; the planner fixes one level and one version from the question ("top-level",
  "level 2", a named version; otherwise the top level and the most complete version), which the reference query uses;
  the semantic check flags SQL that adds levels or versions together, groups by the level column, or uses `<>`/`NOT IN`
  on a mostly-NULL column, so the repair round fixes it. An entity that exists at one level needs no level filter.
- Measured on the model's recorded CSV SQL: 21 of 23 wrong answers are now flagged; 0 of 18 correct answers are.
- Second round, from a live run on the CSV: a variable the question names by a word unique to one stored value
  ("sales" -> 'Sales (operating income)') must be filtered; selecting the level column as the industry name is
  flagged; a long-format table may be joined with itself on its key columns (putting wages next to sales); a
  question asking for a percentage answered with a bare ratio is flagged; and when the missing level, version or
  variable has one right value, the filter is added deterministically (listed under the answer's fixes) instead of
  hoping the model's repair adds it.

### Workspace and History
- **Verifier false alarm (seen live on the sample data):** "Which region placed the most orders?" was checked against a
  count of *stores* per region and reported *Mismatch* on the correct join. "The most <table>" now names the counted
  table, so such answers are "not independently verified"; "the most stores" is still verified and a wrong count is still
  caught.
- **Share-of-total findings on top-N results:** "North contributes 100.00% of total order_count" was computed from the
  one returned row. When the SQL has a LIMIT, the denominator is now recomputed without it (same filters and grouping,
  read-only); if it cannot be, no share is stated. "Top two contributors" needs at least two rows.
- A failed upload shows the worker's reason ("Excel workbook is not a valid ZIP container."); a dataset switch also
  clears the hidden answer sheet's question text.
- Header: between 861 and 1,180 px wide, a long dataset name pushed the Ask/Datasets/Evaluation/History tabs out of
  view; the tabs no longer shrink and the dataset name truncates instead (found in the end-to-end browser run).
- "Low confidence" is no longer set from the retrieval score for clearly non-English questions (it compares words with
  English schema names): on the multi-format suite, Low on correct answers fell from 86 to 26 of 212; English unchanged.
- Connecting a new upload or the sample clears the previous question, answer, SQL, results and verification; Ask is locked
  while an upload runs; a failed upload leaves the current dataset active; late preview responses for an older dataset are
  dropped.
- History: Reopen and Rerun removed; entries are read-only snapshots ("Result not saved" when none was stored); Delete
  asks for confirmation and reports a failed delete.

### Release packaging and credentials
- **A hand-made archive of the folder carried `.env`** (live API keys, SMTP password, JWT secret), both local databases,
  the owner's private evaluation files, `.idea/`, caches and `__MACOSX` metadata: Finder's *Compress* ignores
  `.gitignore`. `scripts/package_release.py` now builds the release ZIP with its own exclusion rules, writes it with
  Python's `zipfile` (no macOS metadata), re-reads it, and deletes it if an excluded path, a value from the local `.env`
  or a real-looking key is found (file names only are printed). `--scan-git` scans the files git tracks.
- **`docker-compose.yml` no longer contains credentials.** The PostgreSQL password and the JWT secret come from `.env`
  (`POSTGRES_PASSWORD`, `JWT_SECRET_KEY`); Compose stops with a message when they are missing. An existing volume keeps
  working when `.env` holds the password it was created with.
- `.gitignore` also excludes `.idea/`, `*.sqlite`, `*.sqlite3`, `.deepeval/` and `tests/fixtures/user/`.

### Docs
- README rewritten; broken `docs/portfolio/` links fixed.
- Documentation consolidated into `docs/01_PRODUCT.md`, `02_ARCHITECTURE.md`, `03_EVALUATION.md` and `SPRINT_HISTORY.md`, plus the
  reference documents that code and tests depend on (`API.md`, `ENVIRONMENT.md`, `BILLING.md`, `OPERATIONS.md`).
  `DEPLOYMENT.md` and `BACKUP_RESTORE.md` were merged into `OPERATIONS.md`; `ARCHITECTURE.md`, `AGILE_DEVELOPMENT.md`,
  `SECURITY_PRIVACY.md`, `LOAD_TEST_RESULTS.md` and `FORMAT_RELIABILITY_v1.0.5.md` were folded into the core documents.

## v1.0.7 — release (2026-10-23)

### Fixed
- **LLM defaults no longer point at retired models.** Groq shut down `llama-3.3-70b-versatile` and `llama-3.1-8b-instant`
  on 2026-08-16 and Anthropic retired `claude-3-5-haiku-20241022`, so generation failed with only a Groq key. Defaults are
  now `openai/gpt-oss-120b` → `openai/gpt-oss-20b` (sent `reasoning_effort=low`, output cap `LLM_MAX_OUTPUT_TOKENS`) and
  `claude-haiku-4-5-20251001`; prices updated. All IDs stay overridable.
- **Result verifier false alarms.** The planner no longer builds a reference query when the counted entity is a different
  table from the grouping column, when a threshold applies to an aggregate (HAVING), or when the question names a period it
  could not parse. "At least / at most" are thresholds, not rankings; "how many days" is a duration, not a row count.
  On the 44 gold benchmark queries, false `mismatch` verdicts went from 3 to 0; real mismatches are still caught.
- **"Low confidence" recalibrated** from a 0.15 to a 0.05 retrieval-score cutoff (`LOW_RETRIEVAL_CONFIDENCE`): it no
  longer fires on 82% of correct answers.
- **Answer sentence** now states what the rows show ("North has the highest revenue at …") instead of a fixed phrase.
- **Report failures** after an answer is shown no longer mark the whole run as stopped.
- **`truncated`** is reported only when rows were actually cut (a result of exactly 1,000 rows was flagged).
- **First local run**: `.env.example` now uses `EMAIL_PROVIDER=dev` and `WEB_CONCURRENCY=1`; SQLite table creation
  retries when two workers race. A fresh clone's test run builds the sample database automatically.
- **Production start-up** no longer requires `GOOGLE_CLIENT_ID`; without it Google sign-in is hidden and returns 503.
- Production error message no longer tells users to add an API key in Settings (not allowed in production).

### Security
- FastAPI 0.115.6 → 0.159.1 (Starlette 0.41.3 → 1.9.0); `python-jose` replaced by `PyJWT[crypto]`; `certifi`, dev
  `aiohttp` and `pytest` bumped. `pip-audit`: no known vulnerabilities. CI now runs `pip-audit`.
- Admin-route and documented-route tests walk FastAPI's new included-router wrappers, so they still check every route.

### Removed
- Internal sprint/merge notes and superseded reports: `MERGE_NOTES.md`, `docs/SPRINT5_CLOSURE.md`,
  `docs/RC_REPORT_UI_POLISH.md`, `docs/UI_AUDIT_REPORT.md`, `docs/DEPLOY_AUDIT.md`, `docs/REDESIGN_v1_0_7.md`,
  `docs/FORMAT_RELIABILITY_v1.0.4.md`, `e2e/autofill_repro.mjs`; the generated `retail.db` is no longer shipped.

### Docs
- New portfolio set in `docs/portfolio/` (product case study, architecture, evaluation; Markdown + PDF) and README.
