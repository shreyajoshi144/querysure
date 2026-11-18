"""Provider transports: one function per provider that sends (system, user) and returns an ``LLMResult``.

Groq, OpenAI and Anthropic are moved unchanged from modules/sql_generator.py (Sprint 2). Gemini and xAI are new and use
each vendor's documented OpenAI-compatible endpoint with JSON-Schema structured output:

  * xAI:    https://api.x.ai/v1 -- ``response_format`` ``json_schema`` is documented (docs.x.ai, structured outputs).
  * Gemini: https://generativelanguage.googleapis.com/v1beta/openai/ -- Google documents structured output through the
    OpenAI SDK's JSON-Schema response format and marks the compatibility layer as beta.

Capabilities are NOT assumed equal: each provider's structured-output mechanism is listed in ``PROVIDERS``. No model id is
hard-coded for Gemini or xAI (config has no default model), and nothing here has been exercised against those two live
APIs without keys; their tests stub the HTTP client.
"""
from __future__ import annotations

import json
from typing import Optional

import ai_guard
from ai.types import LLMResult
from config import AI_PROVIDER_TIMEOUT_SECONDS

# SDK defaults are ~10 minutes; a hung provider must fail over, not pin a worker.
LLM_TIMEOUT_S = AI_PROVIDER_TIMEOUT_SECONDS  # Sprint 4: configurable (AI_PROVIDER_TIMEOUT_SECONDS, default 25s)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
XAI_BASE_URL = "https://api.x.ai/v1"

# How each provider is asked for structured output. Shown to operators; never used to claim parity.
PROVIDERS = {
    "groq": {"label": "Groq", "structured_output": "json_object", "config_key": "GROQ_API_KEY"},
    "openai": {"label": "OpenAI", "structured_output": "json_object", "config_key": "OPENAI_API_KEY"},
    "anthropic": {"label": "Anthropic", "structured_output": "forced_tool_call", "config_key": "ANTHROPIC_API_KEY"},
    "gemini": {"label": "Google Gemini", "structured_output": "json_schema (OpenAI-compatible, beta)", "config_key": "GEMINI_API_KEY"},
    "xai": {"label": "xAI Grok", "structured_output": "json_schema (OpenAI-compatible)", "config_key": "XAI_API_KEY"},
}


def call_groq(model: str, api_key: str, system_prompt: str, user_prompt: str) -> LLMResult:
    from groq import Groq
    import config as _cfg
    client = Groq(api_key=api_key, timeout=ai_guard.call_timeout(LLM_TIMEOUT_S), max_retries=1)
    extra = {}
    if _cfg.GROQ_REASONING_EFFORT and model.startswith("openai/gpt-oss"):
        extra["extra_body"] = {"reasoning_effort": _cfg.GROQ_REASONING_EFFORT}
    resp = client.chat.completions.create(
        model=model,
        max_tokens=_cfg.LLM_MAX_OUTPUT_TOKENS,
        **extra,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    u = getattr(resp, "usage", None)
    return LLMResult(resp.choices[0].message.content or "",
                     getattr(u, "prompt_tokens", None), getattr(u, "completion_tokens", None))


def call_openai(model: str, api_key: str, system_prompt: str, user_prompt: str) -> LLMResult:
    from openai import OpenAI
    import config as _cfg
    client = OpenAI(api_key=api_key, timeout=ai_guard.call_timeout(LLM_TIMEOUT_S), max_retries=1)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=min(_cfg.LLM_MAX_OUTPUT_TOKENS, 1500),
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    u = getattr(resp, "usage", None)
    return LLMResult(resp.choices[0].message.content or "",
                     getattr(u, "prompt_tokens", None), getattr(u, "completion_tokens", None))


def call_anthropic(model: str, api_key: str, system_prompt: str, user_prompt: str, *, json_schema: dict,
                   tool_name: str, tool_description: str = "Return the result.") -> LLMResult:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key, timeout=ai_guard.call_timeout(LLM_TIMEOUT_S), max_retries=1)
    # Forced tool call = Anthropic's structured-output mechanism; the tool input IS the JSON object.
    import config as _cfg
    resp = client.messages.create(
        model=model,
        max_tokens=min(_cfg.LLM_MAX_OUTPUT_TOKENS, 1500),
        system=system_prompt,
        tools=[{"name": tool_name, "description": tool_description, "input_schema": json_schema}],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": user_prompt}],
    )
    parts = [json.dumps(b.input) for b in resp.content if getattr(b, "type", None) == "tool_use"]
    parts = parts or [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    u = getattr(resp, "usage", None)
    return LLMResult("".join(parts), getattr(u, "input_tokens", None), getattr(u, "output_tokens", None))


def call_openai_compatible(base_url: str, model: str, api_key: str, system_prompt: str, user_prompt: str, *,
                           json_schema: Optional[dict] = None, schema_name: str = "result") -> LLMResult:
    """Chat completion against an OpenAI-compatible endpoint. With ``json_schema`` the reply is constrained by a strict
    JSON Schema (the schema must list every property as required and forbid extra ones); without it, JSON mode."""
    from openai import OpenAI
    import config as _cfg
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=ai_guard.call_timeout(LLM_TIMEOUT_S), max_retries=1)
    response_format = ({"type": "json_schema", "json_schema": {"name": schema_name, "schema": json_schema, "strict": True}}
                       if json_schema else {"type": "json_object"})
    resp = client.chat.completions.create(
        model=model,
        max_tokens=min(_cfg.LLM_MAX_OUTPUT_TOKENS, 1500),
        response_format=response_format,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    u = getattr(resp, "usage", None)
    return LLMResult(resp.choices[0].message.content or "",
                     getattr(u, "prompt_tokens", None), getattr(u, "completion_tokens", None))


def call_gemini(model: str, api_key: str, system_prompt: str, user_prompt: str, *, json_schema: Optional[dict] = None,
                schema_name: str = "result") -> LLMResult:
    return call_openai_compatible(GEMINI_BASE_URL, model, api_key, system_prompt, user_prompt,
                                  json_schema=json_schema, schema_name=schema_name)


def call_xai(model: str, api_key: str, system_prompt: str, user_prompt: str, *, json_schema: Optional[dict] = None,
             schema_name: str = "result") -> LLMResult:
    return call_openai_compatible(XAI_BASE_URL, model, api_key, system_prompt, user_prompt,
                                  json_schema=json_schema, schema_name=schema_name)
