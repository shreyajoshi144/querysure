"""python -m evaluation.multiformat [--data DIR] [--only ID_PREFIX] [--json OUT.json] [--holdout] [--live --yes | --replay LIVE.json]"""
from __future__ import annotations

import argparse
import json
import sys

from .runner import render, run


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m evaluation.multiformat")
    p.add_argument("--data", help="directory with the source files (default: $QUERYSURE_EVAL_DATA or tests/fixtures/user)")
    p.add_argument("--only", help="run only cases whose id starts with this prefix")
    p.add_argument("--json", help="write the full report as JSON to this path")
    p.add_argument("--holdout", action="store_true", help="run the held-out cases (never used for tuning)")
    p.add_argument("--live", action="store_true", help="let the real LLM provider chain write the SQL (COSTS MONEY; needs --yes)")
    p.add_argument("--yes", action="store_true", help="confirm spending provider credits in --live mode")
    p.add_argument("--delay", type=float, default=0.0, help="seconds to wait between live questions (rate limits)")
    p.add_argument("--replay", help="re-check the model SQL recorded in an earlier --live --json report (no LLM calls)")
    p.add_argument("--negatives", action="store_true", help="score deliberately wrong SQL (detection rate) instead")
    a = p.parse_args(argv)
    if a.negatives:
        from .runner import run_negatives
        neg = run_negatives(a.data)
        print(f"negative cases: {neg['cases']}  detected (detectable): {neg['detected_of_detectable']}/{neg['detectable']}"
              f"  detected (hard): {neg['detected_of_hard']}/{neg['hard']}  of which corrected to the right answer: {neg['corrected']}")
        for r in neg["results"]:
            print(f"  {'DETECTED' if r['detected'] else 'missed  '} {r['id']:7s} {r['mistake']:55s} {'' if r['detectable'] else '(hard)'} {'; '.join(r['signals'])[:160]}")
        if a.json:
            with open(a.json, "w", encoding="utf-8") as fh:
                json.dump(neg, fh, indent=2, default=str)
        return 0
    if a.live:
        if not a.yes:
            print("error: --live calls paid LLM APIs; re-run with --yes to confirm.", file=sys.stderr)
            return 1
        from ..cli import load_llm_env_from_dotenv
        load_llm_env_from_dotenv()             # LLM variables only, before config is imported
    replay = None
    if a.replay:
        with open(a.replay, encoding="utf-8") as fh:
            replay = {(r["id"], r["lang"]): r.get("sql") for r in json.load(fh)["results"] if r.get("sql")}
    report = run(a.data, a.only, holdout=a.holdout, live=a.live, delay_seconds=a.delay, replay=replay)
    print(render(report))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
    return 0 if report["metrics"]["overall"]["cases"] else 2


if __name__ == "__main__":
    sys.exit(main())
