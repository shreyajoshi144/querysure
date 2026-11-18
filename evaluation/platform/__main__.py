"""``python -m evaluation.platform [--suite retrieval|agent|safety|all] [--mode oracle|replay|live] ...``

Runs against a throw-away SQLite database (never your real one). ``--mode live`` uses the real provider chain and costs money: it needs
``--yes`` and a configured key. Exit code 1 when a safety case is not blocked."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="qs-platform-eval-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/eval.db"      # before anything imports db
os.environ.setdefault("ENVIRONMENT", "development")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m evaluation.platform", description=__doc__)
    p.add_argument("--suite", choices=("retrieval", "agent", "answers", "safety", "all"), default="all")
    p.add_argument("--mode", choices=("oracle", "replay", "live"), default="oracle", help="agent and answers suites")
    p.add_argument("--responses", help="replay: JSON {goal id: raw model reply}")
    p.add_argument("--save-responses", help="live: write raw replies here for later replay")
    p.add_argument("--yes", action="store_true", help="live: confirm that real provider calls cost money")
    p.add_argument("--limit", type=int, default=0, help="answers suite: ask at most N questions (bounds live spend)")
    p.add_argument("--out", help="write the JSON report here")
    args = p.parse_args(argv)
    import db
    db.Base.metadata.create_all(db.engine)
    from . import agent_eval, answers, env as ev, rag, safety
    api_key = ""
    if args.suite in ("agent", "answers") and args.mode == "live":
        import config
        api_key = (config.GROQ_API_KEY or "").strip()
        if not args.yes or not api_key:
            print("live mode needs --yes and a configured GROQ_API_KEY (it spends money); refusing.", file=sys.stderr)
            return 2
    report = {}
    with db.SessionLocal() as session:
        env = ev.build(session)
        if args.suite in ("retrieval", "all"):
            report["retrieval"] = rag.run(env)
        if args.suite in ("agent", "all"):
            recorded = {}
            responses = json.load(open(args.responses)) if args.responses else None
            report["agent_plans"] = agent_eval.run(env, args.mode, responses, api_key, recorded if args.mode == "live" else None)
            if args.save_responses and recorded:
                json.dump(recorded, open(args.save_responses, "w"), indent=1)
        if args.suite in ("answers", "all"):
            rec2 = {}
            resp2 = json.load(open(args.responses)) if args.responses and args.suite == "answers" else None
            report["answers"] = answers.run(env, args.mode if args.suite == "answers" else "oracle", resp2, api_key, rec2 if args.mode == "live" else None, args.limit)
            if args.save_responses and rec2:
                json.dump(rec2, open(args.save_responses, "w"), indent=1)
        if args.suite in ("safety", "all"):
            report["safety"] = safety.run(env)
    text = json.dumps(report, indent=1, default=str)
    if args.out:
        open(args.out, "w").write(text + "\n")
    print(text)
    return 1 if report.get("safety", {}).get("failures") else 0


if __name__ == "__main__":
    sys.exit(main())
