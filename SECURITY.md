# Security policy

Report suspected vulnerabilities privately to the maintainer; do not open a public issue with exploit details. Include the affected route or file, steps to reproduce and the impact.

Controls are described in [docs/02_ARCHITECTURE.md](docs/02_ARCHITECTURE.md) (Security controls) and checked by `evaluation/platform/safety.py` (36 adversarial cases) and `tests/test_s12_hardening.py` (anonymous access to every route). Never commit `.env`, keys or local databases; package releases with `scripts/package_release.py`, which scans for secrets.

Known gaps: no MFA; the access token is kept in `localStorage` (mitigated by CSP and escaping); behaviour of live models under attack is not measured.
