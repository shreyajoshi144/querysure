"""
Runs the AI analytics pipeline:
Connect -> Schema -> Semantic -> Retrieval -> SQL -> Validation -> Execution -> Reliability -> Insights -> Report

Every /api/* route below (except /api/auth/*) requires a valid bearer token
and operates only on pipeline sessions owned by that user, persisted in the
database rather than an in-memory dict.
"""

# Sprint 1 blocker fix: this file used to start with
# `from __future__ import annotations`, which turns every type annotation
# into a plain string, resolved lazily. That's normally harmless, but it
# breaks FastAPI request-body detection on any route wrapped by slowapi's
# @limiter.limit(...): that decorator returns a new wrapper function whose
# __globals__ is slowapi's own module namespace, not this file's -- so when
# FastAPI tries to resolve a stringified annotation like 'UserCreate' to
# build the route's dependant, it looks it up in slowapi's globals, doesn't
# find it, and silently falls back to treating the parameter as a query
# param instead of a request body. In practice this made
# POST /api/auth/register -- the only rate-limited route with a
# Pydantic-typed body parameter -- return 422 "payload: Field required" on
# every valid request; registration was completely broken. All type hints
# in this file use syntax (`str | None`, `dict`, `list`) that's natively
# supported at runtime on Python 3.10+ (the Dockerfile pins 3.12), so the
# future-annotations import was never load-bearing here -- removing it is
# a pure bugfix with no behavior change to anything else in the file.
import json
import logging
import os
import re
import sys
import asyncio
import time
import uuid
from datetime import datetime, timezone, timedelta

import uvicorn
from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, File, Form, Request, UploadFile
from pydantic import BaseModel, model_validator
from fastapi.exceptions import RequestValidationError
from starlette.concurrency import run_in_threadpool
import anyio
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.exc import IntegrityError, SQLAlchemyError, TimeoutError as SATimeoutError
# Names that api.py itself no longer uses but that tests (and operators' monkeypatches) address as ``api.<name>``.
# Removing them breaks ``patch("api.get_email_provider")`` and friends, so they are kept deliberately.
from auth import LoginRequest, create_access_token  # noqa: E402,F401
from config import ENVIRONMENT, TRUSTED_PROXY_IPS, WEB_CONCURRENCY  # noqa: E402,F401
from db import UsageDaily, create_session, session_to_dict  # noqa: E402,F401
from email_provider import get_email_provider  # noqa: E402,F401
from entitlements import PLAN_CONFIG  # noqa: E402,F401
from fastapi.responses import FileResponse  # noqa: E402,F401
from modules import generate_report  # noqa: E402,F401
from modules.ingestion import ingest_to_sqlite  # noqa: E402,F401
from modules.schema_engine import SchemaTooLarge  # noqa: E402,F401
from sqlalchemy import text  # noqa: E402,F401
import sqlite3  # noqa: E402,F401
from billing_service import (
    CheckoutError, PURCHASABLE_PLANS, create_checkout, get_billing_state, payload_digest,
    process_verified_event,
)
import account_service
import payment_provider
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
import jwt as jose_jwt  # PyJWT; alias kept so call sites are unchanged
from jwt import PyJWTError as JWTError

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, ".env"))

# Sprint 1 blocker fix: no logging was configured anywhere in the app --
# `import logging` only happened lazily inside two functions, with no
# handler/format/level set, so output relied entirely on Python's default
# "last resort" stderr handler (WARNING+ only, unstructured). This gives
# every `logging.getLogger("querysure")` call a consistent, greppable
# format that includes the level and logger name, and makes INFO-level
# (e.g. the access log below) actually emit instead of being silently
# dropped by the default WARNING root level.
# Sprint 4: structured JSON logs (LOG_FORMAT=text restores the old one-line format), every line PII-scrubbed and tagged with the
# request / pseudonymous user / session correlation ids. See observability/logging_setup.py.
from observability.logging_setup import configure_logging  # noqa: E402

configure_logging()
logger = logging.getLogger("querysure")

from config import (
    IS_PRODUCTION,
    PORT as CONFIG_PORT,
    REDIS_URL,
    CORS_ALLOWED_ORIGINS,
    CORS_ALLOW_CREDENTIALS,
    GROQ_API_KEY,
    OPENAI_API_KEY,
    ANTHROPIC_API_KEY,
    DAILY_LLM_BUDGET_USD,
    DAILY_LLM_BUDGET_CENTS,
    GLOBAL_DAILY_AI_BUDGET_CENTS,
    plan_budget_multiplier,
    GOOGLE_CLIENT_ID,
    PRODUCT_CONTACT_EMAIL,
    CSP_REPORT_ONLY,
    THREADPOOL_MAX_WORKERS,
    CONCURRENCY_GENERATE_SQL, CONCURRENCY_UPLOAD, CONCURRENCY_EXECUTE_SQL,
)

from modules import (
    load_database,
    build_semantic_layer,
    build_vector_store,
    retrieve_context,
    clear_cache_key,
    generate_sql,
    validate_sql,
    execute_query,
    assess_reliability,
    generate_insights,
)
from modules.trust_trace import build_trust_trace
from modules.ingestion import sanitize_filename
from modules.query_result_engine import PREVIEW_MAX_PAGE_SIZE, PREVIEW_MAX_ROWS, preview_table

from db import (
    User,
    PipelineSession,
    get_db,
    init_db,
    get_or_create_session,
    update_session,
    SessionNotFoundError,
    SessionConflictError,
    DatabaseBusyError,
    SESSION_FIELDS,
    get_or_create_daily_usage,
    record_llm_usage,
    reserve_daily_budget,
    settle_daily_budget,
    global_spend_cents_db,
    record_ai_invocation,
    AIInvocation,
    Report,
    reserve_query_slot,
    refund_query_slot,
    Job,
)
import storage
import config as _config_module
import quotas
from auth import (
    JWT_SECRET_KEY,
    JWT_ALGORITHM,
    Token,
    UserCreate,
    UserOut,
    EmailRequest,
    PasswordRequest,
    authenticate_user,
    decode_token_claims,
    get_current_user,
    issue_session_token,
    oauth2_scheme,
    revoke_all_sessions,
    revoke_session,
    hash_password,
    normalize_email,
    new_token,
    hash_token,
    verify_google_id_token,
    verify_password,
    is_google_only_account,
    make_google_only_password,
    GoogleUnavailableError,
)
from db import EmailVerificationToken, PasswordResetToken
from entitlements import DEFAULT_PLAN, daily_limit, get_plan
from jobs import enqueue_job
from observability import context as obs_context, errors as obs_errors, events as obs_events, gauges as obs_gauges
from observability import health as obs_health, metrics as obs_metrics
from observability import alerts as obs_alerts

from schemas import (
    LoginBody, FlushSessionRequest, SampleRequest, SchemaIntelligenceRequest,
    SemanticLayerRequest, RetrieveTablesRequest, GenerateSqlRequest,
    ValidateSqlRequest, ExecuteSqlRequest, ReliabilityRequest, InsightsRequest,
    ReportRequest, ChangePasswordRequest,
)
from email_provider import verification_url, password_reset_url
app = FastAPI(title="QuerySure", version="2.0.0", docs_url=None if IS_PRODUCTION else "/docs", redoc_url=None if IS_PRODUCTION else "/redoc", openapi_url=None if IS_PRODUCTION else "/openapi.json")


class GoogleSignInRequest(BaseModel):
    """Body for POST /api/auth/google and /api/auth/google/link.

    Google Identity Services hands the browser a ``credential``; some clients name the same value ``id_token`` /
    ``idToken`` / ``token``. All are accepted. A missing or blank value is answered by the route (422
    GOOGLE_CREDENTIAL_REQUIRED, as before) instead of a generic pydantic error.
    """
    credential: str = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_alternate_names(cls, data):
        if not isinstance(data, dict):
            return {"credential": ""}
        for name in ("credential", "id_token", "idToken", "token"):
            candidate = data.get(name)
            if isinstance(candidate, str) and candidate.strip():
                return {"credential": candidate.strip()}
        return {"credential": ""}


_GOOGLE_CREDENTIAL_MAX_CHARS = 8192


def _verified_google_claims(credential: str, *, action: str):
    """Verify a Google credential. Returns ((sub, email), None) or (None, error_response) with a safe 4xx/503."""
    credential = (credential or "").strip()
    if not GOOGLE_CLIENT_ID:
        return None, err_response("Google sign-in is not configured for this deployment.", code=503, error_code="GOOGLE_NOT_CONFIGURED")
    if not credential:
        return None, err_response("Google sign-in credential is required.", code=422, error_code="GOOGLE_CREDENTIAL_REQUIRED")
    if len(credential) > _GOOGLE_CREDENTIAL_MAX_CHARS:
        return None, err_response("Google sign-in could not be verified.", code=401, error_code="INVALID_GOOGLE_TOKEN")
    try:
        claims = verify_google_id_token(credential)
    except GoogleUnavailableError:
        logger.warning("Google signing keys unavailable during %s", action)
        return None, err_response("Google sign-in is temporarily unavailable. Please try again shortly.", code=503, error_code="GOOGLE_UNAVAILABLE")
    except Exception:
        logger.warning("Google ID token verification failed during %s", action)
        return None, err_response("Google sign-in could not be verified.", code=401, error_code="INVALID_GOOGLE_TOKEN")
    subject = str(claims.get("sub") or "").strip()
    email = normalize_email(str(claims.get("email", "")))
    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com") or not subject or not email or claims.get("email_verified") is not True:
        return None, err_response("Google sign-in could not be verified.", code=401, error_code="INVALID_GOOGLE_IDENTITY")
    return (subject, email), None


@app.get("/api/auth/google/config")
def google_auth_config():
    # Client IDs are public OAuth identifiers; client secrets are never sent
    # to the browser. An absent ID means the deployment has not configured GIS.
    return {"enabled": bool(GOOGLE_CLIENT_ID), "client_id": GOOGLE_CLIENT_ID or None}


