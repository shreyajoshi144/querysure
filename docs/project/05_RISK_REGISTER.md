# Risk register

**Likelihood (L) and impact (I):** 1 low to 3 high. **Score** = L × I. **Status:** *Open*, *Mitigated* (controls in place and tested), *Accepted* (known and published), *Occurred* (happened; response recorded).

| ID | Risk | L | I | Score | Mitigation | Evidence | Status |
|---|---|---|---|---|---|---|---|
| R-01 | The model writes SQL that changes or deletes data | 2 | 3 | 6 | Four independent layers: syntax-tree allow-list, read-only connection, SQLite authorizer, time/row/size caps | `test_execution_guardrails.py`; safety suite 36/36 | Mitigated |
| R-02 | Text inside an uploaded file or document steers the AI (prompt injection) | 3 | 3 | 9 | Untrusted text fenced and escaped; the agent never sees tool output; an uncited answer is withheld | `test_sprint3_prompt_injection.py`, `test_injection_in_documents_and_names_cannot_add_steps` | Mitigated |
| R-03 | One team sees another team's data | 2 | 3 | 6 | One permission function; non-members get 404; anonymous-access test on every route | `test_s1_workspaces.py`, `test_s12_hardening.py` | Mitigated |
| R-04 | A confident but wrong answer is trusted | 3 | 3 | 9 | Verified only after independent recalculation; "not independently verified" label; SQL always shown | `result_verifier.py`; live accuracy 57.5–60.9% | **Open**: reduced, not removed |
| R-05 | AI costs run away on a public demo | 2 | 2 | 4 | Budget reserved before each call ($2 per user per day, $25 global); kill switch; daily quotas | `test_sprint3_token_accounting.py` | Mitigated |
| R-06 | A provider retires a model and generation stops | 3 | 2 | 6 | Fallback chain across 4 models; model IDs in configuration | Groq retired the previous defaults on 2026-08-16; fixed in v1.0.7 | **Occurred**, mitigated |
| R-07 | Free AI quota blocks evaluation | 3 | 2 | 6 | One-command live run ready for a paid quota | Live runs stopped by quota (Run 4 held-out) | **Occurred**, open (B-01) |
| R-08 | Free hosting deletes data (database expires after 30 days; Key Value not persistent) | 3 | 2 | 6 | Documented in README and launch checklist; paid plans before real users | The Render free database expired on 2026-11-25 and was replaced | **Occurred**, open (B-04) |
| R-09 | Verification e-mails land in spam, so users can't sign in | 3 | 2 | 6 | Send from a verified domain | Brevo showed "delivered"; mail was in spam | **Occurred**, open (B-12) |
| R-10 | Reworded questions miss the right document | 3 | 1 | 3 | Decision rule fixed before measuring; gap published | Paraphrase recall@3 0.72 | Accepted |
| R-11 | Session token in `localStorage` is exposed by an XSS bug | 1 | 3 | 3 | Strict CSP; no `innerHTML` in workspace code; escaping tests | `test_xss_escaping.py`; backlog B-07 | Open, low |
| R-12 | Burst traffic deadlocks the server | 2 | 3 | 6 | Admission control (`API_ADMISSION_LIMIT`) returns 503 instead of hanging | Load test: 160 errors at 200 users before; all 200 OK after | **Occurred** in testing, mitigated |
| R-13 | Vulnerable dependencies | 2 | 2 | 4 | `pip-audit` in CI | 14 known vulnerabilities → 0 (v1.0.7) | Mitigated |
| R-14 | Secrets or private data shipped in a release | 1 | 3 | 3 | Packaging script with exclusions and a post-build scan; `.env` ignored | `test_release_packaging.py`, `--scan-git` | Mitigated |
| R-15 | Scope grows faster than quality (solo project) | 2 | 2 | 4 | Capability map classifies every feature Working / Partial / Missing; done means a test proves it | [CAPABILITY_MAP.md](../CAPABILITY_MAP.md) | Mitigated |
| R-16 | Local uploads disappear when containers are rebuilt | 2 | 1 | 2 | Documented; production requires S3 | README | Accepted |

## Top risks now

1. **R-04** wrong answers trusted: the product's core risk. The response is transparency now and better accuracy next (B-01, B-09, B-06, B-10).
2. **R-08 / R-09** demo reliability: both have already happened. Low effort to fix (B-04, B-12).
3. **R-07** no full live evaluation: blocks the one success metric still below target.
