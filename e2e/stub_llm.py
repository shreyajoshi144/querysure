"""Stub OpenAI/Groq-compatible chat endpoint for the browser E2E: always answers with one valid query against the bundled
sample (retail) database. Usage: python e2e/stub_llm.py <port>"""
import json
import sys

from aiohttp import web

ANSWER = {
    "sql": "SELECT s.region, COUNT(o.order_id) AS orders FROM orders AS o JOIN stores AS s ON s.store_id = o.store_id "
           "GROUP BY s.region ORDER BY orders DESC",
    "explanation": "Orders per store region.", "confidence": "High", "complexity": "Simple",
}


async def completions(request: web.Request):
    await request.read()
    return web.json_response({
        "id": "x", "object": "chat.completion", "created": 0, "model": "stub",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(ANSWER)}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 70, "total_tokens": 970},
    })


app = web.Application()
app.router.add_post("/{tail:.*}", completions)
if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=int(sys.argv[1]), print=None, access_log=None)