# ---------------------------------------------------------------------------
# Public product evaluation summary. This is presentation-only: it reads the
# immutable benchmark metadata and any explicitly generated evaluation result
# artifacts. It never runs evaluation and never invents missing metrics.
# ---------------------------------------------------------------------------
@app.get("/api/evaluation/product-summary")
def evaluation_product_summary():
    benchmark_path = os.path.join(ROOT, "evaluation", "datasets", "retail_benchmark.json")
    try:
        with open(benchmark_path, "r", encoding="utf-8") as fh:
            benchmark = json.load(fh)
        cases = benchmark.get("cases", [])
        def _label(value) -> str:
            return str(value).replace("_", " ").title()

        def _count(key: str) -> dict:
            counts: dict = {}
            for c in cases:
                if c.get(key):
                    counts[_label(c[key])] = counts.get(_label(c[key]), 0) + 1
            return dict(sorted(counts.items()))

        category_counts = _count("category")
        categories = sorted(category_counts)
        difficulty_counts = _count("difficulty")
        behavior_counts = _count("expected_behavior")
        total = len(cases)
    except (OSError, ValueError, TypeError):
        return JSONResponse(status_code=200, content={
            "success": True, "benchmark": {"available": False},
            "metrics": {}, "deepeval": {"status": "not_available"},
            "synthetic": {"status": "not_available"},
        })

    result_dir = os.path.join(ROOT, "evaluation", "results")
    real_reports = []
    deepeval_reports = []
    synthetic_reports = []
    if os.path.isdir(result_dir):
        for name in os.listdir(result_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(result_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and "querysure" in payload and "deepeval" in payload:
                deepeval_reports.append(payload)
                continue
            meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
            mode = meta.get("mode")
            source = meta.get("source", {})
            # Only live reports represent actual model evaluation. Oracle and
            # synthetic demo artifacts are methodology examples, not product
            # quality measurements.
            if mode == "live" and source.get("calls_llm") is True:
                real_reports.append(payload)
            elif mode == "synthetic" and source.get("calls_llm") is True:
                synthetic_reports.append(payload)

    latest = sorted(real_reports, key=lambda r: r.get("meta", {}).get("generated_at", ""))[-1:]
    metrics = {}
    evaluated_cases = 0
    if latest:
        m = latest[0].get("metrics", {})
        a = m.get("answerable", {})
        h = m.get("hallucination", {})
        rj = m.get("rejection", {})
        metrics = {
            "result_accuracy": a.get("execution_accuracy"),
            "sql_execution_success": a.get("execution_success_rate"),
            "schema_grounding": a.get("column_grounding_rate"),
            "hallucinated_identifiers": h.get("identifier_level_rate"),
            "correct_rejection": rj.get("strict_accuracy"),
        }
        evaluated_cases = latest[0].get("meta", {}).get("dataset", {}).get("cases_run", 0)

    deepeval = {"status": "not_run"}
    if deepeval_reports:
        latest_de = sorted(deepeval_reports, key=lambda r: r.get("deepeval", {}).get("generated_at", ""))[-1]["deepeval"]
        deepeval = {
            "status": "available",
            "cases": latest_de.get("cases", 0),
            "metrics": latest_de.get("metrics", {}),
        }

    synthetic = {"status": "not_run"}
    if synthetic_reports:
        latest_syn = sorted(synthetic_reports, key=lambda r: r.get("meta", {}).get("generated_at", ""))[-1]
        synthetic = {
            "status": "available",
            "cases": latest_syn.get("meta", {}).get("dataset", {}).get("cases_run", 0),
        }

    return {
        "success": True,
        "benchmark": {
            "available": True, "cases": total, "coverage": total,
            "version": benchmark.get("version"), "categories": categories,
            "category_counts": category_counts, "difficulty_counts": difficulty_counts,
            "behavior_counts": behavior_counts,
        },
        "metrics": metrics, "evaluated_cases": evaluated_cases,
        "deepeval": deepeval, "synthetic": synthetic,
    }


# ---------------------------------------------------------------------------
# Request IDs — every response gets a short opaque id in X-Request-ID so a
# user-facing error can show "Reference: 8f31c2" and that string can be
# grepped straight out of server logs, instead of "something went wrong"
# being a dead end. Implemented as middleware (not threaded through every
# err_response call) so it's attached uniformly to every response, success
# or failure, with no per-route changes required.
# ---------------------------------------------------------------------------
@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):
    request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    if IS_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    csp = (
        "default-src 'self'; "
        "base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        "script-src 'self' https://accounts.google.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://accounts.google.com/gsi/style; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https://*.googleusercontent.com; "
        "connect-src 'self' https://accounts.google.com/gsi/; "
        "frame-src https://accounts.google.com/gsi/"
    )
    # A route may set a stricter CSP of its own (e.g. report downloads use `sandbox`); never overwrite it.
    if "content-security-policy" not in response.headers:
        response.headers["Content-Security-Policy-Report-Only" if CSP_REPORT_ONLY else "Content-Security-Policy"] = csp
    # Access logging + metrics live in _observability_middleware (registered last => outermost, so it also sees
    # CORS preflights, rate-limit and concurrency rejections that never reach this middleware).
    return response

# BUGFIX: this used to call StaticFiles(directory=...) unconditionally.
# StaticFiles raises RuntimeError at import time if the directory doesn't
# exist -- since index.html references /assets/bg.jpeg, an `assets/` folder
# must exist next to api.py, but if it's missing (e.g. .gitignored, or not
# copied during deploy) the ENTIRE APP FAILS TO START, not just that one
# image. Guard it exactly like STATIC_DIR below so a missing assets folder
# degrades to "no background image" instead of "server won't boot."
# Per-user concurrency guard. Redis is shared across workers when available;
# development falls back to a bounded in-process counter. The middleware is
# intentionally path-based so no route signature/response contract changes.
_INFLIGHT_PATHS = {
    "/api/generate-sql": ("generate-sql", CONCURRENCY_GENERATE_SQL),
    "/api/connect/upload": ("upload", CONCURRENCY_UPLOAD),
    "/api/execute-sql": ("execute-sql", CONCURRENCY_EXECUTE_SQL),
}
_inflight_local: dict[str, dict[str, float]] = {}
_inflight_lock = __import__("threading").Lock()
_async_redis = None
try:
    if REDIS_URL:
        import redis.asyncio as _redis_async
        _async_redis = _redis_async.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
except Exception:
    _async_redis = None

# Lease = one unique token per in-flight request, stored in a per-(user, endpoint) sorted set whose score is the
# lease's expiry time (Redis server clock). Acquire prunes expired leases, then admits only if fewer than
# `limit` live leases remain. Release removes ONLY the caller's own token. Consequences:
#   * a lost/late release can never decrement someone else's slot (the old INCR/DECR counter could);
#   * a lease whose release was lost (crash, dropped reply) expires on its own after _INFLIGHT_TTL and is
#     pruned on the next acquire -- stale slots are bounded, never permanent;
#   * releasing twice, or after expiry, is a harmless no-op.
_INFLIGHT_ACQUIRE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
local ttl_ms = tonumber(ARGV[2]) * 1000
redis.call('ZADD', KEYS[1], now + ttl_ms, ARGV[3])
redis.call('PEXPIRE', KEYS[1], ttl_ms)
return 1
"""
_INFLIGHT_RELEASE_LUA = """
return redis.call('ZREM', KEYS[1], ARGV[1])
"""
_INFLIGHT_TTL = 300


class _Lease(str):
    """Backend name ("redis" / "local") that also carries this request's unique lease token. It compares equal
    to the plain backend string, so callers that only look at the backend keep working."""
    token: str = ""

    def __new__(cls, backend: str, token: str):
        obj = super().__new__(cls, backend)
        obj.token = token
        return obj


async def _inflight_acquire(key: str, limit: int):
    global _async_redis
    token = uuid.uuid4().hex
    if _async_redis is not None:
        try:
            ok = await _async_redis.eval(_INFLIGHT_ACQUIRE_LUA, 1, key, limit, _INFLIGHT_TTL, token)
            return bool(ok), _Lease("redis", token)
        except Exception:
            obs_metrics.record_redis_fallback("inflight")
            # The reply may have been lost AFTER Redis admitted us. Best-effort: remove our token so the
            # slot is not stranded until expiry. If this also fails, expiry bounds the stale slot.
            try:
                await _async_redis.eval(_INFLIGHT_RELEASE_LUA, 1, key, token)
            except Exception:
                pass
    now = time.monotonic()
    with _inflight_lock:
        leases = _inflight_local.setdefault(key, {})
        for tok in [t for t, exp in leases.items() if exp <= now]:
            leases.pop(tok, None)
        if len(leases) >= limit:
            if not leases:
                _inflight_local.pop(key, None)
            return False, _Lease("local", token)
        leases[token] = now + _INFLIGHT_TTL
        return True, _Lease("local", token)


async def _inflight_release(key: str, backend) -> None:
    global _async_redis
    token = getattr(backend, "token", "")
    if not token:
        return  # no ownership proof -> never touch a slot we cannot prove is ours (TTL reclaims it)
    if str(backend) == "redis" and _async_redis is not None:
        for _attempt in range(2):  # one retry: a dropped reply must not strand the slot for a full TTL
            try:
                await _async_redis.eval(_INFLIGHT_RELEASE_LUA, 1, key, token)
                return
            except Exception:
                continue
        return  # bounded by expiry
    with _inflight_lock:
        leases = _inflight_local.get(key)
        if leases is not None:
            leases.pop(token, None)
            if not leases:
                _inflight_local.pop(key, None)


@app.middleware("http")
async def _user_concurrency_middleware(request: Request, call_next):
    target = _INFLIGHT_PATHS.get(request.url.path)
    if not target:
        return await call_next(request)
    auth_header = request.headers.get("authorization", "")
    user_id = None
    if auth_header.lower().startswith("bearer "):
        try:
            payload = jose_jwt.decode(auth_header[7:], JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
            user_id = payload.get("sub")
        except JWTError:
            pass
    if not user_id:
        return await call_next(request)
    name, limit = target
    key = f"querysure:inflight:{user_id}:{name}"
    lease = await _inflight_acquire(key, limit)
    if isinstance(lease, tuple):
        acquired, backend = lease
    else:  # backwards-compatible with test/local monkeypatches returning bool
        acquired, backend = bool(lease), "local"
    if not acquired:
        obs_metrics.record_rate_limited("concurrency:" + name)
        return JSONResponse(
            status_code=429,
            content={"success": False, "error": "Too many concurrent requests. Please wait for the current operation to finish.", "error_code": "CONCURRENCY_LIMIT", "request_id": getattr(request.state, "request_id", None)},
            headers={"Retry-After": "2"},
        )
    try:
        return await call_next(request)
    finally:
        # shield: a client disconnect cancelling this task must not abort the release and strand the slot
        await asyncio.shield(_inflight_release(key, backend))


ASSETS_DIR = os.path.join(ROOT, "assets")
if os.path.isdir(ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
else:
    import logging
    # Optional legacy mount: the frontend now serves images from /static/assets, so a missing top-level assets/
    # folder is normal and should not appear as a warning on every start.
    logging.getLogger("querysure").info(
        "optional assets/ directory not present at %s; /assets/* is not mounted.",
        ASSETS_DIR,
    )


def _rate_limit_key(request: Request) -> str:
    """Rate-limit per authenticated user where possible (so one user can't
    starve another sharing an IP/NAT), falling back to IP address for the
    unauthenticated auth endpoints (login/register), which is exactly where
    brute-force/spam protection matters most.

    IMPROVEMENT: previously this used the raw bearer token string itself as
    the limiter key ("user:" + token). That worked functionally, but it's
    unnecessary handling of a sensitive credential as a cache/dict key, and
    it's not semantically what you want (a re-issued token for the same user
    would count as a different bucket). Decode to the JWT subject (user id)
    instead. Falls back to IP if the token is missing/invalid -- this is only
    a rate-limit key, not an auth check, so a bad token here just means
    "treat this like an anonymous caller for throttling purposes"; the real
    auth check still happens in get_current_user.
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:]
        try:
            payload = jose_jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
            sub = payload.get("sub")
            if sub:
                return f"user:{sub}"
        except JWTError:
            pass
    return "ip:" + get_remote_address(request)


# PRODUCTION UPGRADE: in-memory rate-limit storage only works correctly with
# a single process. The moment you scale to more than one uvicorn worker or
# more than one instance behind a load balancer, each process has its own
# counters, and a "15/minute" limit effectively becomes "15/minute per
# process" -- a real gap, not a hypothetical one, flagged in an earlier audit
# round and left as a documented limitation at the time. Redis fixes this by
# giving every process a shared counter. REDIS_URL is optional: if it's not
# set, storage_uri is None and slowapi/limits falls back to its in-memory
# backend automatically (verified against the actual Limiter signature) --
# so this never breaks a deployment that doesn't have Redis configured, it
# only upgrades one that does. REDIS_URL itself now comes from config.py
# (see the production-without-Redis warning logged there).
# in_memory_fallback_enabled=True: if Redis becomes unreachable AFTER startup
# (not just "never configured" -- an outage mid-flight), slowapi switches
# every @limiter.limit(...) route (register/login/generate-sql) to an
# in-process in-memory limiter using the SAME configured thresholds, instead
# of letting the connection error propagate into a 500 on every request.
# This only reintroduces the pre-Redis "per process" limiting caveat, and
# only for the duration of the outage -- slowapi probes the real backend on
# an exponential backoff and switches back automatically once it recovers
# (verified against the installed slowapi==0.1.9 / limits==5.8.0 source: the
# recovery probe itself never raises even if still down, so this can't
# reintroduce the same failure mode it's fixing).
limiter = Limiter(key_func=_rate_limit_key, storage_uri=REDIS_URL, in_memory_fallback_enabled=True)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


def _retry_after_seconds(exc: RateLimitExceeded) -> int:
    """Exact, not a guess: exc.limit.limit is the underlying `limits` library
    RateLimitItem (confirmed by reading slowapi/limits source directly), and
    .GRANULARITY.seconds is the real configured window -- 60 for "N/minute",
    3600 for "N/hour", etc. A prior version of this function pattern-matched
    the word "second"/"hour"/"day" out of the human-readable exc.detail
    string and guessed a value (e.g. hardcoding 5 for anything containing
    "second") -- fragile, and wrong the moment detail's wording changes
    across a slowapi version. Falls back to 60 only if that library shape
    ever changes."""
    try:
        return exc.limit.limit.GRANULARITY.seconds
    except AttributeError:
        return 60


@app.exception_handler(RateLimitExceeded)
async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    retry_after = _retry_after_seconds(exc)
    request_id = getattr(request.state, "request_id", None)
    obs_metrics.record_rate_limited(_route_label(request))
    return JSONResponse(
        status_code=429,
        content={
            "success": False,
            "error": f"Too many requests ({exc.detail}). Please slow down and try again shortly.",
            "error_code": "RATE_LIMITED",
            "retry_after_seconds": retry_after,
            "request_id": request_id,
        },
        headers={"Retry-After": str(retry_after)},
    )



@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError):
    request_id = getattr(request.state, "request_id", None)
    messages = []
    for error in exc.errors():
        msg = error.get("msg", "Invalid request.")
        if msg not in messages:
            messages.append(msg)
    message = " ".join(messages) or "Invalid request."
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": message,
            "error_code": "INVALID_REQUEST",
            "request_id": request_id,
        },
    )


# Daily per-user budget for LLM calls (/api/generate-sql is the only route
# that spends money). DAILY_LLM_BUDGET_USD / DAILY_LLM_BUDGET_CENTS now come
# from config.py; defaults to a conservative amount so a runaway loop or
# shared demo account can't rack up a surprise bill overnight.

# Rough blended cost estimate (~$0.15 per 1K tokens, ~4 chars/token) used
# ONLY as a fallback when the SQL generator's response doesn't include real
# token-usage figures. If modules/sql_generator.py starts returning a
# `usage` dict (e.g. {"input_tokens":.., "output_tokens":..}) that is used
# instead automatically -- see _estimate_cost_cents below. This project's
# sql_generator.py wasn't available to verify what it currently returns, so
# this heuristic is the safe default until real usage numbers are confirmed.
_EST_COST_PER_1K_TOKENS_CENTS = 0.15


