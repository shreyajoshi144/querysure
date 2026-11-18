#!/usr/bin/env bash
# Live model-quality run (Sprint 13). SPENDS MONEY on your Groq account; refuses without a key and asks before starting.
#   GROQ_API_KEY=... scripts/live_quality.sh [SQL_CASES=62] [ANSWER_QUESTIONS=40]
# Writes raw model replies under evaluation/results/live13/ (replay them for free with `--mode replay`) and a summary to
# docs/baseline/live_quality.txt. Providers other than Groq are disabled for the run so the numbers name one model.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${GROQ_API_KEY:?set GROQ_API_KEY in the environment (never pass it on a command line that is logged)}"
export LLM_PROVIDER_ORDER=groq OPENAI_API_KEY= ANTHROPIC_API_KEY= GEMINI_API_KEY= XAI_API_KEY=
N_SQL="${1:-62}"; N_ANS="${2:-40}"; OUT=evaluation/results/live13; mkdir -p "$OUT"
echo "About to call Groq for up to: $N_SQL SQL questions + 14 agent goals + $N_ANS cited answers (about $((N_SQL * 2 + 14 + N_ANS)) model calls with repairs)."
read -r -p "Proceed and spend money? [y/N] " yn; [ "$yn" = y ] || { echo aborted; exit 1; }
{
  echo "Live quality -- $(date -u +%Y-%m-%dT%H:%MZ) -- provider: Groq only (LLM_PROVIDER_ORDER=groq)"
  echo; echo "== Text-to-SQL, retail benchmark (execution accuracy, hallucination, validity) =="
  python -m evaluation live --yes --limit "$N_SQL" --delay-seconds 2 --record-responses "$OUT/sql_responses.json" --name live13_sql || true
  echo; echo "== Agent plans =="
  python -m evaluation.platform --suite agent --mode live --yes --save-responses "$OUT/agent_responses.json" --out "$OUT/agent.json" | python -c "import json,sys; d=json.load(sys.stdin)['agent_plans']; d.pop('rows',None); print(json.dumps(d,indent=1))"
  echo; echo "== Cited answers over documents =="
  python -m evaluation.platform --suite answers --mode live --yes --limit "$N_ANS" --save-responses "$OUT/answer_responses.json" --out "$OUT/answers.json" | python -c "import json,sys; d=json.load(sys.stdin)['answers']; d.pop('rows',None); print(json.dumps(d,indent=1))"
} 2>&1 | tee docs/baseline/live_quality.txt
