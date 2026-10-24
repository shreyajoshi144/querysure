"""Minimal OpenAI/Groq-compatible chat-completions stub for load tests: fixed schema-valid answer after a simulated provider
latency. Point the app at it with GROQ_BASE_URL=http://127.0.0.1:5556 (the groq SDK reads that variable).
    python loadtests/fake_llm.py [port] [mean_latency_ms]
Env FAKE_LLM_FAIL_RATE (0..1) makes a fraction of calls return HTTP 500 (to exercise fallback / circuit breaker under load)."""
import asyncio
import json
import os
import random
import sys

from aiohttp import web

MEAN_MS = float(sys.argv[2]) if len(sys.argv) > 2 else 400.0
FAIL_RATE = float(os.environ.get("FAKE_LLM_FAIL_RATE", "0"))
ANSWER = {"sql": "SELECT s.region, SUM(s.amount) AS total FROM sales AS s GROUP BY s.region ORDER BY total DESC",
          "explanation": "Total amount by region.", "confidence": "High", "complexity": "Simple"}
calls = {"n": 0, "failed": 0}


async def completions(request: web.Request):
    calls["n"] += 1
    await request.read()
    await asyncio.sleep(max(0.02, random.gauss(MEAN_MS, MEAN_MS * 0.25)) / 1000.0)
    if FAIL_RATE and random.random() < FAIL_RATE:
        calls["failed"] += 1
        return web.json_response({"error": {"message": "simulated provider failure"}}, status=500)
    return web.json_response({"id": "x", "object": "chat.completion", "created": 0, "model": "stub",
                              "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(ANSWER)}}],
                              "usage": {"prompt_tokens": 900, "completion_tokens": 70, "total_tokens": 970}})


async def stats(_):
    return web.json_response(calls)


app = web.Application()
app.router.add_post("/{tail:.*}", completions)
app.router.add_get("/_stats", stats)
if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 5556, print=None, access_log=None)