def _estimate_cost_cents(question: str, retrieval: dict | None, result: dict) -> float:
    usage = result.get("usage") if isinstance(result, dict) else None
    # Sprint 3: per-model cost computed from real provider token counts (config.llm_cost_cents).
    if isinstance(usage, dict) and usage.get("cost_cents") is not None:
        return max(0.0001, float(usage["cost_cents"]))
    if isinstance(usage, dict) and (usage.get("total_tokens") or usage.get("input_tokens")):
        total_tokens = usage.get("total_tokens") or (
            (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
        )
        return max(0.0001, total_tokens / 1000 * _EST_COST_PER_1K_TOKENS_CENTS * 100)
    context_text = retrieval.get("retrieved_context_text", "") if isinstance(retrieval, dict) else ""
    approx_chars = len(question or "") + len(context_text or "") + len(str(result.get("sql", "") or ""))
    approx_tokens = max(1, approx_chars / 4)
    return max(0.0001, approx_tokens / 1000 * _EST_COST_PER_1K_TOKENS_CENTS * 100)


def _estimate_cost_cents_pre_call(question: str, retrieval: dict | None) -> float:
    """Same heuristic as _estimate_cost_cents, but usable BEFORE the LLM call
    happens (no `result` yet, since we haven't generated SQL to know its
    length). Used only to size the atomic Redis reservation below; the real
    cost is computed after the call via _estimate_cost_cents and reconciled."""
    context_text = retrieval.get("retrieved_context_text", "") if isinstance(retrieval, dict) else ""
    approx_chars = len(question or "") + len(context_text or "")
    approx_tokens = max(1, approx_chars / 4)
    return max(0.0001, approx_tokens / 1000 * _EST_COST_PER_1K_TOKENS_CENTS * 100)


# ---------------------------------------------------------------------------
# Redis-backed atomic budget enforcement
#
# PRODUCTION UPGRADE: the previous budget check was "read usage_today, if
# under budget then proceed, then record actual usage afterward" -- three
# separate steps with no locking between them. Two concurrent requests could
# both read "under budget" before either had recorded its spend, letting the
# real total exceed DAILY_LLM_BUDGET_CENTS. This was flagged and explicitly
# left as a documented "best-effort" limitation in an earlier round, since
# fixing it properly needs either a DB row lock/transaction or an external
# atomic counter -- Redis INCRBYFLOAT is the standard tool for exactly this
# (it's a single atomic operation at the Redis server, so two concurrent
# increments can never interleave). Pattern used here is reserve-then-
# reconcile: increment first (atomic), check whether the new total broke the
# budget, refund immediately if so. Worst-case overshoot under concurrent
# requests is bounded to roughly one request's estimated cost, not the
# unbounded overshoot the old check allowed -- this is an honest description,
# not a claim of perfect atomicity across arbitrary concurrency.
#
# Entirely optional: without REDIS_URL configured, everything falls back to
# the original DB-based check further down, unchanged.
# ---------------------------------------------------------------------------
try:
    import redis as _redis_lib
except ImportError:
    _redis_lib = None

redis_client = None
if REDIS_URL and _redis_lib:
    try:
        redis_client = _redis_lib.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        redis_client.ping()
    except Exception:
        logging.getLogger("querysure").warning(
            "REDIS_URL was set but Redis is unreachable -- falling back to "
            "DB-only rate limiting and budget enforcement."
        )
        redis_client = None


def _seconds_until_utc_midnight() -> int:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


def _budget_key(user_id: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"querysure:budget:{user_id}:{day}"


def _redis_incr_with_expiry(key: str, estimated_cents: float, ttl_seconds: int) -> float:
    """INCRBYFLOAT + (first write only) EXPIRE, self-compensating: if the EXPIRE fails the increment is
    taken back, so a half-done reservation can neither leak spend nor leave an immortal key."""
    new_total = float(redis_client.incrbyfloat(key, estimated_cents))
    if new_total == float(estimated_cents):  # first write to this key today -- set expiry once
        try:
            redis_client.expire(key, ttl_seconds)
        except _redis_lib.exceptions.RedisError:
            _best_effort_incr(key, -estimated_cents)
            raise
    return new_total


def _best_effort_incr(key: str, delta: float) -> bool:
    """Compensating write. A failure here can only leave the counter HIGHER than true spend (fail-safe: it
    blocks sooner, never later), so it is logged, not raised."""
    try:
        redis_client.incrbyfloat(key, delta)
        return True
    except Exception:  # noqa: BLE001
        logging.getLogger("querysure").error("Could not roll back Redis budget reservation on %s (over-counted until expiry)", key)
        return False


def _reserve_budget_atomic(user_id: str, estimated_cents: float, limit_cents: float | None = None) -> bool:
    """Atomically reserve `estimated_cents` against today's budget. Returns
    True if the caller may proceed (reservation held), False if it would
    exceed the budget (reservation refunded before returning)."""
    key = _budget_key(user_id)
    limit = DAILY_LLM_BUDGET_CENTS if limit_cents is None else limit_cents
    new_total = _redis_incr_with_expiry(key, estimated_cents, _seconds_until_utc_midnight())
    if new_total > limit:
        _best_effort_incr(key, -estimated_cents)
        return False
    return True


def _reconcile_budget_atomic(user_id: str, reserved_cents: float, actual_cents: float) -> None:
    """Correct the reservation to the real cost once it's known (the
    pre-call estimate and the post-call real estimate will rarely match
    exactly)."""
    diff = actual_cents - reserved_cents
    if diff:
        redis_client.incrbyfloat(_budget_key(user_id), diff)


# ---------------------------------------------------------------------------
# Global (all-users) daily AI spend cap -- same reserve/reconcile pattern as the per-user budget.
# Redis path: atomic INCRBYFLOAT on one shared per-day key. DB fallback (Redis absent/down): the sum of
# today's usage_daily rows, which record_llm_usage() already maintains (not atomic under concurrency,
# same documented limitation as the per-user DB fallback). GLOBAL_DAILY_AI_BUDGET_USD=0 disables it.
# ---------------------------------------------------------------------------
def _global_cap_enabled() -> bool:
    return GLOBAL_DAILY_AI_BUDGET_CENTS > 0


def _global_budget_key() -> str:
    return f"querysure:global_budget:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"


def _reserve_global_atomic(estimated_cents: float) -> bool:
    key = _global_budget_key()
    new_total = _redis_incr_with_expiry(key, estimated_cents, _seconds_until_utc_midnight() + 3600)
    if new_total > GLOBAL_DAILY_AI_BUDGET_CENTS:
        _best_effort_incr(key, -estimated_cents)
        return False
    return True


def _reconcile_global_atomic(reserved_cents: float, actual_cents: float) -> None:
    diff = actual_cents - reserved_cents
    if diff:
        redis_client.incrbyfloat(_global_budget_key(), diff)


def _global_spend_cents_db(db: OrmSession) -> float:
    return global_spend_cents_db(db)


def _global_budget_response():
    return err_response(
        "AI features are temporarily at capacity for today. Please try again after midnight UTC.",
        code=503, error_code="AI_CAPACITY_REACHED",
        headers={"Retry-After": str(_seconds_until_utc_midnight())},
    )


# GAP FIXED: a Redis outage AFTER startup (redis_client set successfully at
# boot, then the connection drops -- a blip, a restart, a network partition)
# used to be fatal: _reserve_budget_atomic()/_reconcile_budget_atomic() were
# called with no error handling, so the ConnectionError/TimeoutError
# propagated all the way up and /api/generate-sql -- the one route that
# actually spends money -- returned a bare 500 for every user, even though a
# working DB-only fallback already existed for the "Redis was never
# configured" case one branch below. `redis_client` being a fixed object set
# once at startup meant that fallback was never reachable once Redis had
# degraded mid-flight.
#
# _redis_down_until is a tiny circuit breaker, mirroring the same pattern
# slowapi already uses for its own Redis-backed rate limiter above: once a
# call fails, skip Redis entirely for a cooldown window instead of paying a
# fresh ~2s connect-timeout stall (socket_connect_timeout=2, set above) on
# every single request during a sustained outage. It clears itself the first
# time a call succeeds again -- no separate recovery probe needed.
_REDIS_BUDGET_COOLDOWN_SECONDS = 30
_redis_down_until = 0.0


def _redis_budget_available() -> bool:
    return bool(redis_client) and time.time() >= _redis_down_until


def _mark_redis_budget_down() -> None:
    global _redis_down_until
    _redis_down_until = time.time() + _REDIS_BUDGET_COOLDOWN_SECONDS
    obs_metrics.record_redis_fallback("budget")
    obs_events.record_event("redis_budget_fallback", severity="warning", component="redis",
                            detail={"component": "budget", "state": "db_fallback"})
    logging.getLogger("querysure").warning(
        "Redis budget check failed -- falling back to DB-only budget "
        "enforcement for %ss.", _REDIS_BUDGET_COOLDOWN_SECONDS,
    )


def _mark_redis_budget_up() -> None:
    global _redis_down_until
    _redis_down_until = 0.0


# CORS_ALLOWED_ORIGINS / CORS_ALLOW_CREDENTIALS now come from config.py,
# which also fails fast at import time if ENVIRONMENT=production and
# CORS_ALLOWED_ORIGINS is still the wildcard default -- so by the time this
# line runs, either we're not in production, or CORS_ALLOWED_ORIGINS is a
# real, explicit origin list. No separate warning needed here anymore.
_ADMISSION_EXEMPT = frozenset({"/api/health", "/api/ready"})
_admission_state: dict = {"loop": None, "sem": None}


def _admission_semaphore() -> asyncio.Semaphore:
    """One semaphore per event loop (uvicorn worker process). Created lazily so import order and tests are unaffected."""
    loop = asyncio.get_running_loop()
    if _admission_state["loop"] is not loop:
        _admission_state["loop"], _admission_state["sem"] = loop, asyncio.Semaphore(_config_module.API_ADMISSION_LIMIT)
    return _admission_state["sem"]


@app.middleware("http")
async def _admission_middleware(request: Request, call_next):
    """Bound in-flight API requests per process (see config.API_ADMISSION_LIMIT for why). Liveness/readiness probes and static
    assets bypass it so the platform can always see the process. A request that cannot be admitted within
    API_ADMISSION_WAIT_SECONDS gets a fast 503 + Retry-After instead of stalling for the 10 s DB pool timeout."""
    path = request.url.path
    if not path.startswith("/api/") or path in _ADMISSION_EXEMPT:
        return await call_next(request)
    sem = _admission_semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=_config_module.API_ADMISSION_WAIT_SECONDS)
    except asyncio.TimeoutError:
        obs_metrics.record_admission_rejected()
        return JSONResponse(status_code=503, headers={"Retry-After": "2"},
                            content={"success": False, "error": "The service is busy. Please retry shortly.", "error_code": "SERVER_BUSY",
                                     "request_id": getattr(request.state, "request_id", None)})
    try:
        return await call_next(request)
    finally:
        sem.release()


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=CORS_ALLOW_CREDENTIALS,
)

