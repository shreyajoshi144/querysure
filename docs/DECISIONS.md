# Design decisions

Short records of choices a reviewer is likely to question: what was decided, why, and what would change it.

1. **The model writes SQL and nothing else in the Text-to-SQL path.** Grounding, planning, validation, execution and verification are deterministic and tested, so failures are attributable and the validator is the real gate. *Revisit if* live accuracy shows the planning rules cap quality.
2. **Four independent SQL safety layers** (AST allow-list, read-only connection, SQLite authorizer, time/row/size caps). Any one failing must not expose data.
3. **Retrieval stays lexical (TF-IDF) with an inverted index; no vector database.** One implementation serves schema and document search. A decision rule was fixed before measuring (recall@3 floors with Wilson 95% intervals); current result: not justified. *Revisit if* live results show paraphrase misses drive wrong or missing answers.
4. **Retrieved documents are untrusted input.** They sit inside delimiters declared as evidence only, delimiter look-alikes are escaped, and an answer that cites nothing valid is withheld. Citations prove existence, not support.
5. **The agent is a one-shot validated plan, not a loop.** Tool output is never fed back to the model, write steps need a person's approval (atomic claim, expiry, role re-check), and the tool set is closed. This removes the main prompt-injection path at the cost of flexibility. No LangGraph.
6. **Investigations are templates over real queries** (trend, comparison, breakdown); hypotheses are rule-based and labelled unverified, rather than free-form generated analysis.
7. **Workspace tenancy with one permission function.** Roles viewer < analyst < admin < owner; non-members get 404 so existence is not leaked; an anonymous-access test covers every route.
8. **The audit log is append-only with allow-listed scalar details**, so it cannot become a copy of user data.
9. **A curated SQLite file per dataset in object storage.** Imports write a new version and swap it only on success; queries use the same read-only sandbox as uploads. *Limit:* single-node reads; tables above 500,000 rows are refused for investigations.
10. **AI budgets are reserved before a model call and settled after**, with a kill switch; one provider chain and one trace for every AI task.
11. **Vanilla JS front end, no framework migration.** Strict CSP and escaped rendering; the UI is small enough that a build pipeline adds more risk than value.
12. **Honest evaluation.** Oracle modes are labelled self-checks; unmeasured things (live plan and answer quality, PostgreSQL load, real mail delivery) are listed as unmeasured in the README and checklist.
