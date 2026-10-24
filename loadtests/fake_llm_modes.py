"""Failure-injecting OpenAI/Groq-compatible stub for Sprint 4 closure verification (real HTTP, real SDK clients).
Control:  POST /_mode  {"groq": "ok", "groq:llama-3.1-8b-instant": "500", "openai": "429"}   (model-specific key wins)
          POST /_reset  (clears modes + counters)      GET /_stats  (calls per provider:model)
Modes: ok | timeout (sleeps 6s) | 429 | 500 | malformed (200, not JSON) | badsql_then_garbage (1st call: valid JSON, invalid SQL;
       every later call: malformed)  | hang_close (closes the connection without replying)
Every error body embeds CANARY strings (an API-key look-alike and an e-mail) so tests can prove provider text never reaches
clients, logs or ops views."""
import asyncio, json, sys
from aiohttp import web

CANARY_KEY = "sk-CANARYPROVIDERKEY0123456789abcdef"
CANARY_MAIL = "canary.victim@example.com"
GOOD = {"sql": "SELECT s.region, SUM(s.amount) AS total FROM sales AS s GROUP BY s.region", "explanation": "x", "confidence": "High", "complexity": "Simple"}
BADSQL = {"sql": "SELECT n.nope FROM nonexistent_table AS n", "explanation": "x", "confidence": "High", "complexity": "Simple"}
state = {"modes": {}, "calls": {}, "seq": {}}


def completion(content):
    return {"id": "x", "object": "chat.completion", "created": 0, "model": "stub",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 70, "total_tokens": 970}}


async def chat(request: web.Request):
    body = json.loads((await request.read()) or b"{}")
    provider = "groq" if "/openai/v1/" in request.path else "openai"
    model = body.get("model", "?")
    key = f"{provider}:{model}"
    state["calls"][key] = state["calls"].get(key, 0) + 1
    mode = state["modes"].get(key) or state["modes"].get(provider, "ok")
    if mode == "ok":
        return web.json_response(completion(json.dumps(GOOD)))
    if mode == "timeout":
        await asyncio.sleep(6)
        return web.json_response(completion(json.dumps(GOOD)))
    if mode == "429":
        return web.json_response({"error": {"message": f"rate limit for {CANARY_KEY} user {CANARY_MAIL}", "type": "rate_limit"}}, status=429, headers={"Retry-After": "1"})
    if mode == "500":
        return web.json_response({"error": {"message": f"internal error key={CANARY_KEY} user={CANARY_MAIL}"}}, status=500)
    if mode == "malformed":
        return web.json_response(completion(f"Sure! Here is your answer for {CANARY_MAIL}: {{not valid json"))
    if mode == "badsql_then_garbage":
        n = state["seq"][key] = state["seq"].get(key, 0) + 1
        return web.json_response(completion(json.dumps(BADSQL) if n == 1 else "garbage {"))
    if mode == "hang_close":
        request.transport.close()
        return web.Response(status=500)
    return web.json_response({"error": "unknown mode"}, status=500)


async def control(request: web.Request):
    if request.path == "/_reset":
        state.update(modes={}, calls={}, seq={})
    elif request.path == "/_mode":
        state["modes"].update(await request.json()); state["seq"] = {}
    return web.json_response({"modes": state["modes"], "calls": state["calls"]})


app = web.Application()
app.router.add_post("/_mode", control); app.router.add_post("/_reset", control); app.router.add_get("/_stats", control)
app.router.add_post("/{tail:.*}", chat)
if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 5557, print=None, access_log=None)