def _route_label(request: Request) -> str:
    """Bounded-cardinality route label: the matched route TEMPLATE (``/api/session/{session_id}``), never the raw path."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    raw = request.url.path
    if raw.startswith(("/static", "/assets")):
        return "static"
    return "unmatched"


_QUIET_ROUTES = frozenset({"/api/health", "/api/ready"})


@app.middleware("http")
async def _observability_middleware(request: Request, call_next):
    """Outermost middleware (registered after CORS): correlation context, access log and HTTP metrics for EVERY request,
    including preflights and rejections produced by inner middleware."""
    request_id = obs_context.valid_inbound_request_id(request.headers.get("x-request-id")) or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    ctx = obs_context.new_context(request_id)
    start = time.monotonic()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers.setdefault("X-Request-ID", request_id)
        return response
    finally:
        duration_ms = (time.monotonic() - start) * 1000
        route = _route_label(request)
        ctx.route = route
        obs_metrics.record_http(route, request.method, status, duration_ms)
        if not (route in _QUIET_ROUTES and status < 400):
            # Route TEMPLATE only: the raw path can contain ids; query strings (verification / reset tokens) never appear.
            logger.info("%s %s -> %s (%.1fms) [request_id=%s]", request.method, route, status, duration_ms, request_id,
                        extra={"event": "http_request", "method": request.method, "route": route, "status": status,
                               "duration_ms": round(duration_ms, 1)})
        obs_context.clear()


def _ai_budget_probe() -> dict:
    """Global AI spend state for readiness / alerts / the ops dashboard: percentages and a boolean, never per-user figures."""
    if not _global_cap_enabled():
        return {"enabled": False}
    spent = None
    if _redis_budget_available():
        try:
            raw = redis_client.get(_global_budget_key())
            spent = float(raw) if raw else 0.0
        except Exception:  # noqa: BLE001
            spent = None
    if spent is None:
        from db import SessionLocal
        db = SessionLocal()
        try:
            spent = global_spend_cents_db(db)
        except Exception:  # noqa: BLE001
            return {"enabled": True, "known": False}
        finally:
            db.close()
    pct = round(100.0 * spent / GLOBAL_DAILY_AI_BUDGET_CENTS, 1)
    return {"enabled": True, "known": True, "spent_pct": pct, "cap_usd": round(GLOBAL_DAILY_AI_BUDGET_CENTS / 100, 2),
            "global_cap_reached": spent >= GLOBAL_DAILY_AI_BUDGET_CENTS}


obs_health.register_probe("ai_budget", _ai_budget_probe)


def _start_observability() -> None:
    """Startup hooks: optional Sentry, gauge publisher (DB pool / breaker state for the ops view), maintenance loop."""
    obs_errors.init_sentry()
    obs_gauges.start_publisher("web")
    obs_alerts.start_maintenance()


UPLOAD_DIR = storage.UPLOAD_DIR
os.makedirs(UPLOAD_DIR, exist_ok=True)
# GAP FIXED: uploads were read fully into memory with no size ceiling
# (`contents = await file.read()`), so a large file could cause memory
# pressure before any validation happened. These caps are enforced in
# connect_upload() below, checked against Content-Length first (fast path,
# rejects before reading anything) and against actual bytes read as a
# backup (in case a client lies about Content-Length).
MAX_UPLOAD_CSV_BYTES = 50 * 1024 * 1024    # 50 MB (also applies to .json)
MAX_UPLOAD_DB_BYTES = 100 * 1024 * 1024    # 100 MB
MAX_UPLOAD_XLSX_BYTES = 50 * 1024 * 1024   # 50 MB
MAX_SQL_LENGTH = 20_000
MAX_QUESTION_LENGTH = 4_000

# Extensions accepted by /api/connect/upload, and the size ceiling each maps
# to. Kept as one table so adding a format later is a one-line change here
# rather than touching the branching logic below.
UPLOAD_SIZE_LIMITS = {
    ".csv": MAX_UPLOAD_CSV_BYTES,
    ".json": MAX_UPLOAD_CSV_BYTES,
    ".db": MAX_UPLOAD_DB_BYTES,
    ".sqlite": MAX_UPLOAD_DB_BYTES,
    ".sqlite3": MAX_UPLOAD_DB_BYTES,
    ".xlsx": MAX_UPLOAD_XLSX_BYTES,
    ".xlsm": MAX_UPLOAD_XLSX_BYTES,
}


def _stream_upload_to_disk(file: UploadFile, dest_path: str, limit: int, chunk_size: int = 1024 * 1024) -> bool:
    """Streams an UploadFile straight to `dest_path` in fixed-size chunks,
    aborting (and deleting the partial file) the moment the running total
    exceeds `limit`.

    REFINEMENT over the previous version: that version bounded memory
    against a MALICIOUS oversized upload correctly (it aborted before ever
    holding more than ~limit + chunk_size bytes, contrary to the claim that
    it didn't -- it did exactly what it said), but it still held the FULL
    file in memory for any legitimate upload under the limit, via
    `chunks.append(...)` + `b"".join(...)`. Writing each chunk straight to
    disk as it arrives means peak memory is ~chunk_size (1 MB) regardless of
    whether the file is 10 KB or 100 MB. Returns True on success, False if
    the limit was exceeded (caller should treat that as a 413; the partial
    file is already cleaned up before returning).
    """
    total = 0
    with open(dest_path, "wb") as out:
        while True:
            chunk = file.file.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                out.close()
                try:
                    os.remove(dest_path)
                except OSError:
                    pass
                return False
            out.write(chunk)
    return True
_root_db = os.path.join(ROOT, "retail.db")
_sample_db = os.path.join(ROOT, "sample_db", "retail.db")
SAMPLE_DB = _root_db if os.path.exists(_root_db) else _sample_db
STATIC_DIR = os.path.join(ROOT, "static")
if os.path.isdir(STATIC_DIR):
    class _CachedStaticFiles(StaticFiles):
        """Content-addressed assets (static/dist/*, or any URL carrying ?v=<content hash>) are immutable for a year; everything
        else is revalidated on each load via the ETag StaticFiles already sends, so a deploy is never masked by a stale cache."""

        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            if response.status_code in (200, 304):
                versioned = path.startswith("dist/") or b"v=" in scope.get("query_string", b"")
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if versioned else "no-cache"
            return response

    app.mount("/static", _CachedStaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def _on_startup():
    await run_in_threadpool(init_db)
    anyio.to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_MAX_WORKERS
    await run_in_threadpool(_start_observability)


@app.exception_handler(SATimeoutError)
async def sqlalchemy_timeout_handler(request: Request, exc: SATimeoutError):
    if "QueuePool limit" in str(exc):
        obs_metrics.record_db_busy()
    # QueuePool emits this distinctive message when checkout reaches
    # pool_timeout. Other SQLAlchemy TimeoutError cases must remain generic 500s.
    if "QueuePool limit" not in str(exc):
        logger.exception("Unhandled SQLAlchemy timeout [request_id=%s]", getattr(request.state, "request_id", None), exc_info=exc)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error.", "request_id": getattr(request.state, "request_id", None)})
    return JSONResponse(status_code=503, content={"success": False, "error": "Database is temporarily busy. Please retry shortly.", "error_code": "SERVER_BUSY", "request_id": getattr(request.state, "request_id", None)}, headers={"Retry-After": "5"})


@app.exception_handler(DatabaseBusyError)
async def database_busy_handler(request: Request, exc: DatabaseBusyError):
    obs_metrics.record_db_busy()
    return JSONResponse(status_code=503, content={"success": False, "error": "Database is temporarily busy. Please retry shortly.", "error_code": "SERVER_BUSY", "request_id": getattr(request.state, "request_id", None)}, headers={"Retry-After": "5"})


@app.exception_handler(SessionNotFoundError)
async def session_not_found_handler(request: Request, exc: SessionNotFoundError):
    return JSONResponse(status_code=404, content={"success": False, "error": "Session not found.", "error_code": "SESSION_NOT_FOUND", "request_id": getattr(request.state, "request_id", None)})


@app.exception_handler(SessionConflictError)
async def session_conflict_handler(request: Request, exc: SessionConflictError):
    return JSONResponse(status_code=409, content={"success": False, "error": "This session was changed by another request. Please retry.", "error_code": "SESSION_CONFLICT", "request_id": getattr(request.state, "request_id", None)})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Never leak stack traces or internals to the client; log server-side instead.
    import logging
    request_id = getattr(request.state, "request_id", None)
    # Sprint 4: type + fingerprint + safe frames only; the exception MESSAGE is never stored or forwarded (it can carry row
    # values / SQL / e-mail from the DB driver). The log line itself is scrubbed and logs the route template, not the raw path.
    fingerprint = obs_errors.capture_exception(exc, component="api", request_id=request_id)
    logging.getLogger("querysure").error(
        "Unhandled error on %s %s [request_id=%s]", request.method, _route_label(request), request_id,
        extra={"event": "unhandled_exception", "fingerprint": fingerprint, "exc_type": type(exc).__name__},
    )
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "Internal server error.", "request_id": request_id},
    )


def err_response(msg: str, code: int = 400, error_code: str | None = None, headers: dict | None = None):
    # error_code is an optional short machine-readable tag (e.g. "NO_LLM_PROVIDER",
    # "NO_DATABASE") so the frontend can react specifically (open the API-key
    # settings panel, prompt to connect a database, etc.) instead of only being
    # able to string-match the human-readable `error` message.
    content = {"success": False, "error": msg}
    if error_code:
        content["error_code"] = error_code
    return JSONResponse(status_code=code, content=content, headers=headers)


def ok_response(data: dict):
    data["success"] = True
    return JSONResponse(content=data)


def _release_superseded_dataset(db: OrmSession, old_ref: str | None, new_ref: str | None, session_id: str) -> None:
    """Deletes the dataset a session USED to point at once it has been
    replaced (re-upload, or switching to the sample database), so replaced
    uploads no longer pile up in storage forever.

    Safety rules -- it only ever deletes a dataset if ALL hold:
      * it actually changed (old != new);
      * it is a managed upload (never the bundled sample database);
      * no other session row still references it.
    Best-effort: a cleanup failure must never fail the user's request.
    """
    try:
        if not old_ref or old_ref == new_ref or not storage.is_managed(old_ref):
            return
        still_used = (
            db.query(PipelineSession.id)
            .filter(PipelineSession.db_path == old_ref, PipelineSession.id != session_id)
            .first()
        )
        if still_used:
            return
        storage.delete_dataset(old_ref)
    except Exception as e:
        logger.warning("superseded dataset cleanup failed for session %s: %s", session_id, e)



def _clear_query_state(row: PipelineSession) -> dict:
    """Invalidate all artifacts derived from a prior question/dataset."""
    return {
        "retrieval": None,
        "sql": None,
        "sql_result": None,
        "question": None,
        "validation": None,
        "query_result": None,
        "reliability": None,
        "insights": None,
    }

def _schema_summary(schema: dict) -> list:
    out = []
    for name, info in schema["tables"].items():
        out.append({
            "name": name,
            "rows": info["row_count"],
            "columns": [
                {
                    "name": c["name"],
                    "type": c["type"],
                    "pk": c["pk"],
                    "fk": any(fk["from"] == c["name"] for fk in info["foreign_keys"]),
                }
                for c in info["columns"]
            ],
            "foreign_keys": info["foreign_keys"],
        })
    return out


def _calc_health(schema: dict) -> int:
    score = 100
    for _, info in schema["tables"].items():
        has_pk = any(c["pk"] for c in info["columns"])
        if not has_pk:
            score -= 5
        if info["row_count"] == 0:
            score -= 5
    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.post("/api/auth/register", response_model=UserOut)
@limiter.limit("3/minute")
def register(request: Request, payload: UserCreate, db: OrmSession = Depends(get_db)):
    email = normalize_email(payload.email)
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        return err_response("An account with this email already exists.", code=409, error_code="ACCOUNT_EXISTS")

    user = User(email=email, hashed_password=hash_password(payload.password), email_verified=False, auth_version=0)
    raw_token, token_hash = new_token()
    verification = EmailVerificationToken(
        user_id=user.id or "", token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return err_response("An account with this email already exists.", code=409, error_code="ACCOUNT_EXISTS")
    verification.user_id = user.id
    db.add(verification)
    try:
        db.commit()
        db.refresh(user)
        url = verification_url(raw_token)
        enqueue_job(db, user.id, "auth_email", {
            "to": email, "subject": "Verify your QuerySure email",
            "text": "Verify your QuerySure account by opening this link:\n\n" + url + "\n\nThis link expires in 24 hours and can only be used once.",
        })
    except Exception:
        db.rollback()
        logger.exception("Authentication email job creation failed during registration")
        return err_response("We could not complete registration right now. Please try again.", code=503, error_code="EMAIL_DELIVERY_FAILED")
    return UserOut.model_validate(user)


def _utc_datetime(value: datetime | None) -> datetime | None:
    """Normalize DB datetimes to aware UTC without changing stored semantics.

    SQLite does not preserve timezone information for SQLAlchemy DateTime(timezone=True),
    so local development can return a naive datetime even though production Postgres
    returns an aware one. Comparing those directly raises TypeError and becomes a 500.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _email_cooldown_active(db: OrmSession, model, user_id: str) -> bool:
    """True if this account was sent a token of this kind within AUTH_EMAIL_COOLDOWN_SECONDS (stops one inbox being
    mail-bombed from many IPs, which the per-IP limiter cannot see). 0 disables."""
    cooldown = _config_module.AUTH_EMAIL_COOLDOWN_SECONDS
    if cooldown <= 0:
        return False
    latest = db.query(model.created_at).filter(model.user_id == user_id).order_by(model.created_at.desc()).first()
    return bool(latest and datetime.now(timezone.utc) - _utc_datetime(latest[0]) < timedelta(seconds=cooldown))


@app.post("/api/auth/verify-email")
@limiter.limit("10/minute")
def verify_email(request: Request, token: str, db: OrmSession = Depends(get_db)):
    # Raw verification tokens are never logged or stored. Reject obviously malformed
    # input before touching the database, then handle all expected lookup/transaction
    # failures as controlled auth responses rather than exposing a 500.
    if not token or len(token) > 256 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return err_response("This verification link is invalid or has already been used.", code=400, error_code="INVALID_VERIFICATION_TOKEN")
    now = datetime.now(timezone.utc)
    try:
        row = db.query(EmailVerificationToken).filter(
            EmailVerificationToken.token_hash == hash_token(token)
        ).first()
        expires_at = _utc_datetime(row.expires_at) if row else None
        if not row or row.used_at is not None or expires_at is None:
            return err_response("This verification link is invalid or has already been used.", code=400, error_code="INVALID_VERIFICATION_TOKEN")
        if expires_at <= now:
            return err_response("Verification link expired. Please request a new verification email.", code=400, error_code="EXPIRED_VERIFICATION_TOKEN")

        user = db.query(User).filter(User.id == row.user_id).first()
        if not user or not user.is_active:
            return err_response("This verification link is invalid or expired.", code=400, error_code="INVALID_VERIFICATION_TOKEN")

        row.used_at = now
        user.email_verified = True
        db.query(EmailVerificationToken).filter(
            EmailVerificationToken.user_id == user.id,
            EmailVerificationToken.id != row.id,
            EmailVerificationToken.used_at.is_(None),
        ).update({EmailVerificationToken.used_at: now}, synchronize_session=False)
        db.commit()
        return {"success": True, "email_verified": True}
    except (SQLAlchemyError, TypeError, ValueError):
        db.rollback()
        logger.exception("Email verification failed during token lookup or commit")
        return err_response("Verification could not be completed. Please try again.", code=503, error_code="VERIFICATION_UNAVAILABLE")


@app.post("/api/auth/resend-verification")
@limiter.limit("3/hour")
def resend_verification(request: Request, payload: EmailRequest, db: OrmSession = Depends(get_db)):
    email = normalize_email(payload.email)
    user = db.query(User).filter(User.email == email).first()
    generic = {"success": True, "message": "If the account exists and needs verification, QuerySure will attempt to send a new verification email."}
    if not user or user.email_verified or not user.is_active or _email_cooldown_active(db, EmailVerificationToken, user.id):
        return generic
    db.query(EmailVerificationToken).filter(
        EmailVerificationToken.user_id == user.id, EmailVerificationToken.used_at.is_(None)
    ).update({EmailVerificationToken.used_at: datetime.now(timezone.utc)}, synchronize_session=False)
    raw_token, token_hash = new_token()
    db.add(EmailVerificationToken(
        user_id=user.id, token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    ))
    try:
        db.commit()
        enqueue_job(db, user.id, "auth_email", {
            "to": email, "subject": "Verify your QuerySure email",
            "text": "Verify your QuerySure account:\n\n" + verification_url(raw_token) + "\n\nThis link expires in 24 hours and can only be used once.",
        })
    except Exception:
        db.rollback()
        logger.exception("Authentication email job creation failed during verification resend")
    return generic


@app.post("/api/auth/login", response_model=Token)
@limiter.limit("5/minute")
async def login(request: Request, db: OrmSession = Depends(get_db)):
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        raw = await request.body()
        try:
            decoded = json.loads(raw.decode("utf-8") if raw else "{}")
            payload = LoginBody.model_validate(decoded)
        except Exception as exc:
            raise RequestValidationError([
                {"type": "json_invalid", "loc": ("body",), "msg": "Invalid JSON request body.", "input": None}
            ]) from exc
        email, password = payload.email, payload.password
    else:
        form = await request.form()
        email, password = form.get("username") or form.get("email"), form.get("password")
    if not email or not password:
        return err_response("Email and password are required.", code=422)
    user = await run_in_threadpool(authenticate_user, db, email, password)
    if not user:
        return err_response("Incorrect email or password.", code=401)
    if not user.email_verified:
        return err_response("Please verify your email before signing in.", code=403, error_code="EMAIL_NOT_VERIFIED")
    return {"access_token": issue_session_token(db, user), "token_type": "bearer"}


@app.post("/api/auth/change-password", response_model=Token)
@limiter.limit("5/minute")
def change_password(
    request: Request,
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    if is_google_only_account(current_user):
        return err_response(
            "This account does not have a password set. Use password reset to add one.",
            code=409,
            error_code="PASSWORD_NOT_SET",
        )
    if not verify_password(payload.current_password, current_user.hashed_password):
        return err_response("Current password is incorrect.", code=401, error_code="INVALID_CURRENT_PASSWORD")
    if payload.current_password == payload.new_password:
        return err_response("New password must be different from the current password.", code=422, error_code="PASSWORD_UNCHANGED")
    current_user.hashed_password = hash_password(payload.new_password)
    # Every other signed-in device is signed out; the caller gets a fresh session so this device stays signed in.
    revoke_all_sessions(db, current_user, "password_change", commit=False)
    return {"access_token": issue_session_token(db, current_user), "token_type": "bearer"}


@app.post("/api/auth/forgot-password")
@limiter.limit("3/hour")
def forgot_password(request: Request, payload: EmailRequest, db: OrmSession = Depends(get_db)):
    email = normalize_email(payload.email)
    user = db.query(User).filter(User.email == email, User.is_active == 1).first()
    generic = {"success": True, "message": "If an account exists for that email, QuerySure will attempt to send password reset instructions."}
    if not user or _email_cooldown_active(db, PasswordResetToken, user.id):
        return generic
    now = datetime.now(timezone.utc)
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None)
    ).update({PasswordResetToken.used_at: now}, synchronize_session=False)
    raw_token, token_hash = new_token()
    db.add(PasswordResetToken(
        user_id=user.id, token_hash=token_hash, expires_at=now + timedelta(hours=1)
    ))
    try:
        db.commit()
        enqueue_job(db, user.id, "auth_email", {
            "to": email, "subject": "Reset your QuerySure password",
            "text": "Reset your QuerySure password:\n\n" + password_reset_url(raw_token) + "\n\nThis link expires in 1 hour and can only be used once.",
        })
    except Exception:
        db.rollback()
        logger.exception("Authentication email job creation failed during password reset request")
    return generic


@app.post("/api/auth/reset-password")
@limiter.limit("5/minute")
def reset_password(request: Request, token: str, payload: PasswordRequest, db: OrmSession = Depends(get_db)):
    row = db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == hash_token(token)).first()
    now = datetime.now(timezone.utc)
    expires_at = _utc_datetime(row.expires_at) if row else None
    if not row or row.used_at is not None or expires_at is None or expires_at <= now:
        return err_response("This password reset link is invalid or expired.", code=400, error_code="INVALID_RESET_TOKEN")
    user = db.query(User).filter(User.id == row.user_id, User.is_active == 1).first()
    if not user:
        return err_response("This password reset link is invalid or expired.", code=400, error_code="INVALID_RESET_TOKEN")
    user.hashed_password = hash_password(payload.password)
    revoke_all_sessions(db, user, "password_reset", commit=False)
    row.used_at = now
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id, PasswordResetToken.id != row.id, PasswordResetToken.used_at.is_(None)
    ).update({PasswordResetToken.used_at: now}, synchronize_session=False)
    db.commit()
    return {"success": True, "message": "Password reset successfully. Please sign in with your new password."}



@app.post("/api/auth/google", response_model=Token)
@limiter.limit("10/minute")
def google_sign_in(request: Request, payload: GoogleSignInRequest, db: OrmSession = Depends(get_db)):
    identity, error = _verified_google_claims(payload.credential, action="sign-in")
    if error is not None:
        return error
    subject, email = identity

    user = db.query(User).filter(User.google_subject == subject).first()
    if user:
        if not user.is_active:
            return err_response("This account is inactive.", code=403, error_code="ACCOUNT_INACTIVE")
        return {"access_token": issue_session_token(db, user), "token_type": "bearer"}

    # Never silently attach Google to a password account based only on email.
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        return err_response(
            "An account already exists for this email. Sign in with your password first, then link Google from your account.",
            code=409,
            error_code="GOOGLE_ACCOUNT_LINK_REQUIRED",
        )

    # Google has already verified control of this email. Keep the existing
    # password/session model by assigning an unreachable random password;
    # password reset remains the explicit path for adding a password later.
    user = User(
        email=email,
        hashed_password=make_google_only_password(),
        email_verified=True,
        auth_version=0,
        google_subject=subject,
    )
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        return err_response("Google sign-in could not complete. Please try again.", code=409, error_code="GOOGLE_ACCOUNT_CONFLICT")
    return {"access_token": issue_session_token(db, user), "token_type": "bearer"}


@app.post("/api/auth/google/link")
@limiter.limit("10/minute")
def google_link(request: Request, payload: GoogleSignInRequest, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
    if not GOOGLE_CLIENT_ID:
        return err_response("Google sign-in is not configured for this deployment.", code=503, error_code="GOOGLE_NOT_CONFIGURED")
    if current_user.google_subject:
        return err_response("A Google identity is already linked to this account.", code=409, error_code="GOOGLE_ALREADY_LINKED")
    identity, error = _verified_google_claims(payload.credential, action="account linking")
    if error is not None:
        return error
    subject, email = identity
    if email != normalize_email(current_user.email):
        return err_response("The Google account email must match your QuerySure account.", code=403, error_code="GOOGLE_EMAIL_MISMATCH")
    other = db.query(User).filter(User.google_subject == subject, User.id != current_user.id).first()
    if other:
        return err_response("That Google identity is already linked to another QuerySure account.", code=409, error_code="GOOGLE_IDENTITY_IN_USE")
    current_user.google_subject = subject
    current_user.email_verified = True
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return err_response("That Google identity is already linked to another QuerySure account.", code=409, error_code="GOOGLE_IDENTITY_IN_USE")
    return {"success": True, "google_linked": True}

@app.post("/api/auth/logout")
def logout(current_user: User = Depends(get_current_user)):
    """Legacy client-side sign-out (kept for API compatibility): the token is NOT revoked server-side.
    Use POST /api/auth/sign-out, which is what the web app calls."""
    return {"success": True, "message": "Signed out."}


@app.post("/api/auth/sign-out")
def sign_out(
    token: str | None = Depends(oauth2_scheme),
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """Revoke THIS token server-side: it stops working immediately, not merely when the client forgets it."""
    revoke_session(db, decode_token_claims(token).get("sid"), current_user.id, "logout")
    return {"success": True, "message": "Signed out."}


@app.post("/api/auth/logout-all")
@limiter.limit("10/minute")
def logout_all(request: Request, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
    """Sign out every device, this one included (all tokens of the account are revoked)."""
    revoke_all_sessions(db, current_user, "logout_all")
    return {"success": True, "message": "Signed out everywhere."}


@app.get("/api/auth/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@app.get("/api/auth/methods")
def auth_methods(current_user: User = Depends(get_current_user)):
    """Which sign-in methods the account has (Google linked, password set)."""
    return {
        "google_linked": bool(current_user.google_subject),
        "has_password": not is_google_only_account(current_user),
    }


# ---------------------------------------------------------------------------
# Pipeline routes — all require an authenticated user and a DB-backed session
# ---------------------------------------------------------------------------

_ASSET_URLS: dict | None = None


def _asset_urls() -> dict:
    """Source asset URL -> URL to serve. Built once per process (static files do not change while it runs)."""
    global _ASSET_URLS
    if _ASSET_URLS is None:
        urls: dict = {}
        manifest_path = os.path.join(STATIC_DIR, "dist", "manifest.json")
        try:
            with open(manifest_path, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
            built_ok = all(os.path.isfile(os.path.join(STATIC_DIR, v[len("/static/"):])) for v in manifest.values())
            if built_ok and not IS_PRODUCTION:
                # A dist/ left over from an earlier build must never mask edited sources in development: if any source is
                # newer than the manifest, serve the sources (cache-busted by content hash) instead of the stale bundle.
                built_at = os.path.getmtime(manifest_path)
                for rel in manifest:
                    src = os.path.join(STATIC_DIR, rel[len("/static/"):])
                    if os.path.isfile(src) and os.path.getmtime(src) > built_at:
                        built_ok = False
                        break
            if built_ok:
                urls = dict(manifest)
        except (OSError, ValueError, TypeError, AttributeError):
            urls = {}
        if not urls:
            import hashlib
            for rel in ("js/app.js", "js/scene.js", "js/ops.js", "css/styles.css", "css/ops.css"):
                try:
                    with open(os.path.join(STATIC_DIR, rel), "rb") as fh:
                        urls["/static/" + rel] = f"/static/{rel}?v={hashlib.sha256(fh.read()).hexdigest()[:10]}"
                except OSError:
                    continue
        _ASSET_URLS = urls
    return _ASSET_URLS


def _html_page(filename: str):
    path = os.path.join(STATIC_DIR, filename)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        html = fh.read()
    for src, dst in _asset_urls().items():
        html = html.replace(f'"{src}"', f'"{dst}"')
    # The shell is revalidated every load so a deploy is picked up immediately; its asset URLs are content-addressed.
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    page = _html_page("index.html")
    if page is not None:
        return page
    return JSONResponse({"message": "QuerySure API is running. Place index.html in /static/"})


@app.post("/api/session")
def new_session(
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    try:
        row = quotas.create_session_with_quota(db, current_user)  # lock + check + insert in one transaction
    except quotas.QuotaExceeded as exc:
        return JSONResponse(status_code=403, content=exc.as_payload())
    return ok_response({"session_id": row.id})


@app.get("/api/sessions")
def list_sessions(
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """Owner-scoped list of the caller's sessions plus quota usage, so a client can free space when it hits a
    QUOTA_EXCEEDED. Additive: the bundled frontend does not call it."""
    rows = (db.query(PipelineSession).filter(PipelineSession.user_id == current_user.id)
            .order_by(PipelineSession.updated_at.desc()).limit(500).all())
    return ok_response({
        "sessions": [{
            "session_id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "has_dataset": bool(r.db_path),
            "dataset_bytes": r.dataset_bytes,
        } for r in rows],
        "quota": {"limits": _config_module.plan_quota(current_user.plan), "used": quotas.usage_for_user(db, current_user.id)},
    })


@app.delete("/api/session/{session_id}")
def delete_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """Delete one of the caller's sessions and release its dataset (only if no other session shares it)."""
    row = db.query(PipelineSession).filter(PipelineSession.id == session_id, PipelineSession.user_id == current_user.id).first()
    if row is None:
        return err_response("Session not found.", code=404, error_code="SESSION_NOT_FOUND")
    old_ref = row.db_path
    db.delete(row)
    db.commit()
    clear_cache_key(session_id)
    _release_superseded_dataset(db, old_ref, None, session_id)
    return ok_response({"deleted": True, "session_id": session_id})


@app.post("/api/session/flush")
@limiter.limit("20/minute")
def flush_session(
    request: Request,
    payload: FlushSessionRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """Clear the active session completely without creating a stale successor.

    Dataset storage is released only when no other session references it. All
    pipeline artifacts (schema, semantic layer, retrieval, SQL, validation,
    result, reliability, insights, and question) are cleared, and the in-process
    retrieval cache for this session is evicted. Trust trace is derived from these
    fields, so clearing them also removes trace state.
    """
    body = payload.model_dump(exclude_none=True) if payload else {}
    session_id = body.get("session_id")
    if not session_id:
        return err_response("session_id is required")
    row = get_or_create_session(db, user_id=current_user.id, session_id=session_id)
    old_ref = row.db_path
    cleared = {field: None for field in SESSION_FIELDS if field != "db_path"}
    cleared["db_path"] = None
    cleared["dataset_bytes"] = None
    row = update_session(db, row, **cleared)
    clear_cache_key(row.id)
    _release_superseded_dataset(db, old_ref, None, row.id)
    return ok_response({"session_id": row.id, "flushed": True, "dataset_connected": False})


@app.post("/api/connect/sample")
@limiter.limit("10/minute")
def connect_sample(
    request: Request,
    payload: SampleRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    body = payload.model_dump(exclude_none=True) if payload else {}
    session_id = body.get("session_id")

    if not os.path.exists(SAMPLE_DB):
        return err_response("Sample database not found. Run: python create_retail.py")

    row = get_or_create_session(db, user_id=current_user.id, session_id=session_id)
    old_ref = row.db_path
    schema = load_database(SAMPLE_DB)
    semantic = build_semantic_layer(schema)
    clear_cache_key(row.id)
    build_vector_store(semantic, cache_key=row.id)
    row = update_session(db, row, db_path=SAMPLE_DB, dataset_bytes=None, schema=schema, semantic=semantic, **_clear_query_state(row))
    _release_superseded_dataset(db, old_ref, SAMPLE_DB, row.id)

    return ok_response({
        "session_id": row.id,
        "db_name": os.path.basename(SAMPLE_DB),
        "table_count": schema["table_count"],
        "total_rows": schema["total_rows"],
        "relationships": len(schema["relationships"]),
        "db_size": schema["db_size"],
        "tables": _schema_summary(schema),
        "health_score": _calc_health(schema),
    })


@app.post("/api/connect/upload")
@limiter.limit("10/minute")
def connect_upload(
    request: Request,
    file: UploadFile = File(...),
    session_id: str = Form(None),
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    fname = sanitize_filename(file.filename)
    ext = os.path.splitext(fname)[1].lower()
    if ext not in UPLOAD_SIZE_LIMITS:
        return err_response(
            "Unsupported file type. Upload a .csv, .json, .xlsx, .xlsm, .db, .sqlite, or .sqlite3 file."
        )

    size_limit = UPLOAD_SIZE_LIMITS[ext]
    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > size_limit:
        limit_mb = size_limit // (1024 * 1024)
        return err_response(f"File too large. Maximum allowed size is {limit_mb} MB.", code=413)

    raw_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}{ext}")
    ok = _stream_upload_to_disk(file, raw_path, size_limit)
    if not ok:
        limit_mb = size_limit // (1024 * 1024)
        return err_response(f"File too large. Maximum allowed size is {limit_mb} MB.", code=413)

    upload_size = os.path.getsize(raw_path)
    try:
        quotas.check_upload_quota(db, current_user, upload_size, session_id)  # cheap early reject; not authoritative
    except quotas.QuotaExceeded as exc:
        storage.discard_local(raw_path)
        return JSONResponse(status_code=403, content=exc.as_payload())

    kind = {".csv": "CSV", ".json": "JSON", ".xlsx": "Excel", ".xlsm": "Excel"}.get(ext, "SQLite database")
    is_sqlite = ext in {".db", ".sqlite", ".sqlite3"}
    # Heavy conversion, profiling, semantic construction and vector indexing are
    # deliberately deferred to the worker. The request only streams the upload
    # to disk and creates a durable job record.

    path = raw_path if is_sqlite else raw_path.rsplit(ext, 1)[0] + ".db"
    object_ref = raw_path
    try:
        object_ref = storage.stage_upload(raw_path, current_user.id, ext)
    except storage.StorageError:
        storage.discard_local(raw_path)
        return err_response("Uploaded file could not be stored. Please retry shortly.", code=503, error_code="STORAGE_UNAVAILABLE")
    if object_ref != raw_path:
        storage.discard_local(raw_path)

    def _queue_upload():
        row = get_or_create_session(db, user_id=current_user.id, session_id=session_id)
        # Authoritative quota decision: owner lock + check, then the job (which records the reservation)
        # commits in the SAME transaction, so concurrent uploads cannot both take the last slot.
        quotas.lock_and_reserve_upload(db, current_user, upload_size, row.id)
        job = enqueue_job(
            db, current_user.id, "dataset_ingestion",
            {"object_ref": object_ref, "path": path, "ext": ext, "fname": fname, "session_id": row.id, "size": upload_size},
        )
        return row.id, job.id
    try:
        created_session_id, job_id = _queue_upload()
    except quotas.QuotaExceeded as exc:
        if object_ref != raw_path:
            storage.delete_staged(object_ref, current_user.id)
        else:
            storage.discard_local(raw_path)
        return JSONResponse(status_code=403, content=exc.as_payload())
    except Exception:
        if object_ref != raw_path:
            storage.delete_staged(object_ref, current_user.id)
        else:
            storage.discard_local(raw_path)
        raise
    return JSONResponse(status_code=202, content={"success": True, "job_id": job_id, "session_id": created_session_id})


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == current_user.id).first()
    if not job:
        return err_response("Job not found.", code=404, error_code="JOB_NOT_FOUND")
    payload = {"success": True, "job_id": job.id, "status": job.status, "error_code": job.error_code}
    if job.result_ref:
        try:
            stored = json.loads(job.result_ref)
            if job.status == "succeeded":
                payload["result"] = stored
            elif job.status == "failed":
                payload["error"] = stored.get("error", "The background operation failed.")
        except Exception:
            payload["result"] = None
    return ok_response(payload)


@app.post("/api/schema-intelligence")
@limiter.limit("30/minute")
def schema_intelligence(
    request: Request,
    payload: SchemaIntelligenceRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    body = payload.model_dump(exclude_none=True) if payload else {}
    row = get_or_create_session(db, user_id=current_user.id, session_id=body.get("session_id"))
    if not row.schema:
        return err_response("No schema loaded")
    profiles = []
    for name, info in row.schema.get("tables", {}).items():
        profiles.append({
            "table": name,
            "row_count": info["row_count"],
            "column_count": len(info["columns"]),
            "has_primary_key": any(c["pk"] for c in info["columns"]),
            "foreign_key_count": len(info["foreign_keys"]),
        })
    return ok_response({"session_id": row.id, "table_profiles": profiles, "semantic_preview": row.semantic})


@app.post("/api/semantic-layer")
@limiter.limit("30/minute")
def api_semantic_layer(
    request: Request,
    payload: SemanticLayerRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    body = payload.model_dump(exclude_none=True) if payload else {}
    row = get_or_create_session(db, user_id=current_user.id, session_id=body.get("session_id"))
    if not row.schema:
        return err_response("No schema loaded")
    semantic = build_semantic_layer(row.schema)
    build_vector_store(semantic, cache_key=row.id)
    row = update_session(db, row, semantic=semantic)
    return ok_response({**semantic, "session_id": row.id})


@app.get("/api/dataset/preview")
@limiter.limit("60/minute")
def api_dataset_preview(
    request: Request,
    session_id: str = "",
    table: str = "",
    page: int = 1,
    page_size: int = PREVIEW_MAX_ROWS,
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """Dataset Explorer: one page of one profiled table (default 10 rows, ``page_size`` capped at 50). Owner-scoped,
    read-only, uses no daily query slot and does not touch the session's question / SQL / result state."""
    row = (db.query(PipelineSession)
           .filter(PipelineSession.id == session_id, PipelineSession.user_id == current_user.id).first())
    if row is None:
        return err_response("Session not found.", code=404, error_code="SESSION_NOT_FOUND")
    if not row.db_path or not row.schema:
        return err_response("No database connected", error_code="NO_DATABASE")
    tables = (row.schema or {}).get("tables", {})
    if table not in tables:
        return err_response("Table not found in this dataset.", code=404, error_code="TABLE_NOT_FOUND")
    try:
        local_db_path = storage.ensure_local(row.db_path, current_user.id)
    except storage.DatasetUnavailable:
        return err_response("Your uploaded dataset is no longer available on the server. Please upload it again.",
                            code=410, error_code="DATASET_UNAVAILABLE")
    except storage.StorageError:
        return err_response("Dataset storage is temporarily unavailable. Please try again in a moment.",
                            code=503, error_code="STORAGE_UNAVAILABLE")
    page_size = max(1, min(int(page_size), PREVIEW_MAX_PAGE_SIZE))
    total_rows = tables[table].get("row_count")
    total_pages = max(1, -(-int(total_rows) // page_size)) if isinstance(total_rows, int) else None
    page = max(1, int(page))
    if total_pages is not None:
        page = min(page, total_pages)
    try:
        preview = preview_table(local_db_path, table, tables, limit=page_size, offset=(page - 1) * page_size)
    except Exception:  # noqa: BLE001 - a broken table must not become a 500 with internals in it
        logger.warning("Dataset preview failed", exc_info=True)
        return err_response("This table could not be previewed.", code=422, error_code="PREVIEW_FAILED")
    return ok_response({
        "session_id": row.id, "table": table, "columns": preview["columns"], "rows": preview["rows"],
        "row_count": len(preview["rows"]), "limit": page_size, "page": page, "page_size": page_size,
        "total_pages": total_pages, "total_rows": total_rows,
    })


@app.post("/api/retrieve-tables")
@limiter.limit("30/minute")
def api_retrieve_tables(
    request: Request,
    payload: RetrieveTablesRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    body = payload.model_dump(exclude_none=True) if payload else {}
    row = get_or_create_session(db, user_id=current_user.id, session_id=body.get("session_id"))
    question = (body.get("question") or "").strip()
    if not row.schema:
        return err_response("No database connected")
    if not question:
        return err_response("Question is required")
    if len(question) > MAX_QUESTION_LENGTH:
        return err_response(f"Question is too long (max {MAX_QUESTION_LENGTH} characters).", code=413)
    semantic = row.semantic or build_semantic_layer(row.schema)
    retrieval = retrieve_context(question, semantic, cache_key=row.id)
    row = update_session(db, row, retrieval=retrieval, semantic=semantic, sql=None, sql_result=None, question=question, validation=None, query_result=None, reliability=None, insights=None)
    return ok_response({**retrieval, "session_id": row.id})



def _ai_retrieval_summary(retrieval: dict | None) -> dict | None:
    """Table/column NAMES only -- never retrieved context text, prompts or data rows."""
    if not isinstance(retrieval, dict):
        return None
    cols = retrieval.get("relevant_columns_by_table") or {}
    return {
        "tables": [str(t)[:128] for t in (retrieval.get("relevant_tables") or [])[:20]],
        "columns": {str(t)[:128]: [str(c.get("column"))[:128] for c in (cs or [])[:30]] for t, cs in list(cols.items())[:20]},
    }


def _split_provider(provider_used: str | None) -> tuple[str | None, str | None]:
    if not provider_used:
        return None, None
    name, _, model = provider_used.partition(":")
    return name[:32], (model or None)


def _log_ai_invocation(db, request, user_id, session_id, result, retrieval, *, success, error_type=None, cost_cents=0.0):
    validation = result.get("validation") if isinstance(result, dict) else None
    if not success:
        validation_status = None
    elif isinstance(validation, dict):
        validation_status = "valid" if validation.get("is_valid") else "invalid"
    else:
        validation_status = "unknown"
    provider, model = _split_provider(result.get("provider_used"))
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    record_ai_invocation(
        db,
        request_id=getattr(request.state, "request_id", None),
        user_id=user_id, session_id=session_id,
        provider=provider, model=model,
        prompt_version=result.get("prompt_version"),
        input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
        usage_estimated=bool(usage.get("estimated", False)) if usage else False,
        estimated_cost_cents=float(cost_cents or 0.0),
        latency_ms=result.get("latency_ms"),
        retrieval_summary=_ai_retrieval_summary(retrieval),
        repair_attempts=int(result.get("repair_attempts") or 0),
        fallback_used=bool(result.get("fallback_used", False)),
        validation_status=validation_status,
        success=bool(success), error_type=error_type,
    )



# ---------------------------------------------------------------------------
# AI budget reservation: reserve (user + global) BEFORE the provider call, settle to real cost AFTER, and
# roll everything back if anything in between fails. There is no unmetered path:
#   1. Redis available -> atomic counters (global then user). Any partial step is compensated.
#   2. Redis absent / down / erroring -> the same reservation in the DATABASE ledger via an atomic conditional
#      UPDATE (db.reserve_daily_budget). The DB ledger always holds real spend, so a Redis outage never resets
#      or bypasses the budget.
#   3. Neither can reserve (database error) -> the exception propagates BEFORE any provider call: fail closed.
# Lost replies are treated as "unknown": we never refund a step we cannot prove was applied, so the worst case
# is a counter that is HIGHER than true spend until it expires -- it blocks early, never late.
# ---------------------------------------------------------------------------
class _BudgetHold:
    __slots__ = ("mode", "reserved", "global_held")

    def __init__(self, mode: str, reserved: float, global_held: bool = False):
        self.mode, self.reserved, self.global_held = mode, reserved, global_held


def _redis_reserve_pair(user_id: str, est: float, user_limit_cents: float):
    """Returns ("ok", hold) | ("user_exceeded", None) | ("global_exceeded", None). Raises RedisError only when
    NOTHING remains reserved (partial global reservation is rolled back first)."""
    global_held = False
    try:
        if _global_cap_enabled():
            if not _reserve_global_atomic(est):
                return "global_exceeded", None
            global_held = True
        if not _reserve_budget_atomic(user_id, est, user_limit_cents):
            if global_held:
                _best_effort_incr(_global_budget_key(), -est)
            return "user_exceeded", None
        return "ok", _BudgetHold("redis", est, global_held)
    except _redis_lib.exceptions.RedisError:
        if global_held:
            _best_effort_incr(_global_budget_key(), -est)
        raise


def _db_reserve_pair(db: OrmSession, user_id: str, est: float, user_limit_cents: float):
    if not reserve_daily_budget(db, user_id, est, user_limit_cents):
        return "user_exceeded", None
    hold = _BudgetHold("db", est)
    # Reserve-then-verify: our reservation is already committed, so any concurrent request sees it; if the
    # all-users total now exceeds the cap we back out. Concurrent requests can only be over-conservative.
    if _global_cap_enabled() and global_spend_cents_db(db) > GLOBAL_DAILY_AI_BUDGET_CENTS:
        settle_daily_budget(db, user_id, est, 0.0, count_request=False)
        return "global_exceeded", None
    return "ok", hold


def _reserve_ai_budget(db: OrmSession, user_id: str, est: float, user_limit_cents: float):
    if _redis_budget_available():
        try:
            outcome = _redis_reserve_pair(user_id, est, user_limit_cents)
            _mark_redis_budget_up()
            return outcome
        except _redis_lib.exceptions.RedisError:
            _mark_redis_budget_down()
    return _db_reserve_pair(db, user_id, est, user_limit_cents)


def _settle_ai_budget(db: OrmSession, user_id: str, hold: "_BudgetHold", actual_cents: float, *, count_request: bool):
    """Replace the reservation with the real cost. Returns today's UsageDaily row (or None when nothing needed
    recording). Never raises for Redis problems."""
    if hold.mode == "redis":
        for key, held in ((_budget_key(user_id), True), (_global_budget_key(), hold.global_held)):
            if not held:
                continue
            diff = actual_cents - hold.reserved
            if diff and not _best_effort_incr(key, diff):
                _mark_redis_budget_down()
        if count_request or actual_cents > 0:
            return record_llm_usage(db, user_id, actual_cents)
        return None
    return settle_daily_budget(db, user_id, hold.reserved, actual_cents, count_request=count_request or actual_cents > 0)

def _settle_ai_budget_safely(db: OrmSession, user_id: str, hold: "_BudgetHold", actual_cents: float, *, count_request: bool):
    """_settle_ai_budget that can never turn an already-paid-for provider answer into a 500. On failure the reservation
    stays held (counter over-states spend until expiry/next settle: fail-safe) and the problem is logged + counted."""
    try:
        return _settle_ai_budget(db, user_id, hold, actual_cents, count_request=count_request)
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        obs_errors.capture_exception(exc, component="ai_budget_settle")
        obs_events.record_event("ai_budget_settle_failed", severity="warning", component="ai",
                                detail={"component": "ai_budget", "state": hold.mode})
        return None


@app.post("/api/generate-sql")
@limiter.limit("30/minute")
def api_generate_sql(
    request: Request,
    payload: GenerateSqlRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    # Emergency stop: no provider call, no spend, no session work. Read at call time so ops can flip it.
    if _config_module.AI_KILL_SWITCH:
        obs_metrics.record_ai_budget_denied("kill_switch")
        return err_response(
            "AI features are temporarily disabled. Please try again later.",
            code=503, error_code="AI_DISABLED", headers={"Retry-After": "300"},
        )
    body = payload.model_dump(exclude_none=True) if payload else {}
    row = get_or_create_session(db, user_id=current_user.id, session_id=body.get("session_id"))
    question = (body.get("question") or "").strip()
    # GAP FIXED: previously any request body could supply "api_key" and it
    # would be used as-is. Fine for local dev/testing different keys, but in
    # a public deployment that lets any authenticated caller route arbitrary
    # third-party API keys through your server's LLM-calling code -- not a
    # capability a public demo should expose. Server-configured keys only
    # once ENVIRONMENT=production; body-supplied keys still work in dev.
    if IS_PRODUCTION:
        api_key = GROQ_API_KEY.strip()
    else:
        api_key = (body.get("api_key") or GROQ_API_KEY).strip()
    if not question:
        return err_response("Business question is required")
    if len(question) > MAX_QUESTION_LENGTH:
        return err_response(f"Question is too long (max {MAX_QUESTION_LENGTH} characters).", code=413)
    if not row.schema:
        return err_response("No database connected. Connect a database first.", error_code="NO_DATABASE")
    # Daily quota is checked BEFORE any provider call: an exhausted account must not spend LLM budget on SQL it can never
    # run. (The slot itself is still reserved atomically in /api/execute-sql.)
    _limit = daily_limit((current_user.plan or DEFAULT_PLAN).lower())
    if _limit is not None:
        _usage = get_or_create_daily_usage(db, current_user.id)
        if int(_usage.query_count or 0) >= _limit:
            return JSONResponse(status_code=429, content={"success": False, **_query_limit_payload(current_user, _usage)})
    if not api_key and not (OPENAI_API_KEY or ANTHROPIC_API_KEY):
        return err_response(
            # Production never accepts a browser-supplied key, so only point at Settings in development.
            "AI query generation is not configured on this server. Please try again later."
            if IS_PRODUCTION else
            "No LLM provider configured. Add your own API key in Settings, or set "
            "GROQ_API_KEY (or OPENAI_API_KEY / ANTHROPIC_API_KEY) in .env.",
            error_code="NO_LLM_PROVIDER",
        )

    semantic = row.semantic or build_semantic_layer(row.schema)
    # BUGFIX: this used to be `row.retrieval or retrieve_context(...)`, which
    # reused whatever retrieval was last saved on the session -- computed for
    # a PREVIOUS question (or, after a re-upload, a previous dataset). SQL was
    # then generated from the wrong tables/columns. Retrieval is cheap
    # (in-process TF-IDF), so always compute it for the question being asked.
    retrieval = retrieve_context(question, semantic, cache_key=row.id)

    # Per-user cap = base daily budget x the plan's multiplier (derived from entitlements in config.py).
    user_limit_cents = DAILY_LLM_BUDGET_CENTS * plan_budget_multiplier(current_user.plan)
    user_limit_usd = user_limit_cents / 100
    try:
        outcome, hold = _reserve_ai_budget(db, current_user.id, _estimate_cost_cents_pre_call(question, retrieval), user_limit_cents)
    except Exception:
        # Neither Redis nor the DB ledger could reserve: FAIL CLOSED. The exception propagates (existing, tested behaviour:
        # a 500 through the unhandled-error handler, which fingerprints it) BEFORE any provider call, with nothing held.
        obs_metrics.record_ai_budget_denied("unavailable")
        raise
    if outcome == "global_exceeded":
        obs_metrics.record_ai_budget_denied("global")
        return _global_budget_response()
    if outcome == "user_exceeded":
        obs_metrics.record_ai_budget_denied("user")
        return err_response(
            f"Daily AI usage budget of ${user_limit_usd:.2f} reached for your account. "
            "This resets at midnight UTC.",
            code=429,
            error_code="BUDGET_EXCEEDED",
        )

    # Blocking network I/O (LLM providers) -- must not run on the event loop.
    try:
        # Local path of the dataset so grounding/verification can read REAL values (read-only). Best effort: without it the
        # generator still works, it just cannot ground values or verify results.
        try:
            _gen_db_path = storage.ensure_local(row.db_path, current_user.id) if row.db_path else None
        except Exception:  # noqa: BLE001
            _gen_db_path = None
        _gen_kwargs = {"db_path": _gen_db_path} if _gen_db_path else {}
        result = generate_sql(question=question, schema=row.schema, api_key=api_key, semantic=semantic, retrieval=retrieval, **_gen_kwargs)
    except BaseException:
        # Unexpected crash between reserve and settle: give the whole reservation back, then re-raise.
        try:
            _settle_ai_budget(db, current_user.id, hold, 0.0, count_request=False)
        except Exception:  # noqa: BLE001
            logger.exception("could not roll back AI budget reservation")
        raise
    if not result.get("success", True) and result.get("error"):
        failed_usage = result.get("usage") if isinstance(result.get("usage"), dict) else None
        # Tokens can be spent even when generation fails (e.g. a billed reply that was not valid JSON).
        spent_cents = float(failed_usage.get("cost_cents") or 0.0) if failed_usage else 0.0
        # Refund the reservation down to what was really spent (0 when every provider errored). Best effort: if the refund
        # itself fails the counter stays HIGHER than true spend (fail-safe -- blocks early, never late) and we still answer.
        _settle_ai_budget_safely(db, current_user.id, hold, spent_cents, count_request=False)
        obs_metrics.record_ai_call(None, success=False, latency_ms=result.get("latency_ms"),
                                   input_tokens=int((failed_usage or {}).get("input_tokens") or 0),
                                   output_tokens=int((failed_usage or {}).get("output_tokens") or 0), cost_cents=spent_cents)
        # Provider SDK errors can embed request details (masked key fragments,
        # model/base URLs). Keep them in server logs; show the client a generic
        # message plus the request id it can quote.
        logger.warning("SQL generation failed [request_id=%s]: %s",
                       getattr(request.state, "request_id", None), result["error"])
        _log_ai_invocation(db, request, current_user.id, row.id, result, retrieval,
                           success=False, error_type=result.get("error_type") or "provider_error", cost_cents=spent_cents)
        return err_response(
            "SQL generation failed: every configured AI provider was unavailable or returned an error. "
            "Please try again in a moment.",
            code=502,
            error_code="GENERATION_FAILED",
        )

    cost_cents = _estimate_cost_cents(question, retrieval, result)
    usage_today = _settle_ai_budget_safely(db, current_user.id, hold, cost_cents, count_request=True)
    _ai_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    obs_metrics.record_ai_call(
        (result.get("provider_used") or "unknown").partition(":")[0], success=True, latency_ms=result.get("latency_ms"),
        input_tokens=int(_ai_usage.get("input_tokens") or 0), output_tokens=int(_ai_usage.get("output_tokens") or 0),
        cost_cents=cost_cents, fallback=bool(result.get("fallback_used")))
    _log_ai_invocation(db, request, current_user.id, row.id, result, retrieval, success=True, cost_cents=cost_cents)

    row = update_session(
        db, row,
        semantic=semantic, retrieval=retrieval,
        sql=result.get("sql"), sql_result=result, question=question,
        validation=result.get("validation") if result.get("validation") else None,
        query_result=None, reliability=None, insights=None,
    )

    tables_used = [t for t in row.schema.get("tables", {}) if t.upper() in (result.get("sql", "").upper())]
    return ok_response({
        "session_id": row.id,
        "sql": result.get("sql"),
        "explanation": result.get("explanation"),
        "confidence": result.get("confidence"),
        "complexity": result.get("complexity"),
        "tables_used": tables_used,
        "retrieval_confidence": retrieval.get("confidence") if isinstance(retrieval, dict) else None,
        "provider_used": result.get("provider_used"),
        "plan": result.get("plan"),
        "semantic_validation": ({"status": (result.get("semantic_validation") or {}).get("status"),
                                 "issues": (result.get("semantic_validation") or {}).get("issues", [])}
                                if result.get("semantic_validation") else None),
        "verification": _public_verification(result.get("verification")),
        "grounded_values": result.get("grounded_values"),
        "usage_today": {
            "requests": usage_today.requests if usage_today is not None else None,
            "estimated_cost_usd": round(usage_today.estimated_cost_cents / 100, 4) if usage_today is not None else None,
            "budget_usd": user_limit_usd,
        },
    })


@app.get("/api/ai-usage")
def api_ai_usage(
    limit: int = 20,
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """Read-only, owner-scoped view of the caller's most recent AI invocations. Additive: the
    frontend does not call it. Contains no prompts, keys or data rows."""
    limit = max(1, min(int(limit), 100))
    rows = (
        db.query(AIInvocation)
        .filter(AIInvocation.user_id == current_user.id)
        .order_by(AIInvocation.created_at.desc())
        .limit(limit)
        .all()
    )
    return ok_response({"invocations": [
        {
            "id": r.id, "request_id": r.request_id, "session_id": r.session_id,
            "provider": r.provider, "model": r.model, "prompt_version": r.prompt_version,
            "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
            "usage_estimated": bool(r.usage_estimated),
            "estimated_cost_cents": r.estimated_cost_cents, "latency_ms": r.latency_ms,
            "retrieval_summary": r.retrieval_summary, "repair_attempts": r.repair_attempts,
            "fallback_used": bool(r.fallback_used), "validation_status": r.validation_status,
            "success": bool(r.success), "error_type": r.error_type,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows
    ]})


def _record_history(db, user, row, sql, status, exec_result, error=None, question=None):
    import history_service
    return history_service.record(db, user, row, sql, status, exec_result, error=error, question=question)


@app.get("/api/history")
def api_history_list(
    q: str = "", limit: int = 50, offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """The caller's own query history, newest first. ``q`` searches question and SQL text."""
    import history_service
    return ok_response(history_service.list_history(db, current_user.id, q=q, limit=limit, offset=offset))


@app.get("/api/history/{entry_id}")
def api_history_get(
    entry_id: str,
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """One history entry with its saved result. Viewing never executes anything."""
    import history_service
    entry = history_service.get_entry(db, current_user.id, entry_id)
    if entry is None:
        return err_response("History entry not found.", code=404, error_code="HISTORY_NOT_FOUND")
    return ok_response({**entry, "saved_result": True})


@app.delete("/api/history/{entry_id}")
def api_history_delete(
    entry_id: str,
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    import history_service
    if not history_service.delete_entry(db, current_user.id, entry_id):
        return err_response("History entry not found.", code=404, error_code="HISTORY_NOT_FOUND")
    return ok_response({"deleted": True, "id": entry_id})


@app.post("/api/history/clear")
def api_history_clear(
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    import history_service
    return ok_response({"deleted": history_service.clear_all(db, current_user.id)})


def _public_verification(v):
    if not v:
        return None
    return {"status": v.get("status"), "verified": bool(v.get("verified")), "checks": v.get("checks", []),
            "diagnosis": v.get("diagnosis"), "reference_sql": v.get("reference_sql")}


@app.post("/api/validate-sql")
@limiter.limit("60/minute")
def api_validate_sql(
    request: Request,
    payload: ValidateSqlRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    body = payload.model_dump(exclude_none=True) if payload else {}
    row = get_or_create_session(db, user_id=current_user.id, session_id=body.get("session_id"))
    sql = (body.get("sql") or row.sql or "").strip()
    if not sql:
        return err_response("No SQL to validate")
    if len(sql) > MAX_SQL_LENGTH:
        return err_response(f"SQL is too long (max {MAX_SQL_LENGTH} characters).", code=413)
    result = validate_sql(sql, row.schema or {}, row.semantic)
    row = update_session(db, row, validation=result, sql=sql)
    return ok_response({**result, "session_id": row.id})


def _query_limit_payload(user: User, usage_row) -> dict:
    plan_key = (user.plan or DEFAULT_PLAN).lower()
    plan = get_plan(plan_key)
    limit = plan["daily_query_limit"]
    used = int(usage_row.query_count or 0)
    return {
        "code": "QUERY_LIMIT_REACHED",
        "plan": plan_key,
        "daily_limit": limit,
        "used_today": used,
        "remaining": max(0, limit - used) if limit is not None else None,
    }


@app.post("/api/execute-sql")
@limiter.limit("30/minute")
def api_execute_sql(
    request: Request,
    payload: ExecuteSqlRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    body = payload.model_dump(exclude_none=True) if payload else {}
    row = get_or_create_session(db, user_id=current_user.id, session_id=body.get("session_id"))
    sql = (body.get("sql") or row.sql or "").strip()
    if not row.db_path:
        return err_response("No database connected", error_code="NO_DATABASE")
    if not sql:
        return err_response("No SQL to execute")
    if len(sql) > MAX_SQL_LENGTH:
        return err_response(f"SQL is too long (max {MAX_SQL_LENGTH} characters).", code=413)

    # BUGFIX (P0): the previous check `if row.validation and not row.validation.get(...)`
    # only rejected SQL when a PRIOR validation record existed and had failed. If a
    # caller hits this endpoint directly -- skipping /api/validate-sql entirely, which
    # any authenticated REST client can do regardless of what the frontend UI does --
    # row.validation is None, the condition short-circuits to False, and unvalidated
    # SQL executes. The execution endpoint must never trust client-side sequencing;
    # it re-validates the exact SQL it's about to run, every time.
    validation = validate_sql(sql, row.schema or {}, row.semantic)
    row = update_session(db, row, validation=validation, sql=sql)
    if not validation.get("is_valid", False):
        return err_response("SQL validation failed.", code=422)

    try:
        local_db_path = storage.ensure_local(row.db_path, current_user.id)
    except storage.DatasetUnavailable:
        return err_response(
            "Your uploaded dataset is no longer available on the server. Please upload it again.",
            code=410,
            error_code="DATASET_UNAVAILABLE",
        )
    except storage.StorageError:
        return err_response(
            "Dataset storage is temporarily unavailable. Please try again in a moment.",
            code=503,
            error_code="STORAGE_UNAVAILABLE",
        )

    plan_key = (current_user.plan or DEFAULT_PLAN).lower()
    limit = daily_limit(plan_key)
    usage_before = get_or_create_daily_usage(db, current_user.id)
    if limit is not None and usage_before.query_count >= limit:
        payload = _query_limit_payload(current_user, usage_before)
        return JSONResponse(status_code=429, content={"success": False, **payload})

    usage_after = reserve_query_slot(db, current_user.id, limit)
    if usage_after is None:
        usage_now = get_or_create_daily_usage(db, current_user.id)
        payload = _query_limit_payload(current_user, usage_now)
        return JSONResponse(status_code=429, content={"success": False, **payload})
    obs_metrics.record_query_slot("reserved")

    # The slot is only kept if the caller actually receives a result. Every other exit (executor crash, failed query,
    # session write conflict, client cancellation) refunds it in the finally block.
    slot_consumed = False
    try:
        exec_result = execute_query(sql, local_db_path, parameterize=True)
        if not exec_result.get("success", False):
            _record_history(db, current_user, row, sql, "failed", None, error=exec_result.get("error"), question=(body.get("question") or None))
            return err_response(exec_result.get("error", "Query execution failed"))
        # Honest, separate assessment of the answer: semantic fit + independent recomputation (never inferred from
        # "it ran" or from the validation score).
        semantic_res = verification = None
        # A rerun from history supplies the original question; otherwise the session's last question applies.
        _q_for_check = (body.get("question") or row.question or "").strip()[:MAX_QUESTION_LENGTH] or None
        if _q_for_check:
            try:
                from modules.result_verifier import semantic_validate, verify_result, correctness_dimensions
                semantic_res = semantic_validate(_q_for_check, sql, row.schema or {}, local_db_path)
                verification = verify_result(_q_for_check, sql, row.schema or {}, exec_result, local_db_path,
                                             plan=semantic_res.get("plan"))
            except Exception:  # noqa: BLE001 - verification is an aid; it must never break execution
                logger.exception("result verification failed")
        exec_result["semantic_validation"] = ({"status": semantic_res["status"], "issues": semantic_res["issues"]}
                                              if semantic_res else None)
        exec_result["verification"] = _public_verification(verification)
        try:
            from modules.result_verifier import correctness_dimensions
            exec_result["correctness"] = correctness_dimensions(validation, semantic_res, exec_result, verification)
        except Exception:  # noqa: BLE001
            pass
        hist_id = _record_history(db, current_user, row, sql, "success" if exec_result.get("status") == "success" else exec_result.get("status", "success"), exec_result, question=(body.get("question") or None))
        if hist_id:
            exec_result["history_id"] = hist_id
        row = update_session(db, row, query_result=exec_result, sql=sql)
        slot_consumed = True
        return ok_response({**exec_result, "session_id": row.id})
    finally:
        if not slot_consumed:
            _refund_query_slot_safely(db, current_user.id)


def _refund_query_slot_safely(db: OrmSession, user_id: str) -> None:
    """Refund one daily query slot. Never raises (it runs in ``finally`` and must not mask the original error); a failed
    refund is logged, counted and recorded as an ops event so it can be reconciled."""
    try:
        db.rollback()  # the session may be mid-failure; the refund needs a clean transaction
        refunded = refund_query_slot(db, user_id)
        obs_metrics.record_query_slot("refunded" if refunded else "refund_noop")
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        obs_metrics.record_query_slot("refund_failed")
        obs_errors.capture_exception(exc, component="query_slot_refund")
        obs_events.record_event("query_slot_refund_failed", severity="warning", component="database",
                                detail={"component": "query_slots"})


@app.post("/api/analyze-reliability")
@limiter.limit("30/minute")
def api_analyze_reliability(
    request: Request,
    payload: ReliabilityRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    body = payload.model_dump(exclude_none=True) if payload else {}
    row = get_or_create_session(db, user_id=current_user.id, session_id=body.get("session_id"))
    if not row.validation or not row.query_result:
        return err_response("Validation and query result are required")
    result = assess_reliability(schema=row.schema, validation_result=row.validation, query_result=row.query_result)
    row = update_session(db, row, reliability=result)
    try:
        import history_service
        history_service.attach_reliability(db, current_user.id, (row.query_result or {}).get("history_id"), result)
    except Exception:  # noqa: BLE001
        pass
    return ok_response({**result, "session_id": row.id})


@app.post("/api/generate-insights")
@limiter.limit("30/minute")
def api_generate_insights(
    request: Request,
    payload: InsightsRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    body = payload.model_dump(exclude_none=True) if payload else {}
    row = get_or_create_session(db, user_id=current_user.id, session_id=body.get("session_id"))
    if not row.query_result:
        return err_response("No query results provided")
    try:   # lets a LIMITed result state shares against the full result instead of the returned rows
        insights_db = storage.ensure_local(row.db_path, current_user.id) if row.db_path else None
    except Exception:  # noqa: BLE001 - without it, shares of a LIMITed result are simply not stated
        insights_db = None
    result = generate_insights(
        schema=row.schema or {},
        query_result=row.query_result,
        reliability_result=row.reliability,
        semantic=row.semantic,
        db_path=insights_db,
    )
    row = update_session(db, row, insights=result)
    return ok_response({**result, "session_id": row.id})


@app.get("/api/answer-trace")
def api_answer_trace(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """Return sanitized provenance from the authenticated session only."""
    session_id = request.query_params.get("session_id")
    row = get_or_create_session(db, user_id=current_user.id, session_id=session_id)
    trace = build_trust_trace(row)
    return ok_response({"session_id": row.id, "trace": trace})


@app.post("/api/generate-report")
@limiter.limit("10/minute")
def api_generate_report(
    request: Request,
    payload: ReportRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    body = payload.model_dump(exclude_none=True) if payload else {}
    row = get_or_create_session(db, user_id=current_user.id, session_id=body.get("session_id"))
    if not (row.question and row.sql_result and row.validation and row.query_result and row.reliability and row.insights):
        return err_response("Run the full pipeline before generating a report")
    job = enqueue_job(db, current_user.id, "report_generation", {"session_id": row.id})
    return JSONResponse(status_code=202, content={"success": True, "job_id": job.id, "session_id": row.id})


@app.get("/api/reports/{report_id}/download")
def download_report(
    report_id: str,
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """Owner-only download of a stored report artifact (streamed through the API; the object key and storage
    layout are never exposed). Another user's report id is indistinguishable from a missing one: 404."""
    import report_storage
    row = db.query(Report).filter(Report.id == report_id, Report.user_id == current_user.id).first()
    if row is None:
        return err_response("Report not found.", code=404, error_code="REPORT_NOT_FOUND")
    try:
        chunks, length = report_storage.open_report(row.key, current_user.id)
    except storage.DatasetUnavailable:
        return err_response("This report is no longer available.", code=404, error_code="REPORT_UNAVAILABLE")
    content_type, filename = report_storage.KINDS.get(row.kind, ("application/octet-stream", "report.bin"))
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Cache-Control": "private, no-store",
    }
    if length is not None:
        headers["Content-Length"] = str(length)
    return StreamingResponse(chunks, media_type=content_type, headers=headers)


@app.get("/api/provider-status")
def provider_status(current_user: User = Depends(get_current_user)):
    """Tells the frontend whether an LLM provider is already configured
    server-side, and whether this deployment permits a caller to supply
    their own key from the UI. Added so the Settings panel can stop
    advertising a "paste your API key" field on a production deployment
    where /api/generate-sql silently ignores body-supplied keys anyway
    (see the ENVIRONMENT == "production" check there) -- without this, the
    UI could accept and store a key that the backend would never use."""
    server_key_configured = bool(GROQ_API_KEY or OPENAI_API_KEY or ANTHROPIC_API_KEY)
    allow_client_api_key = not IS_PRODUCTION
    from modules.sql_generator import describe_provider_chain
    chain = describe_provider_chain(GROQ_API_KEY)
    return ok_response({
        "llm_configured": server_key_configured,
        "allow_client_api_key": allow_client_api_key,
        # Names only (no models, keys, health or error detail): enough for the UI, nothing for an attacker.
        "providers_configured": sorted({c["provider"] for c in chain}),
        "fallback_available": len(chain) > 1,
    })


@app.get("/api/billing")
def api_billing(
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """Return the authenticated user's read-only billing state.

    This endpoint has no mutation path and never uses billing state for
    entitlement enforcement. Effective product entitlements remain owned by
    User.plan and entitlements.py.
    """
    state = get_billing_state(db, current_user.id)
    return ok_response({
        "status": state["status"],
        "plan": state["plan"],
        "provider": state["provider"],
        "provider_customer_id": state["provider_customer_id"],
        "provider_subscription_id": state["provider_subscription_id"],
        "current_period_start": state["current_period_start"].isoformat() if state["current_period_start"] else None,
        "current_period_end": state["current_period_end"].isoformat() if state["current_period_end"] else None,
        "cancel_at_period_end": state["cancel_at_period_end"],
        "billing_enabled": payment_provider.billing_enabled(),
        "purchasable_plans": sorted(PURCHASABLE_PLANS),
        "portal_url": _config_module.PAYMENT_PORTAL_URL or None,
    })


class CheckoutRequest(BaseModel):
    plan: str


@app.post("/api/billing/checkout")
@limiter.limit("10/minute")
def api_billing_checkout(
    request: Request,
    payload: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """Start a hosted checkout. This NEVER changes the plan: the plan changes only when the provider's signed webhook says
    payment succeeded. The browser supplies nothing but the plan it wants to buy."""
    try:
        provider = payment_provider.get_payment_provider()
    except payment_provider.ProviderNotConfigured:
        provider = None
    if provider is None:
        return err_response("Payments are not enabled on this deployment.", code=503, error_code="BILLING_NOT_CONFIGURED")
    try:
        result = create_checkout(
            db, current_user, payload.plan, provider,
            return_url=f"{_config_module.APP_BASE_URL}/?billing=return",
        )
    except CheckoutError as exc:
        return err_response(exc.message, code=exc.status_code, error_code=exc.code)
    return ok_response(result)


@app.post("/api/billing/webhook/{provider_name}", include_in_schema=False)
@limiter.limit("300/minute")
async def api_billing_webhook(provider_name: str, request: Request, db: OrmSession = Depends(get_db)):
    """Provider -> QuerySure. Authenticated ONLY by the provider's signature over the raw body (no bearer token, no cookies,
    so there is nothing for CSRF to ride on). Answers 2xx once an event is durably handled or safely ignorable, 5xx when the
    provider should retry. Bodies, signatures and secrets are never logged."""
    try:
        provider = payment_provider.get_payment_provider()
    except payment_provider.ProviderNotConfigured:
        provider = None
    if provider is None or provider.provider_name != provider_name.lower():
        return JSONResponse({"error": "not found"}, status_code=404)
    limit = _config_module.BILLING_WEBHOOK_MAX_BYTES
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return JSONResponse({"error": "payload too large"}, status_code=413)
    raw = bytes(body)
    try:
        event = provider.verify_event(raw, request.headers.get(provider.signature_header))
    except payment_provider.WebhookVerificationError:
        obs_metrics.record_error("billing", "bad_signature")
        return JSONResponse({"error": "invalid signature"}, status_code=400)
    except payment_provider.InvalidEventError:
        obs_metrics.record_error("billing", "invalid_event")
        return JSONResponse({"error": "invalid event"}, status_code=400)
    try:
        result = await run_in_threadpool(process_verified_event, db, event, payload_sha256=payload_digest(raw))
    except Exception as exc:  # noqa: BLE001 - ledger row is already marked failed; provider retries
        obs_metrics.record_error("billing", "webhook_processing_failed")
        logger.error("billing webhook processing failed", extra={"event": "billing_webhook_failed", "exc_type": type(exc).__name__})
        return JSONResponse({"error": "processing failed"}, status_code=500)
    logger.info("billing webhook handled", extra={"event": "billing_webhook", "outcome": result.outcome})
    if result.outcome == "deferred":
        return JSONResponse({"received": True, "outcome": "deferred"}, status_code=503, headers={"Retry-After": "300"})
    return {"received": True, "outcome": result.outcome}


@app.get("/api/account/export")
@limiter.limit("3/hour")
def api_account_export(request: Request, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
    """Download a JSON copy of the caller's own data (no credentials, storage keys or other users' data)."""
    data = account_service.export_account(db, current_user)
    return JSONResponse(data, headers={
        "Content-Disposition": 'attachment; filename="querysure-account-export.json"',
        "Cache-Control": "private, no-store",
    })


class DeleteAccountRequest(BaseModel):
    confirm_email: str
    password: str | None = None
    google_credential: str | None = None


@app.post("/api/account/delete")
@limiter.limit("5/hour")
def api_account_delete(
    request: Request,
    payload: DeleteAccountRequest,
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    """Permanently delete the account. Requires re-authentication (password, or a fresh Google credential for accounts
    that have no password) and the account e-mail typed back, so a stolen session alone cannot erase an account."""
    if normalize_email(payload.confirm_email) != normalize_email(current_user.email):
        return err_response("The e-mail you typed does not match this account.", code=422, error_code="CONFIRMATION_MISMATCH")
    if is_google_only_account(current_user):
        try:
            claims = verify_google_id_token((payload.google_credential or "").strip())
        except Exception:
            return err_response("Re-authentication with Google failed.", code=401, error_code="REAUTH_FAILED")
        if str(claims.get("sub")) != str(current_user.google_subject):
            return err_response("Re-authentication with Google failed.", code=401, error_code="REAUTH_FAILED")
    elif not payload.password or not verify_password(payload.password, current_user.hashed_password):
        return err_response("Password is incorrect.", code=401, error_code="REAUTH_FAILED")
    try:
        account_service.delete_account(db, current_user)
    except account_service.AccountError as exc:
        return err_response(exc.message, code=exc.status_code, error_code=exc.code)
    return ok_response({"deleted": True})


@app.get("/api/account/usage")
def api_account_usage(
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    row = get_or_create_daily_usage(db, current_user.id)
    plan_key = (current_user.plan or DEFAULT_PLAN).lower()
    plan = get_plan(plan_key)
    limit = plan["daily_query_limit"]
    used = int(row.query_count or 0)
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return ok_response({
        "plan": plan_key,
        "plan_name": plan["name"],
        "price_monthly_usd": plan["price_monthly_usd"],
        "daily_limit": limit,
        "used_today": used,
        "remaining_today": max(0, limit - used) if limit is not None else None,
        "reset_at": tomorrow.isoformat(),
    })


@app.get("/api/usage")
def api_usage(
    current_user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
):
    row = get_or_create_daily_usage(db, current_user.id)
    budget_usd = DAILY_LLM_BUDGET_USD * plan_budget_multiplier(current_user.plan)
    return ok_response({
        "day": row.day,
        "requests": row.requests,
        "estimated_cost_usd": round(row.estimated_cost_cents / 100, 4),
        "budget_usd": budget_usd,
        "remaining_usd": max(0.0, round(budget_usd - row.estimated_cost_cents / 100, 4)),
    })


@app.get("/api/product-info")
def product_info():
    return ok_response({"contact_email": PRODUCT_CONTACT_EMAIL or None, "billing_enabled": payment_provider.billing_enabled()})


@app.get("/api/health")
def health():
    # CHANGED: this used to publicly return which LLM providers were
    # configured ("groq": true/false, etc.) and a hardcoded, always-true
    # "modules" block that claimed to check things it never actually
    # checked. Neither is useful to a caller and the provider info is
    # information an unauthenticated endpoint shouldn't disclose about your
    # infrastructure. This is now a pure liveness check: "is the process
    # up." Use /api/ready (below) for an authenticated check that your
    # actual dependencies (DB) are reachable.
    return ok_response({"status": "ok"})


def _redis_ready() -> bool:
    """True if Redis is reachable. False if it's required and can't be
    reached, or configured-but-unreachable in any environment.

    Redis is "required" here if either: ENVIRONMENT=production (config.py
    already refuses to even start in production without REDIS_URL set, so
    this is belt-and-suspenders making that requirement explicit at the
    readiness layer too, not just at import time), or REDIS_URL happens to
    be configured in a non-production environment (if you bothered to set
    it, a broken connection to it is worth surfacing).

    Uses a fresh short-timeout connection rather than trusting the
    module-level `redis_client` as-is: that value is fixed at startup, so
    if Redis was down at boot and has since recovered (or vice versa),
    readiness should reflect the current state, not the state 10 minutes
    ago.
    """
    redis_required = IS_PRODUCTION or bool(REDIS_URL)
    if not redis_required or not _redis_lib:
        return True
    if not REDIS_URL:
        # IS_PRODUCTION with no REDIS_URL would already have failed at
        # startup via config.py -- reachable here only if that guard was
        # somehow bypassed. Treat as not ready rather than silently passing.
        return False
    try:
        client = redis_client or _redis_lib.from_url(REDIS_URL, socket_connect_timeout=2)
        client.ping()
        return True
    except Exception:
        return False


@app.get("/api/ready")
def ready():
    """Readiness: CRITICAL dependencies (database, Redis) decide 200 vs 503; non-critical ones (worker, queue, object storage,
    AI providers/spend cap, DB pool pressure, metrics store) report ``status: "degraded"`` with HTTP 200 so the platform keeps
    routing traffic while alerts fire. See observability/health.py for the rationale.

    The probe uses its own short-lived session (not the request dependency) so it can still ANSWER 503 with a JSON body when
    the pool is exhausted. Public and minimal: component names only -- no hosts, versions, counts or error text.
    Sprint 1 behaviour is kept: a configured-but-unreachable Redis (or a failing database) is HTTP 503.
    """
    db_ok, _latency = obs_health.check_database()
    doc = obs_health.readiness(db_ok, _redis_ready())
    return JSONResponse(status_code=200 if doc["success"] else 503, content=doc)


from ops_api import router as _ops_router  # noqa: E402

app.include_router(_ops_router)


@app.get("/ops", include_in_schema=False)
def _ops_dashboard_page():
    """Static shell of the admin/ops dashboard. It contains no data; the page calls /api/admin/ops/* with the signed-in
    user's bearer token, and those endpoints enforce the ADMIN_EMAILS allow-list."""
    page = _html_page("ops.html")
    if page is not None:
        return page
    return JSONResponse({"message": "Ops dashboard is unavailable."}, status_code=503)


# Frontend SPA fallback for the existing standalone site views.
# Keep this allowlisted: API, static, assets, and other application routes
# remain handled by their existing route/mount definitions.
SITE_ROUTES = (
    "/product",
    "/how-it-works",
    "/evaluation",
    "/pricing",
    "/security",
    "/faq",
    "/documentation",
    "/contact",
    "/privacy",
    "/terms",
)


def _serve_site_index():
    page = _html_page("index.html")
    if page is not None:
        return page
    return JSONResponse(
        {"message": "QuerySure frontend is unavailable."},
        status_code=503,
    )


for _site_route in SITE_ROUTES:
    app.add_api_route(
        _site_route,
        _serve_site_index,
        methods=["GET"],
        include_in_schema=False,
    )


if __name__ == "__main__":
    # BUGFIX: hardcoded port=5000 with reload=True. Most PaaS platforms
    # (Render, Railway, etc.) inject the port to bind via $PORT and expect
    # the process to honor it -- hardcoding 5000 means the platform's health
    # check hits the wrong port and the deploy is reported as failed even
    # though the app is technically running. reload=True is also dev-only;
    # it should never be on in a production process. PORT/IS_PRODUCTION now
    # come from config.py.
    uvicorn.run("api:app", host="0.0.0.0", port=CONFIG_PORT, reload=not IS_PRODUCTION)