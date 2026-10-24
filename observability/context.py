"""Request / user / session correlation for logs and metrics (Sprint 4).

A mutable ``RequestContext`` lives in a ``ContextVar``. The HTTP middleware creates it, FastAPI dependencies (running in a
worker thread, whose context is a *copy*) mutate the same object, so the middleware sees user/session refs after the route
ran. Ids are never logged raw: ``ref()`` is a truncated HMAC, stable for correlation and not reversible.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

import config

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def ref(value: Optional[str]) -> Optional[str]:
    """Short pseudonymous reference for an id (user / session). ``None`` stays ``None``."""
    if not value:
        return None
    digest = hmac.new(config.LOG_HASH_KEY.encode("utf-8"), str(value).encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:12]


@dataclass
class RequestContext:
    request_id: Optional[str] = None
    user_ref: Optional[str] = None
    session_ref: Optional[str] = None
    job_id: Optional[str] = None
    route: Optional[str] = None


_ctx: ContextVar[Optional[RequestContext]] = ContextVar("qs_request_context", default=None)


def new_context(request_id: Optional[str] = None, **fields) -> RequestContext:
    ctx = RequestContext(request_id=request_id, **fields)
    _ctx.set(ctx)
    return ctx


def current() -> Optional[RequestContext]:
    return _ctx.get()


def clear() -> None:
    _ctx.set(None)


def valid_inbound_request_id(value: Optional[str]) -> Optional[str]:
    """Accept a caller-supplied correlation id only if it is short and made of safe characters."""
    if value and _REQUEST_ID_RE.match(value):
        return value
    return None


def bind_user(user_id: Optional[str]) -> None:
    ctx = _ctx.get()
    if ctx is not None and user_id:
        ctx.user_ref = ref(user_id)


def bind_session(session_id: Optional[str]) -> None:
    ctx = _ctx.get()
    if ctx is not None and session_id:
        ctx.session_ref = ref(session_id)


def bind_job(job_id: Optional[str]) -> None:
    ctx = _ctx.get()
    if ctx is not None and job_id:
        ctx.job_id = job_id
