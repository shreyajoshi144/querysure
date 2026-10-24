"""PII / secret scrubbing for logs, error reports and ops events (Sprint 4).

Design rule: observability output must never carry user content or credentials. Two layers:

1. *Structural*: log call sites and ops events only carry ids/counters. Free text from users (questions, SQL,
   dataset names, e-mail addresses, report content, LLM prompts) is never passed to a logger on purpose.
2. *Defensive* (this module): everything that does reach a log handler is scrubbed anyway -- provider SDK errors,
   driver errors (``DETAIL: Key (email)=(a@b.c) already exists``), uvicorn access lines with ``?token=`` query strings,
   exception messages. Scrubbing is pattern-based and therefore best effort; it is a safety net, not the control.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"
MAX_MESSAGE_CHARS = 2000

_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # JWTs (three base64url segments) and bearer credentials
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), "[jwt]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    # vendor key shapes
    (re.compile(r"\b(?:sk|rk|pk)-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}"), "[api-key]"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{16,}"), "[api-key]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[aws-key]"),
    # credentials in URLs: scheme://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"), r"\1[redacted]@"),
    # secret-bearing query parameters / key=value pairs
    (re.compile(
        r"(?i)([?&;\s,{'\"]|^)((?:access_|refresh_|id_)?token|api[_-]?key|apikey|secret|password|passwd|pwd|"
        r"authorization|signature|x-amz-signature|x-amz-credential|x-amz-security-token|set-cookie|cookie)"
        r"(['\"]?\s*[=:]\s*)('[^']*'|\"[^\"]*\"|[^\s&;,'\"}]+)"),
     lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}"),
    # ambiguous words (code, key, session, state) are only secrets inside a URL query string
    (re.compile(r"(?i)([?&])(code|key|session|state|sig)=([^\s&]+)"), lambda m: f"{m.group(1)}{m.group(2)}={REDACTED}"),
    # e-mail addresses
    (re.compile(r"\b[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b"), "[email]"),
    # long opaque tokens (hex / base64url / url-safe secrets)
    (re.compile(r"\b[A-Za-z0-9_-]{40,}\b"), "[token]"),
)

# Structured fields whose VALUE is never emitted, whatever it contains.
DENY_KEYS = frozenset({
    "password", "passwd", "token", "access_token", "refresh_token", "authorization", "api_key", "apikey", "secret",
    "cookie", "set-cookie", "email", "prompt", "system_prompt", "question", "sql", "query", "result", "results",
    "rows", "context", "retrieved_context_text", "body", "data", "payload", "report", "dataset", "filename", "fname",
    "raw_response", "insights", "explanation", "schema", "semantic", "key", "object_ref", "db_path",
})


def scrub_text(value: Any, max_chars: int = MAX_MESSAGE_CHARS) -> str:
    """Return ``value`` as text with secrets / e-mail addresses / opaque tokens removed and length capped."""
    text = value if isinstance(value, str) else str(value)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    if len(text) > max_chars:
        text = text[:max_chars] + "...[truncated]"
    return text


def scrub_value(value: Any, *, depth: int = 0) -> Any:
    """Recursively scrub a structured log field. Deny-listed keys are replaced wholesale."""
    if depth > 4:
        return REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return scrub_text(value, 500)
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:50]:
            name = str(k)
            out[name] = REDACTED if name.lower() in DENY_KEYS else scrub_value(v, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [scrub_value(v, depth=depth + 1) for v in list(value)[:50]]
    return scrub_text(repr(value), 200)
