"""
Centralized environment configuration for QuerySure.

Sprint 1 blocker fix: environment variables were previously read via ad-hoc
os.environ.get(...) calls scattered across api.py and auth.py, each with
its own (sometimes inconsistent) default and no single place to see what
the app actually reads. This module is the one place that touches
os.environ for application config -- every other module imports the
constants from here instead of calling os.environ.get() itself.

If you add a new environment variable anywhere in the app: add it here,
add it to .env.example with the same name, and import the constant where
it's needed. Don't call os.environ.get() outside this file.

Import this module before anything that depends on it. It has no
dependency on api.py, auth.py, or db.py, so it can be imported first from
anywhere without circular-import risk.
"""

from __future__ import annotations

import logging
import os

# ---------------------------------------------------------------------------
# Runtime environment
# ---------------------------------------------------------------------------
def _env_number(name: str, default, cast=int):
    """Read a numeric env var. Unset *or blank* (e.g. a Render ``sync: false`` key that was
    never filled in) falls back to the default instead of crashing import with ValueError."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return cast(default)
    try:
        return cast(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc


ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
IS_PRODUCTION = ENVIRONMENT == "production"
PORT = int(os.environ.get("PORT") or "5000")
TRUSTED_PROXY_IPS = os.environ.get("TRUSTED_PROXY_IPS", "").strip()
WEB_CONCURRENCY = max(1, _env_number("WEB_CONCURRENCY", 2))
THREADPOOL_MAX_WORKERS = max(4, _env_number("THREADPOOL_MAX_WORKERS", 20))
DB_POOL_SIZE = max(1, _env_number("DB_POOL_SIZE", 5))
DB_MAX_OVERFLOW = max(0, _env_number("DB_MAX_OVERFLOW", 5))
DB_POOL_TIMEOUT = max(1, _env_number("DB_POOL_TIMEOUT", 10))
DB_POOL_RECYCLE = max(60, _env_number("DB_POOL_RECYCLE", 1800))
CONCURRENCY_GENERATE_SQL = max(1, _env_number("CONCURRENCY_GENERATE_SQL", 2))
CONCURRENCY_UPLOAD = max(1, _env_number("CONCURRENCY_UPLOAD", 1))
CONCURRENCY_EXECUTE_SQL = max(1, _env_number("CONCURRENCY_EXECUTE_SQL", 1))
MAX_INGEST_ROWS = max(1, _env_number("MAX_INGEST_ROWS", 1000000))
MAX_INGEST_COLUMNS = max(1, _env_number("MAX_INGEST_COLUMNS", 200))
MAX_INGEST_SHEETS = max(1, _env_number("MAX_INGEST_SHEETS", 20))
MAX_XLSX_UNCOMPRESSED_MB = max(1, _env_number("MAX_XLSX_UNCOMPRESSED_MB", 512))
# Largest accepted upload, any format (each format also has its own ceiling in api.py). The full upload job for a 10 MB file
# peaks at roughly 140-360 MB RSS in one process (CSV/JSON/XLSX; SQLite far less), which fits one web process on 512 MB.
MAX_UPLOAD_MB = max(1, _env_number("MAX_UPLOAD_MB", 10))
# Excel parsing is far slower than CSV (openpyxl XML parsing): on a 0.1-CPU instance a 1 MB workbook took ~6 minutes and a 2 MB
# one ~11 minutes. This lowers the Excel limit separately (the lower of the two applies); render.yaml sets 2 for the free plan.
MAX_UPLOAD_XLSX_MB = max(1, _env_number("MAX_UPLOAD_XLSX_MB", MAX_UPLOAD_MB))
MAX_XLSX_COMPRESSION_RATIO = max(1.0, _env_number("MAX_XLSX_COMPRESSION_RATIO", 100, float))
CSP_REPORT_ONLY = os.environ.get("CSP_REPORT_ONLY", "0").strip().lower() in {"1", "true", "yes", "on"}

# ---------------------------------------------------------------------------
# Auth / JWT
# ---------------------------------------------------------------------------
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY")
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", str(60 * 24)))

if not JWT_SECRET_KEY:
    if IS_PRODUCTION:
        raise RuntimeError(
            "JWT_SECRET_KEY must be set in production. Generate one with: "
            "python -c \"import secrets; print(secrets.token_urlsafe(64))\""
        )
    # Dev-only fallback so the app still boots locally without extra setup.
    # Every restart invalidates existing tokens -- that's intentional
    # friction to make it obvious this isn't a production configuration.
    JWT_SECRET_KEY = "dev-only-insecure-secret-do-not-use-in-production"
elif IS_PRODUCTION and len(JWT_SECRET_KEY) < 16:
    # HS256 is only as strong as its key; a trivial secret lets anyone mint tokens for any account.
    raise RuntimeError(
        "JWT_SECRET_KEY must be at least 16 characters in production (32+ recommended). Generate one with: "
        "python -c \"import secrets; print(secrets.token_urlsafe(64))\""
    )
elif IS_PRODUCTION and len(JWT_SECRET_KEY) < 32:
    import sys as _sys
    print("WARNING: JWT_SECRET_KEY is shorter than 32 characters; use secrets.token_urlsafe(64).", file=_sys.stderr)

# ---------------------------------------------------------------------------
# Authentication email delivery
# ---------------------------------------------------------------------------
EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "smtp").strip().lower()
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = _env_number("SMTP_PORT", 587)
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "").strip()
# HTTPS e-mail APIs (EMAIL_PROVIDER=brevo or resend): used where outbound SMTP ports are blocked (Render's free instances).
EMAIL_API_KEY = os.environ.get("EMAIL_API_KEY", "").strip()
# One bound for every e-mail network call (HTTPS request, or each SMTP step), so a dead mail server cannot hang a worker.
EMAIL_TIMEOUT_SECONDS = max(1.0, min(30.0, _env_number("EMAIL_TIMEOUT_SECONDS", 10, float)))
# How verification / reset e-mails leave: "queue" (background worker), "background" (sent by the web process right after
# the response, for deployments without a worker), or "auto" (queue when a worker is alive, otherwise background).
AUTH_EMAIL_DELIVERY = os.environ.get("AUTH_EMAIL_DELIVERY", "auto").strip().lower()
if AUTH_EMAIL_DELIVERY not in {"auto", "queue", "background"}:
    raise RuntimeError("AUTH_EMAIL_DELIVERY must be auto, queue or background.")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000").strip().rstrip("/")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
PRODUCT_CONTACT_EMAIL = os.environ.get("PRODUCT_CONTACT_EMAIL", "").strip()

if IS_PRODUCTION:
    if EMAIL_PROVIDER not in {"smtp", "brevo", "resend"}:
        raise RuntimeError("EMAIL_PROVIDER must be smtp, brevo or resend in production.")
    if EMAIL_PROVIDER == "smtp" and not all((SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM, APP_BASE_URL)):
        raise RuntimeError(
            "Production email authentication requires SMTP_HOST, SMTP_USERNAME, "
            "SMTP_PASSWORD, EMAIL_FROM, and APP_BASE_URL."
        )
    if EMAIL_PROVIDER in {"brevo", "resend"} and not all((EMAIL_API_KEY, EMAIL_FROM, APP_BASE_URL)):
        raise RuntimeError(f"EMAIL_PROVIDER={EMAIL_PROVIDER} requires EMAIL_API_KEY, EMAIL_FROM and APP_BASE_URL.")
    if not APP_BASE_URL.startswith("https://"):
        raise RuntimeError("Production APP_BASE_URL must use HTTPS for secure authentication links.")
    if not GOOGLE_CLIENT_ID:
        # Optional: without it the "Continue with Google" button is hidden and /api/auth/google returns 503
        # GOOGLE_NOT_CONFIGURED. Email + password sign-in (verified email required) is unaffected.
        import sys as _sys
        print("WARNING: GOOGLE_CLIENT_ID is not set; Google sign-in is disabled.", file=_sys.stderr)

# ---------------------------------------------------------------------------
# Database / cache
# ---------------------------------------------------------------------------
# Defaults to a local SQLite file so the app boots with zero config for
# local dev; production must set a real DATABASE_URL (Postgres) -- see the
# guard just below.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./querysure.db")
REDIS_URL = os.environ.get("REDIS_URL")

# Sprint 1 blocker fix (config centralization): this guard previously lived
# in db.py, reading os.environ directly there instead of going through this
# module -- the one place config.py exists to prevent. Moved here so
# DATABASE_URL's value and its production safety check live next to each
# other, same as the JWT_SECRET_KEY guard above. Same fail-fast philosophy:
# on most container hosts the SQLite file is wiped on every redeploy, which
# would otherwise silently delete every user and session with no error
# anywhere.
if IS_PRODUCTION and DATABASE_URL.startswith("sqlite"):
    raise RuntimeError(
        "Refusing to start in production against SQLite. Set DATABASE_URL to a "
        "real Postgres connection string (see render.yaml / .env.example) -- "
        "SQLite's local file is wiped on most container platforms' redeploys "
        "and restarts, which silently deletes every user and session."
    )

if IS_PRODUCTION and not TRUSTED_PROXY_IPS:
    raise RuntimeError(
        "TRUSTED_PROXY_IPS must be explicitly configured in production. "
        "Do not use *; set the proxy IP/CIDR values supplied by your deployment operator."
    )

if IS_PRODUCTION and not REDIS_URL:
    # Sprint 1 correction: this was previously a warning only. Escalated to
    # a hard fail-fast, matching the JWT_SECRET_KEY / DATABASE_URL guards
    # above -- a production deployment without Redis silently degrades rate
    # limiting and daily LLM-budget enforcement to per-process, non-atomic
    # in-memory storage, which is a real security/cost-control gap the
    # moment there's more than one worker or instance (Render's free web
    # service plan is single-instance today, but nothing in this code
    # should assume that stays true). Development keeps the fallback: a
    # single local `uvicorn` process is exactly the case where in-memory
    # limiting is correct, not a degraded mode.
    raise RuntimeError(
        "REDIS_URL must be set in production. Rate limiting and daily "
        "LLM-budget enforcement require a shared, atomic counter across "
        "processes/instances -- in-memory storage only works correctly "
        "for a single process. Set REDIS_URL (on Render it is wired from the "
        "querysure-redis Key Value instance in render.yaml) before starting in production."
    )

# ---------------------------------------------------------------------------
# Dataset storage (uploaded CSV/JSON/XLSX/XLSM/SQLite -> one SQLite file each)
# ---------------------------------------------------------------------------
# Production containers (Render, etc.) have an ephemeral filesystem: anything
# written to ./uploads is gone on the next restart/redeploy while the Postgres
# session row still points at it. So in production the durable copy lives in
# S3-compatible object storage (AWS S3, Cloudflare R2, Backblaze B2, MinIO...)
# and the local disk is only a re-downloadable cache.
#
#   STORAGE_BACKEND   "s3" or "local". If unset: "s3" when S3_BUCKET is set,
#                     otherwise "local" (development default).
#   S3_BUCKET         bucket name (required for the s3 backend)
#   S3_PREFIX         key prefix inside the bucket (default "datasets")
#   S3_REGION         optional region
#   S3_ENDPOINT_URL   optional, for non-AWS S3-compatible services
#   credentials       standard boto3 chain, e.g. AWS_ACCESS_KEY_ID /
#                     AWS_SECRET_ACCESS_KEY (never committed)
#   STORAGE_CACHE_MAX_MB  cap on the local download cache (default 2048)
S3_BUCKET = os.environ.get("S3_BUCKET", "").strip()
S3_PREFIX = os.environ.get("S3_PREFIX", "datasets").strip().strip("/") or "datasets"
S3_REGION = os.environ.get("S3_REGION", "").strip() or None
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "").strip() or None
STORAGE_CACHE_MAX_MB = _env_number("STORAGE_CACHE_MAX_MB", 2048)
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "").strip().lower() or ("s3" if S3_BUCKET else "local")

if STORAGE_BACKEND not in ("local", "s3"):
    raise RuntimeError(f"STORAGE_BACKEND must be 'local' or 's3', got {STORAGE_BACKEND!r}.")
if STORAGE_BACKEND == "s3" and not S3_BUCKET:
    raise RuntimeError("STORAGE_BACKEND=s3 requires S3_BUCKET to be set.")
if IS_PRODUCTION and STORAGE_BACKEND != "s3":
    raise RuntimeError(
        "Object storage must be configured in production. Set S3_BUCKET (and "
        "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, plus S3_ENDPOINT_URL for "
        "non-AWS providers) -- uploaded datasets stored on the container's "
        "local disk are lost on every restart/redeploy."
    )

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGINS", "*").split(",") if o.strip()
]

if IS_PRODUCTION and CORS_ALLOWED_ORIGINS == ["*"]:
    # Sprint 1 correction: this was previously a warning only, and "*" is
    # still the render.yaml default -- meaning a first prod deploy would
    # silently boot wide open with nothing but a log line most people never
    # read at deploy time. Escalated to fail-fast, same as the other
    # production guards in this file: explicit, one-time friction (set the
    # real origin(s) once) beats a silent, indefinite gap. Development
    # keeps "*" as the default so local work needs no extra config.
    raise RuntimeError(
        "CORS_ALLOWED_ORIGINS must be set to explicit origin(s) in "
        "production -- \"*\" (the default) is not allowed to start. Set it "
        "to your real frontend origin(s), comma-separated if more than "
        "one (e.g. https://app.example.com), in the Render dashboard or "
        "your production environment."
    )

# allow_origins=["*"] combined with allow_credentials=True is invalid per
# the CORS spec (browsers reject it). Auth here is a Bearer token in a
# header, not a cookie, so credentialed CORS isn't needed unless
# CORS_ALLOWED_ORIGINS is set to real, specific origin(s).
CORS_ALLOW_CREDENTIALS = "*" not in CORS_ALLOWED_ORIGINS

# ---------------------------------------------------------------------------
# LLM providers / daily budget
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DAILY_LLM_BUDGET_USD = float(os.environ.get("DAILY_LLM_BUDGET_USD", "2.00"))
# Kept as a float, not rounded to whole cents: a single request's estimated
# cost is often a small fraction of a cent, and rounding either the budget
# or each request's cost to whole-cent integers can collapse a legitimate
# small value to 0.
DAILY_LLM_BUDGET_CENTS = DAILY_LLM_BUDGET_USD * 100

# ---------------------------------------------------------------------------
# LLM model overrides (moved from sql_generator.py -- config centralization)
# ---------------------------------------------------------------------------
# Defaults updated Oct 2026: Groq shut down llama-3.3-70b-versatile and llama-3.1-8b-instant on 2026-08-16 and
# Anthropic retired claude-3-5-haiku-20241022 on 2026-02-19, so the old defaults made every generation fail.
# gpt-4o-mini is still served by OpenAI (snapshot shutdown scheduled for 2027-01-14). Model IDs drift: override
# any of these per deployment instead of editing code.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_FALLBACK_MODEL = os.environ.get("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")
OPENAI_FALLBACK_MODEL = os.environ.get("OPENAI_FALLBACK_MODEL", "gpt-4o-mini")
ANTHROPIC_FALLBACK_MODEL = os.environ.get("ANTHROPIC_FALLBACK_MODEL", "claude-haiku-4-5-20251001")
# Reasoning models (gpt-oss on Groq) spend part of max_tokens on hidden reasoning. "low" keeps that small so the JSON
# answer is not truncated; empty string = do not send the parameter.
GROQ_REASONING_EFFORT = os.environ.get("GROQ_REASONING_EFFORT", "low").strip().lower()
if GROQ_REASONING_EFFORT not in ("", "low", "medium", "high"):
    raise RuntimeError("GROQ_REASONING_EFFORT must be one of: low, medium, high, or empty")
LLM_MAX_OUTPUT_TOKENS = max(256, min(16000, int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "4000") or 4000)))
# Mean top-3 TF-IDF retrieval score below which generated SQL is labelled "Low" confidence (see sql_generator.py).
LOW_RETRIEVAL_CONFIDENCE = max(0.0, min(1.0, _env_number("LOW_RETRIEVAL_CONFIDENCE", 0.05, float)))

# Cross-provider fallback (v1.0.3). Order in which configured providers are tried; unknown names are ignored, providers
# without an API key are skipped. Model IDs above are NOT assumed to exist: an invalid/retired model returns a
# non-retryable "model_not_found"/"bad_request" and the chain moves on. Set a *_MODEL / *_FALLBACK_MODEL to "" to drop it.
_KNOWN_LLM_PROVIDERS = ("groq", "openai", "anthropic")
LLM_PROVIDER_ORDER = [p for p in dict.fromkeys(
    x.strip().lower() for x in os.environ.get("LLM_PROVIDER_ORDER", "groq,openai,anthropic").split(",")
) if p in _KNOWN_LLM_PROVIDERS] or list(_KNOWN_LLM_PROVIDERS)
# Extra app-level retries of the SAME provider/model for transient failures (timeout, connection, 429, 5xx), on top of
# the SDK's single retry. Bounded 0..3; exponential backoff from LLM_RETRY_BACKOFF_SECONDS, never past the request deadline.
LLM_TRANSIENT_RETRIES = max(0, min(3, int(os.environ.get("LLM_TRANSIENT_RETRIES", "1") or 1)))
LLM_RETRY_BACKOFF_SECONDS = max(0.0, min(5.0, float(os.environ.get("LLM_RETRY_BACKOFF_SECONDS", "0.5") or 0.5)))
# Hard cap on provider calls for ONE generate-sql request, shared by every fallback, JSON retry and repair round.
LLM_MAX_ATTEMPTS_PER_REQUEST = max(1, min(30, int(os.environ.get("LLM_MAX_ATTEMPTS_PER_REQUEST", "8") or 8)))


# ---------------------------------------------------------------------------
# LLM pricing (USD per 1M tokens: [input, output]) -- used to turn REAL provider token counts
# into estimated cost. These are list prices as understood at implementation time and WILL drift:
# override per deployment with LLM_PRICES_JSON, e.g.
#   LLM_PRICES_JSON='{"gpt-4o-mini": [0.15, 0.60], "my-model": [1, 2]}'
# Models not listed fall back to LLM_DEFAULT_PRICE_IN/OUT (deliberately conservative) so an
# unknown model is never accounted as free.
# ---------------------------------------------------------------------------
import json as _json

LLM_MODEL_PRICES_USD_PER_MTOK = {
    # Groq and Anthropic list prices checked Oct 2026; override with LLM_PRICES_JSON when they change.
    "openai/gpt-oss-120b": [0.15, 0.60],
    "openai/gpt-oss-20b": [0.075, 0.30],
    "gpt-4o-mini": [0.15, 0.60],
    "claude-haiku-4-5-20251001": [1.00, 5.00],
}
_prices_override = os.environ.get("LLM_PRICES_JSON", "").strip()
if _prices_override:
    try:
        for _m, _p in _json.loads(_prices_override).items():
            LLM_MODEL_PRICES_USD_PER_MTOK[str(_m)] = [float(_p[0]), float(_p[1])]
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as _exc:
        raise RuntimeError("LLM_PRICES_JSON must be a JSON object of model -> [input_usd, output_usd] per 1M tokens") from _exc
LLM_DEFAULT_PRICE_IN_USD_PER_MTOK = _env_number("LLM_DEFAULT_PRICE_IN", 1.0, float)
LLM_DEFAULT_PRICE_OUT_USD_PER_MTOK = _env_number("LLM_DEFAULT_PRICE_OUT", 3.0, float)


def llm_cost_cents(model: str | None, input_tokens: int, output_tokens: int) -> float:
    """Estimated cost in cents for one provider call."""
    price_in, price_out = LLM_MODEL_PRICES_USD_PER_MTOK.get(
        model or "", (LLM_DEFAULT_PRICE_IN_USD_PER_MTOK, LLM_DEFAULT_PRICE_OUT_USD_PER_MTOK))
    usd = (max(0, input_tokens) * price_in + max(0, output_tokens) * price_out) / 1_000_000
    return usd * 100


# ---------------------------------------------------------------------------
# AI control plane (Sprint 3): global spend cap, per-plan budget multiplier, circuit breaker, kill switch
# ---------------------------------------------------------------------------
def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Emergency stop: /api/generate-sql answers 503 AI_DISABLED without calling any provider.
AI_KILL_SWITCH = _env_flag("AI_KILL_SWITCH")

# Hard ceiling on TOTAL AI spend across ALL users per UTC day. 0 disables the global cap.
GLOBAL_DAILY_AI_BUDGET_USD = _env_number("GLOBAL_DAILY_AI_BUDGET_USD", 25.0, float)
GLOBAL_DAILY_AI_BUDGET_CENTS = GLOBAL_DAILY_AI_BUDGET_USD * 100

# Circuit breaker, one per provider (groq / openai / anthropic). Process-local state.
CIRCUIT_FAILURE_THRESHOLD = max(1, _env_number("CIRCUIT_FAILURE_THRESHOLD", 5))
CIRCUIT_COOLDOWN_SECONDS = max(1.0, _env_number("CIRCUIT_COOLDOWN_SECONDS", 30.0, float))

# Per-plan multiplier applied to DAILY_LLM_BUDGET_*. Derived from the plans in entitlements.py
# (read-only import; that module is not modified): a plan's multiplier is its daily query limit
# relative to the free plan's, so budgets scale with what the plan already entitles. Plans with
# no query limit (business) use LLM_UNLIMITED_PLAN_MULTIPLIER. Override wholesale with
# PLAN_BUDGET_MULTIPLIERS_JSON, e.g. '{"free": 1, "pro": 4}'.
from entitlements import PLAN_CONFIG as _PLAN_CONFIG, DEFAULT_PLAN as _DEFAULT_PLAN  # noqa: E402

LLM_UNLIMITED_PLAN_MULTIPLIER = _env_number("LLM_UNLIMITED_PLAN_MULTIPLIER", 20.0, float)


def _derive_plan_multipliers() -> dict:
    base = _PLAN_CONFIG[_DEFAULT_PLAN]["daily_query_limit"] or 1
    out = {}
    for name, plan in _PLAN_CONFIG.items():
        limit = plan["daily_query_limit"]
        out[name] = LLM_UNLIMITED_PLAN_MULTIPLIER if limit is None else max(1.0, limit / base)
    return out


PLAN_BUDGET_MULTIPLIERS = _derive_plan_multipliers()
_mult_override = os.environ.get("PLAN_BUDGET_MULTIPLIERS_JSON", "").strip()
if _mult_override:
    try:
        for _plan, _m in _json.loads(_mult_override).items():
            if float(_m) <= 0:
                raise ValueError("multiplier must be > 0")
            PLAN_BUDGET_MULTIPLIERS[str(_plan).lower()] = float(_m)
    except (ValueError, TypeError, AttributeError) as _exc:
        raise RuntimeError("PLAN_BUDGET_MULTIPLIERS_JSON must be a JSON object of plan -> positive number") from _exc


def plan_budget_multiplier(plan: str | None) -> float:
    """Budget multiplier for a plan name; unknown/missing plans get the free-plan multiplier."""
    return float(PLAN_BUDGET_MULTIPLIERS.get((plan or _DEFAULT_PLAN).lower(), PLAN_BUDGET_MULTIPLIERS.get(_DEFAULT_PLAN, 1.0)))

# ---------------------------------------------------------------------------
# Per-user storage / session quotas (Sprint 3). Per plan; override with QUOTA_LIMITS_JSON, e.g.
#   QUOTA_LIMITS_JSON='{"free": {"max_datasets": 5, "max_dataset_bytes": 209715200}}'
# max_sessions       rows in pipeline_sessions (empty idle sessions are auto-reclaimed first)
# max_datasets       distinct UPLOADED datasets (the bundled sample database is never counted)
# max_dataset_bytes  total stored size of those datasets
# ---------------------------------------------------------------------------
_MB = 1024 * 1024
QUOTA_LIMITS = {
    "free":     {"max_sessions": 20,   "max_datasets": 5,   "max_dataset_bytes": 100 * _MB},
    "pro":      {"max_sessions": 100,  "max_datasets": 25,  "max_dataset_bytes": 2048 * _MB},
    "team":     {"max_sessions": 250,  "max_datasets": 100, "max_dataset_bytes": 10240 * _MB},
    "business": {"max_sessions": 1000, "max_datasets": 500, "max_dataset_bytes": 51200 * _MB},
}
_quota_override = os.environ.get("QUOTA_LIMITS_JSON", "").strip()
if _quota_override:
    try:
        for _plan, _limits in _json.loads(_quota_override).items():
            target = QUOTA_LIMITS.setdefault(str(_plan).lower(), dict(QUOTA_LIMITS["free"]))
            for _k, _v in _limits.items():
                if _k not in ("max_sessions", "max_datasets", "max_dataset_bytes") or int(_v) < 0:
                    raise ValueError(f"bad quota key/value {_k}={_v}")
                target[_k] = int(_v)
    except (ValueError, TypeError, AttributeError) as _exc:
        raise RuntimeError("QUOTA_LIMITS_JSON must be {plan: {max_sessions|max_datasets|max_dataset_bytes: int>=0}}") from _exc
QUOTA_EMPTY_SESSION_RECLAIM_SECONDS = max(0, _env_number("QUOTA_EMPTY_SESSION_RECLAIM_SECONDS", 3600))


def plan_quota(plan: str | None) -> dict:
    """Quota limits for a plan name; unknown plans get the free limits."""
    return dict(QUOTA_LIMITS.get((plan or _DEFAULT_PLAN).lower(), QUOTA_LIMITS[_DEFAULT_PLAN]))


# ---------------------------------------------------------------------------
# Sprint 4: observability, reliability & operations
# ---------------------------------------------------------------------------
# Admission control (found by the Sprint 4 load test; the bug pre-dates Sprint 4). Every API request holds one pooled DB
# connection from get_db() until teardown, across several worker-thread hops. If more requests than threads are admitted at once,
# the extra ones occupy every thread while blocked on the pool and the connection holders can never get a thread to finish:
# a circular wait that only breaks at DB_POOL_TIMEOUT (10 s) as a burst of 503s. Cap in-flight API requests per process below
# the thread-pool size (spare threads cover teardown hops); the rest queue in the event loop for at most API_ADMISSION_WAIT_SECONDS.
API_ADMISSION_LIMIT = max(2, _env_number("API_ADMISSION_LIMIT", max(2, THREADPOOL_MAX_WORKERS - 4)))
API_ADMISSION_WAIT_SECONDS = max(0.5, _env_number("API_ADMISSION_WAIT_SECONDS", 8.0, float))

# Logging. JSON lines on stderr (as before Sprint 4; platforms collect both streams); every line is passed through the PII scrubber (observability/redaction.py).
LOG_FORMAT = os.environ.get("LOG_FORMAT", "json").strip().lower()
if LOG_FORMAT not in ("json", "text"):
    raise RuntimeError("LOG_FORMAT must be 'json' or 'text'")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
# Exception MESSAGES routinely embed user content (DB-driver detail with row values, SQL fragments, validation input) that
# pattern scrubbing cannot reliably remove, so by default logs carry exception TYPE + code frames only. Turn this on only while
# debugging in a non-production environment (messages are still scrubbed and truncated).
LOG_EXCEPTION_MESSAGES = _env_flag("LOG_EXCEPTION_MESSAGES", False)
# Key for the HMAC that turns user/session ids into short, stable, non-reversible log references.
# Defaults to a key derived from JWT_SECRET_KEY so no extra secret is required.
LOG_HASH_KEY = os.environ.get("LOG_HASH_KEY", "").strip() or ("qs-log:" + JWT_SECRET_KEY)

# Metrics: per-minute aggregate buckets (counters + latency histograms). Shared through Redis when REDIS_URL is set
# (so web workers AND the RQ worker contribute to one view); in-process otherwise. No per-request or per-user
# series are ever stored, only bounded label sets (route template, status class, provider, outcome).
METRICS_ENABLED = _env_flag("METRICS_ENABLED", True)
METRICS_RETENTION_SECONDS = max(600, _env_number("METRICS_RETENTION_SECONDS", 6 * 3600))
METRICS_FLUSH_INTERVAL_SECONDS = max(0.2, _env_number("METRICS_FLUSH_INTERVAL_SECONDS", 1.0, float))
GAUGE_TTL_SECONDS = max(10, _env_number("GAUGE_TTL_SECONDS", 30))

# Admin / ops dashboard. Only verified accounts whose e-mail is listed here may call /api/admin/ops/*.
ADMIN_EMAILS = frozenset(e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip())
# Optional bearer token for a metrics scraper (Prometheus text at /api/admin/ops/metrics). Blank = scraper access off.
OPS_METRICS_TOKEN = os.environ.get("OPS_METRICS_TOKEN", "").strip()

# Optional external error tracker (Sentry). Messages, request data, headers, locals and user info are never sent.
SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
SENTRY_ENVIRONMENT = os.environ.get("SENTRY_ENVIRONMENT", "").strip() or ENVIRONMENT

# AI reliability. The per-call SDK timeout was a hard-coded 25s; the overall deadline bounds one whole
# generate-sql request across provider fallbacks, the JSON retry and SQL repair rounds.
AI_PROVIDER_TIMEOUT_SECONDS = max(2.0, _env_number("AI_PROVIDER_TIMEOUT_SECONDS", 25.0, float))
AI_REQUEST_DEADLINE_SECONDS = max(5.0, _env_number("AI_REQUEST_DEADLINE_SECONDS", 60.0, float))
# Do not START another provider call with less than this left on the deadline.
AI_MIN_CALL_BUDGET_SECONDS = max(0.5, _env_number("AI_MIN_CALL_BUDGET_SECONDS", 3.0, float))

# Stale in-flight background jobs (worker killed / Redis flushed): failed and released after this long.
# Must exceed the RQ job_timeout (15 minutes).
JOB_STALE_SECONDS = max(1200, _env_number("JOB_STALE_SECONDS", 1500))
# Where upload and report jobs run: "queue" (the RQ worker), "in_process" (the web process itself, right after the response,
# for deployments without a worker such as Render's free plan), or "auto" (queue while a worker heartbeat is visible,
# otherwise in_process). In-process jobs are NOT durable: a restart or crash loses them, and they are then failed with
# JOB_INTERRUPTED (see JOB_ORPHAN_SECONDS) so the user can retry instead of waiting on a job nothing will finish.
JOB_EXECUTION = os.environ.get("JOB_EXECUTION", "").strip().lower() or "auto"   # blank (unfilled Render key) = auto
if JOB_EXECUTION not in {"auto", "queue", "in_process"}:
    raise RuntimeError("JOB_EXECUTION must be auto, queue or in_process.")
# How many heavy in-process jobs (uploads, reports) one web process runs at once; the rest wait. 1 keeps peak memory bounded.
IN_PROCESS_JOB_CONCURRENCY = max(1, _env_number("IN_PROCESS_JOB_CONCURRENCY", 1))
# With no worker alive, a queued/running job whose heartbeat is older than this has no process left to finish it: it is failed
# as JOB_INTERRUPTED. In-process jobs refresh their heartbeat every JOB_HEARTBEAT_SECONDS, so a live one never reaches it.
JOB_ORPHAN_SECONDS = max(30, _env_number("JOB_ORPHAN_SECONDS", 90))
JOB_HEARTBEAT_SECONDS = max(1, _env_number("JOB_HEARTBEAT_SECONDS", 15))
if JOB_HEARTBEAT_SECONDS * 3 > JOB_ORPHAN_SECONDS:
    raise RuntimeError("JOB_ORPHAN_SECONDS must be at least three times JOB_HEARTBEAT_SECONDS.")
WORKER_HEARTBEAT_INTERVAL_SECONDS = max(2, _env_number("WORKER_HEARTBEAT_INTERVAL_SECONDS", 10))
WORKER_HEARTBEAT_TTL_SECONDS = max(5, _env_number("WORKER_HEARTBEAT_TTL_SECONDS", 30))

# Readiness: cached non-critical probes so a probe storm cannot hammer S3/RQ.
READINESS_CACHE_SECONDS = max(0.0, _env_number("READINESS_CACHE_SECONDS", 5.0, float))
READINESS_DB_TIMEOUT_MS = max(200, _env_number("READINESS_DB_TIMEOUT_MS", 2000))

# SLOs (percent / milliseconds). Evaluated over SLO_WINDOW_MINUTES (and a short window for burn-rate alerts).
SLO_WINDOW_MINUTES = max(5, _env_number("SLO_WINDOW_MINUTES", 30))
SLO_AVAILABILITY_TARGET_PCT = _env_number("SLO_AVAILABILITY_TARGET_PCT", 99.5, float)
SLO_API_P95_MS = _env_number("SLO_API_P95_MS", 1500.0, float)
SLO_API_P99_MS = _env_number("SLO_API_P99_MS", 5000.0, float)
SLO_AI_SUCCESS_TARGET_PCT = _env_number("SLO_AI_SUCCESS_TARGET_PCT", 95.0, float)
SLO_AI_P95_MS = _env_number("SLO_AI_P95_MS", 30000.0, float)
SLO_QUEUE_WAIT_P95_MS = _env_number("SLO_QUEUE_WAIT_P95_MS", 30000.0, float)
SLO_JOB_SUCCESS_TARGET_PCT = _env_number("SLO_JOB_SUCCESS_TARGET_PCT", 98.0, float)
SLO_MIN_SAMPLES = max(1, _env_number("SLO_MIN_SAMPLES", 20))
SLO_BURN_RATE_THRESHOLD = _env_number("SLO_BURN_RATE_THRESHOLD", 14.4, float)

# Alerts. Evaluated by one elected process every ALERT_EVAL_INTERVAL_SECONDS; delivered to ALERT_WEBHOOK_URL (generic JSON,
# contains no user data) and always written to the incident table + logs.
ALERTS_ENABLED = _env_flag("ALERTS_ENABLED", IS_PRODUCTION)
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
ALERT_EVAL_INTERVAL_SECONDS = max(5, _env_number("ALERT_EVAL_INTERVAL_SECONDS", 30))
ALERT_RESOLVE_AFTER_SECONDS = max(0, _env_number("ALERT_RESOLVE_AFTER_SECONDS", 120))
ALERT_QUEUE_DEPTH = max(1, _env_number("ALERT_QUEUE_DEPTH", 100))
ALERT_QUEUE_OLDEST_SECONDS = max(1, _env_number("ALERT_QUEUE_OLDEST_SECONDS", 300))
ALERT_DB_POOL_UTILIZATION_PCT = _env_number("ALERT_DB_POOL_UTILIZATION_PCT", 90.0, float)
ALERT_GLOBAL_BUDGET_PCT = _env_number("ALERT_GLOBAL_BUDGET_PCT", 80.0, float)
OPS_EVENT_RETENTION_DAYS = max(1, _env_number("OPS_EVENT_RETENTION_DAYS", 30))

if IS_PRODUCTION and ALERT_WEBHOOK_URL and not ALERT_WEBHOOK_URL.startswith("https://"):
    raise RuntimeError("ALERT_WEBHOOK_URL must use HTTPS in production.")


# ---------------------------------------------------------------------------
# Sprint 5: billing (provider-neutral) and session lifecycle
# ---------------------------------------------------------------------------
# Billing is OFF unless PAYMENT_PROVIDER names a registered adapter (see payment_provider.py). With it unset, checkout
# returns 503 BILLING_NOT_CONFIGURED, the webhook endpoint 404s, and every account stays on the plan it already has.
PAYMENT_PROVIDER = os.environ.get("PAYMENT_PROVIDER", "").strip().lower()
# One or more webhook signing secrets, comma separated. Several are accepted at once so a secret can be rotated without
# dropping events: add the new one, deploy, switch the provider over, then remove the old one.
PAYMENT_WEBHOOK_SECRETS = tuple(s.strip() for s in os.environ.get("PAYMENT_WEBHOOK_SECRETS", "").split(",") if s.strip())
# Generic (HMAC) adapter only: the hosted-checkout page the user is sent to, the secret QuerySure signs that link with
# (so the plan/reference in it cannot be tampered with), and an optional customer-portal URL for managing/cancelling.
PAYMENT_CHECKOUT_URL = os.environ.get("PAYMENT_CHECKOUT_URL", "").strip()
PAYMENT_CHECKOUT_SIGNING_SECRET = os.environ.get("PAYMENT_CHECKOUT_SIGNING_SECRET", "").strip()
PAYMENT_PORTAL_URL = os.environ.get("PAYMENT_PORTAL_URL", "").strip()
# Provider price/plan identifier -> QuerySure plan key, e.g. '{"price_abc123": "pro"}'. Adapters use it to normalize events.
BILLING_PLAN_MAP_JSON = os.environ.get("BILLING_PLAN_MAP_JSON", "").strip()
BILLING_WEBHOOK_TOLERANCE_SECONDS = max(30, _env_number("BILLING_WEBHOOK_TOLERANCE_SECONDS", 300))
BILLING_WEBHOOK_MAX_BYTES = max(1024, _env_number("BILLING_WEBHOOK_MAX_BYTES", 65536))
# Paid access survives a failed renewal / missing renewal event for this long past current_period_end, then lapses.
BILLING_GRACE_HOURS = max(0, _env_number("BILLING_GRACE_HOURS", 72))
BILLING_CHECKOUT_TTL_MINUTES = max(5, _env_number("BILLING_CHECKOUT_TTL_MINUTES", 30))
# An event whose subscription/checkout we cannot resolve yet is answered 503 (so the provider retries -- the event that links
# them may simply not have arrived) for this long after it occurred, then acknowledged and ignored.
BILLING_UNRESOLVED_RETRY_HOURS = max(1, _env_number("BILLING_UNRESOLVED_RETRY_HOURS", 24))

# Tokens carry a ``sid`` claim backed by a server-side row so they can be revoked. Tokens minted before Sprint 5 have no
# ``sid``; they are accepted (until they expire) outside production so a rolling deploy does not need a forced sign-out in
# development, and refused in production.
AUTH_REQUIRE_SESSION_ID = _env_flag("AUTH_REQUIRE_SESSION_ID", IS_PRODUCTION)
AUTH_MAX_ACTIVE_SESSIONS = max(1, _env_number("AUTH_MAX_ACTIVE_SESSIONS", 20))
# Minimum seconds between two verification / reset e-mails for the same account (stops mail-bombing one inbox from many IPs).
AUTH_EMAIL_COOLDOWN_SECONDS = max(0, _env_number("AUTH_EMAIL_COOLDOWN_SECONDS", 60 if IS_PRODUCTION else 0))

if IS_PRODUCTION and PAYMENT_PROVIDER:
    if not PAYMENT_WEBHOOK_SECRETS:
        raise RuntimeError("PAYMENT_WEBHOOK_SECRETS must be set in production when PAYMENT_PROVIDER is set.")
    if PAYMENT_CHECKOUT_URL and not PAYMENT_CHECKOUT_URL.startswith("https://"):
        raise RuntimeError("PAYMENT_CHECKOUT_URL must use HTTPS in production.")
    if PAYMENT_PORTAL_URL and not PAYMENT_PORTAL_URL.startswith("https://"):
        raise RuntimeError("PAYMENT_PORTAL_URL must use HTTPS in production.")
