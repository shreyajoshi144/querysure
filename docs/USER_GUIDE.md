# User guide

For people using a running QuerySure. Operators: see [OPERATIONS.md](OPERATIONS.md).

After you sign in you land on **Overview**. The top bar follows the order you normally work in:

**Overview → Connect → Explore → Investigate → Analyze → Query → Knowledge**

Members, activity, AI usage and the evaluation benchmark are in the account menu (top right). Every page has a **How it works** chart; select a step to see exactly what happens there.

## Workspaces and roles
Every account has a private **personal workspace**. Create a **team workspace** (account menu → Team, audit and AI usage → Manage) to share data and documents, then add members who already have a verified account. Pick the active workspace in the page header.

| Role | Can |
|---|---|
| viewer | explore datasets, query, search and ask documents, use the Assistant |
| analyst | everything a viewer can, plus upload and re-import data, run analyses, start agent investigations and approve their write steps |
| admin | everything an analyst can, plus manage members, delete documents, see activity and AI usage |
| owner | everything an admin can, plus delete the workspace |

Someone who is not a member of a workspace cannot tell that it exists.

## Overview
A map of the six workspaces with an **Open** button for each, the sources connected to this workspace, its health (datasets not ready or with quality problems, failed syncs, approvals waiting, recent imports) and recent activity.

## Connect
- **Upload a data file:** CSV, JSON, Excel (XLSX/XLSM), Parquet or SQLite. It is imported in the background and becomes a versioned, quality-checked dataset.
- **Use the sample retail database** to try the product; it is connected to your session, not added to the workspace catalog.
- **Database connections:** PostgreSQL or MySQL, read-only; the password is encrypted and never shown again. Test the connection, then import a table. Connection datasets can sync manually or on a schedule (full or incremental); a schedule that keeps failing is switched off and says why.
- **Import runs** show what each import loaded, quarantined or rejected. A failed import never replaces the previous version.

## Explore
The workspace dataset catalog (version, tables, rows, size, quality) and the schema explorer: tables, columns, keys, relationships and a paged, read-only preview. **Open** a dataset to make it the active one for Query.

## Investigate
Describe a goal ("list my datasets and their quality"). The agent plans up to six steps from fixed tools: list datasets, list documents, search documents and show data quality run immediately; sync a dataset and delete a document are **write** steps that wait for **Approve**. Approvals expire after 24 hours and the approver's role is checked again when the step runs. The agent cannot run SQL, shell commands or web requests.

## Analyze
Pick a ready dataset, then **trend** (a measure over time), **comparison** (two periods) or **breakdown** (a measure by category). Results are charted (line for periods, bar for categories) with the table underneath. Each fact keeps its SQL; hypotheses are rule-based and labelled *unverified*. Download a report as JSON or HTML. Very large tables are refused.

## Query
Ask in plain English. The answer, chart and result table come first; underneath are expandable panels for execution metrics and confidence, the SQL, validation, correctness verification, data integrity and the trace. The run stages show while a question runs, then fold away (**Show stages** reopens them).
- "Independently verified" means a second query recalculated the result. "Not independently verified" means it could not be confirmed (most multi-table questions); treat it as a draft.
- Only one read-only `SELECT` runs against your data. Results are capped at 1,000 rows.
- **History** keeps read-only snapshots of earlier answers.

## Knowledge
Upload `.txt`, `.md`, `.pdf` (text layer only, no OCR) or `.docx` files. Ask a question or use **Search passages**; answers cite numbered passages you can open. The **Assistant** is a chat over the same documents that handles short follow-ups. Chats are private to you.
- If no passage matches, nothing is generated; questions that look like data questions are pointed to Query.
- If the model's answer cites nothing valid, it is withheld rather than shown.
- A citation proves the passage exists, not that the answer is right.
- Search matches wording, not meaning: about 28% of paraphrased questions miss in our test set. Use the document's own words.

## Your account
Settings lets you export your data, revoke sessions and delete your account (re-enter your password and type your e-mail).

## Troubleshooting
| You see | Meaning |
|---|---|
| Dataset stays *queued* | The background worker is not running; ask your operator. |
| "Verify your email before signing in" | Open the link in the verification e-mail (check spam) or use **Resend verification email**. |
| `INDEX_FULL` on upload | The workspace holds its maximum number of document passages; delete documents first. |
| `TABLE_TOO_LARGE` | The table is over the analysis row limit. |
| "Daily AI budget reached" | The per-user or global AI budget is used up; it resets daily. |
| An answer is "withheld" | The model did not cite the documents, so QuerySure would not show it. Rephrase and retry. |
