"""
Database layer.

Swaps the old in-memory `sessions: dict = {}` for real persistence. Works
against SQLite out of the box (zero config, good for local dev / small
deployments) and against Postgres in production by setting DATABASE_URL,
e.g. postgresql+psycopg2://user:pass@host:5432/querysure

Migrations: schema changes are managed by Alembic (migrations/), not by
init_db()/create_all() below. create_all() only ever runs in development
(see init_db()) -- production is expected to have already run
`alembic upgrade head` (render.yaml's startCommand does this before
uvicorn starts) against a real migration history, so redeploys can alter
existing tables instead of just no-op'ing on ones that already exist.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    JSON,
    Boolean,
    BigInteger,
    false as sa_false,
    true as sa_true,
    UniqueConstraint,
    Index,
    create_engine,
    text,
)
from sqlalchemy.exc import IntegrityError, TimeoutError as SATimeoutError
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session as OrmSession

from config import (DATABASE_URL, IS_PRODUCTION, DB_POOL_SIZE, DB_MAX_OVERFLOW, DB_POOL_TIMEOUT, DB_POOL_RECYCLE)

# Config centralization (Sprint 1 blocker fix): DATABASE_URL, its default,
# and the "refuse SQLite in production" guard now live in config.py (the
# single source of truth for env-derived config) and are imported here
# rather than read from os.environ directly. The guard fires at import
# time regardless of whether config.py is imported here or from api.py
# first -- either way it runs exactly once per process.

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine_kwargs = {"connect_args": _connect_args, "pool_pre_ping": True}
if not DATABASE_URL.startswith("sqlite"):
    engine_kwargs.update(pool_size=DB_POOL_SIZE, max_overflow=DB_MAX_OVERFLOW, pool_timeout=DB_POOL_TIMEOUT, pool_recycle=DB_POOL_RECYCLE)
engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def _uuid_str() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    email = Column(String(320), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    # Google subject is the stable, provider-issued identity key. Email alone
    # is never used to silently link a Google identity to a password account.
    google_subject = Column(String(255), unique=True, nullable=True, index=True)
    is_active = Column(Integer, default=1, nullable=False)  # 1/0 for cross-db bool
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    # Existing users are migrated as verified to preserve access; newly
    # registered users are explicitly created with email_verified=False.
    email_verified = Column(Boolean, default=False, nullable=False)
    auth_version = Column(Integer, default=0, nullable=False)
    plan = Column(String(20), default="free", nullable=False)

    pipeline_sessions = relationship(
        "PipelineSession", back_populates="user", cascade="all, delete-orphan"
    )


class Subscription(Base):
    """Billing-state snapshot; never the source of product entitlements.

    Subscription.plan records what a verified billing provider says about the
    billing state. Query authorization reads User.plan through entitlements.py.
    Only a trusted billing service may transition User.plan from this state.
    """

    __tablename__ = "subscriptions"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    plan = Column(String(20), nullable=False, default="free")
    status = Column(String(32), nullable=False, default="inactive")
    provider = Column(String(64), nullable=False)
    provider_customer_id = Column(String(255), nullable=True)
    provider_subscription_id = Column(String(255), nullable=True, unique=True)
    current_period_start = Column(DateTime(timezone=True), nullable=True)
    current_period_end = Column(DateTime(timezone=True), nullable=True)
    # Sprint 5: ``occurred_at`` of the newest provider event applied to this row. Provider webhooks are delivered
    # at-least-once and out of order; an event older than this is stale and must never overwrite newer state.
    provider_event_at = Column(DateTime(timezone=True), nullable=True)
    # The provider will end the subscription at current_period_end (the user cancelled but keeps access until then).
    cancel_at_period_end = Column(Boolean, nullable=False, default=False, server_default=sa_false())
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    user = relationship("User")


class CheckoutSession(Base):
    """One server-created checkout attempt. ``id`` is the ONLY value handed to the payment provider as the client
    reference: webhooks map back to a user through this row, never through a user id or e-mail in a payload."""

    __tablename__ = "checkout_sessions"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan = Column(String(20), nullable=False)
    provider = Column(String(64), nullable=False)
    provider_session_id = Column(String(255), nullable=True)
    status = Column(String(16), nullable=False, default="pending", server_default="pending")  # pending|completed|expired
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class BillingEvent(Base):
    """Idempotency + audit ledger: one row per provider event id that was received. UNIQUE(provider, event_id) is what
    makes a duplicate or replayed delivery a no-op. Deliberately stores NO raw payload (it can carry e-mail / card
    metadata) -- only a SHA-256 of it and the normalized, non-personal fields."""

    __tablename__ = "billing_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_billing_events_provider_event"),
        Index("ix_billing_events_status_received", "status", "received_at"),
    )

    id = Column(String(36), primary_key=True, default=_uuid_str)
    provider = Column(String(64), nullable=False)
    event_id = Column(String(255), nullable=False)
    event_type = Column(String(96), nullable=False)
    kind = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False, default="received")  # processed|ignored|rejected|deferred|failed
    outcome = Column(String(64), nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=True)
    received_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    # Deliberately NOT a foreign key: the ledger is kept (de-identified) when the account is deleted.
    user_id = Column(String(36), nullable=True, index=True)
    attempts = Column(Integer, nullable=False, default=1, server_default="1")
    payload_sha256 = Column(String(64), nullable=False)


class Payment(Base):
    """Payment ledger (succeeded / failed / refunded). Never stores card data. ``user_id`` is set NULL when the account
    is deleted so the accounting record can be retained without personal identifiers."""

    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("provider", "provider_payment_id", name="uq_payments_provider_payment"),)

    id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    provider = Column(String(64), nullable=False)
    provider_payment_id = Column(String(255), nullable=False)
    provider_subscription_id = Column(String(255), nullable=True)
    amount_minor = Column(BigInteger, nullable=True)
    currency = Column(String(3), nullable=True)
    status = Column(String(16), nullable=False)  # succeeded|failed|refunded
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class AuthSession(Base):
    """Server-side record of one issued access token (its ``sid`` claim). A token is honoured only while its row exists,
    is not revoked and has not expired -- which is what makes logout / "sign out everywhere" real revocation instead of
    'the client forgot the token'. Stores no IP address or user agent."""

    __tablename__ = "auth_sessions"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_reason = Column(String(32), nullable=True)


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"
    __table_args__ = (Index("ix_email_verification_user_id", "user_id"),)

    id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    user = relationship("User")


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    __table_args__ = (Index("ix_password_reset_user_id", "user_id"),)

    id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    user = relationship("User")


class PipelineSession(Base):
    """Persists exactly what the old in-memory `sessions[sid]` dict held,
    one row per session, scoped to the owning user."""

    __tablename__ = "pipeline_sessions"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)

    db_path = Column(String(1024), nullable=True)
    schema = Column(JSON, nullable=True)
    semantic = Column(JSON, nullable=True)
    retrieval = Column(JSON, nullable=True)
    sql = Column(String, nullable=True)
    sql_result = Column(JSON, nullable=True)
    question = Column(String, nullable=True)
    validation = Column(JSON, nullable=True)
    query_result = Column(JSON, nullable=True)
    reliability = Column(JSON, nullable=True)
    insights = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)
    version = Column(Integer, nullable=False, default=0, server_default="0")
    # Stored size of the user-UPLOADED dataset this session points at (NULL for no dataset and for the
    # shared sample database). Drives the per-user storage quota; deliberately not in SESSION_FIELDS.
    dataset_bytes = Column(BigInteger, nullable=True)
    # Catalog entry (datasets.id) for the uploaded dataset this session points at; NULL for no dataset and for the sample
    # database. No FK: kept consistent by workspaces.py, like query_history.session_id.
    dataset_id = Column(String(36), nullable=True, index=True)
    # Sprint 3: the catalog dataset's ``version`` this session's schema/semantic snapshot was built from. A re-import bumps the
    # dataset's version; a session on an older one is told to reopen instead of querying a schema it no longer matches.
    dataset_version = Column(Integer, nullable=True)

    user = relationship("User", back_populates="pipeline_sessions")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="queued", index=True)
    error_code = Column(String(64), nullable=True)
    result_ref = Column(Text, nullable=True)
    # Storage-quota bookkeeping for in-flight dataset uploads: the target session and the staged size are
    # counted against the owner's quota from the moment the job is created until it finishes (see quotas.py).
    session_id = Column(String(36), nullable=True)
    reserved_bytes = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    user = relationship("User")


class UsageDaily(Base):
    """Per-user, per-UTC-day LLM usage counters, used to enforce a daily
    cost budget on /api/generate-sql (the only endpoint that calls out to a
    paid LLM provider). Deliberately a separate table rather than new
    columns on User -- create_all() only creates tables that don't already
    exist, so this rolls out safely on a database that already has a
    `users` table, with no Alembic migration needed."""

    __tablename__ = "usage_daily"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_usage_daily_user_day"),)

    id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    day = Column(String(10), nullable=False, index=True)  # 'YYYY-MM-DD', UTC
    requests = Column(Integer, default=0, nullable=False)
    estimated_cost_cents = Column(Float, default=0.0, nullable=False)
    query_count = Column(Integer, default=0, nullable=False)

    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class AIInvocation(Base):
    """One row per /api/generate-sql call (successes AND failures) for cost, latency and
    reliability analysis. Deliberately stores NO API keys, NO prompts and NO user data rows:
    ``retrieval_summary`` holds table/column *names* only."""

    __tablename__ = "ai_invocations"
    __table_args__ = (Index("ix_ai_invocations_user_created", "user_id", "created_at"),)

    id = Column(String(36), primary_key=True, default=_uuid_str)
    request_id = Column(String(64), nullable=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    session_id = Column(String(36), nullable=True)  # no FK: audit rows outlive deleted sessions
    trace_id = Column(String(64), nullable=True, index=True)  # Sprint 2: joins this summary to its ai_calls rows
    provider = Column(String(32), nullable=True)
    model = Column(String(128), nullable=True)
    prompt_version = Column(String(32), nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    usage_estimated = Column(Boolean, nullable=False, default=False, server_default=sa_false())
    estimated_cost_cents = Column(Float, nullable=False, default=0.0, server_default="0")
    latency_ms = Column(Integer, nullable=True)
    retrieval_summary = Column(JSON, nullable=True)
    repair_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    fallback_used = Column(Boolean, nullable=False, default=False, server_default=sa_false())
    validation_status = Column(String(32), nullable=True)
    success = Column(Boolean, nullable=False, default=False, server_default=sa_false())
    error_type = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class AICall(Base):
    """One row per provider ATTEMPT (Sprint 2), grouped by ``trace_id`` -- the detail behind an AIInvocation summary.
    Holds ids, provider, model, task, timing, tokens, cost and a sanitized error code: never prompts, SQL, data or keys."""

    __tablename__ = "ai_calls"
    __table_args__ = (Index("ix_ai_calls_user_created", "user_id", "created_at"),)

    id = Column(String(36), primary_key=True, default=_uuid_str)
    trace_id = Column(String(64), nullable=False, index=True)
    request_id = Column(String(64), nullable=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace_id = Column(String(36), nullable=True)  # no FK: the trail outlives a deleted workspace
    task_type = Column(String(32), nullable=False)
    provider = Column(String(32), nullable=False)
    model = Column(String(128), nullable=True)
    attempt_no = Column(Integer, nullable=False)
    retry_index = Column(Integer, nullable=False, default=0, server_default="0")
    outcome = Column(String(16), nullable=False)            # success | failure | skipped
    error_category = Column(String(64), nullable=True)
    latency_ms = Column(Integer, nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    usage_estimated = Column(Boolean, nullable=False, default=False, server_default=sa_false())
    estimated_cost_cents = Column(Float, nullable=False, default=0.0, server_default="0")
    tool_calls = Column(JSON, nullable=True)                # names of structured-output tools the provider was forced to use
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class Report(Base):
    """One stored report artifact (json/html/pdf/rows_csv/summary_csv) in durable storage. ``key`` is the
    object key (``<prefix>/reports/<user_id>/<uuid>``) -- never returned to clients, never a filesystem path."""

    __tablename__ = "reports"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id = Column(String(36), nullable=True)  # no FK: a report outlives its session
    key = Column(String(512), nullable=False)
    kind = Column(String(32), nullable=False)
    size = Column(BigInteger, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class QueryHistory(Base):
    """One executed (or failed) query, owned by one user. Stores what is needed to REVIEW it later: the question, the
    SQL, status, a bounded copy of the result and the honest result-correctness assessment. Rerunning never reads the
    stored result: it revalidates the stored SQL and executes it against the user's CURRENT dataset."""

    __tablename__ = "query_history"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id = Column(String(36), nullable=True)  # no FK: history outlives the session
    question = Column(String, nullable=True)
    sql = Column(String, nullable=False)
    sql_edited = Column(Boolean, nullable=False, default=False, server_default=sa_false())
    dataset_ref = Column(JSON, nullable=True)       # {dataset_id, label, tables}
    status = Column(String(32), nullable=False)     # success | empty_result | failed
    row_count = Column(Integer, nullable=True)
    error = Column(String(500), nullable=True)
    result = Column(JSON, nullable=True)            # bounded: {columns, rows, truncated, total_rows}
    answer_meta = Column(JSON, nullable=True)       # {verification_status, reliability_level, reliability_score, summary}
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)


class OpsEvent(Base):
    """Append-only operational event (circuit breaker opened, alert fired, spend cap hit, ...). Privacy: ``detail`` only
    ever holds allow-listed scalar keys (see observability/events.py) -- never user ids, prompts, SQL, data or e-mail."""

    __tablename__ = "ops_events"
    __table_args__ = (Index("ix_ops_events_kind_created", "kind", "created_at"),)

    id = Column(String(36), primary_key=True, default=_uuid_str)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    kind = Column(String(64), nullable=False)
    severity = Column(String(16), nullable=False, default="info", server_default="info")
    component = Column(String(32), nullable=False, default="app", server_default="app")
    detail = Column(JSON, nullable=True)


class OpsIncident(Base):
    """One incident per alert key while it is open (enforced by the partial unique index), then resolved. Aggregate system
    state only -- same ``detail`` privacy rule as OpsEvent."""

    __tablename__ = "ops_incidents"
    __table_args__ = (
        Index("ix_ops_incidents_status_opened", "status", "opened_at"),
        Index("uq_ops_incidents_open_key", "incident_key", unique=True,
              postgresql_where=text("status = 'open'"), sqlite_where=text("status = 'open'")),
    )

    id = Column(String(36), primary_key=True, default=_uuid_str)
    incident_key = Column(String(96), nullable=False)
    alert = Column(String(64), nullable=False)
    severity = Column(String(16), nullable=False, default="warning", server_default="warning")
    component = Column(String(32), nullable=False, default="app", server_default="app")
    status = Column(String(16), nullable=False, default="open", server_default="open")
    opened_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    occurrences = Column(Integer, nullable=False, default=1, server_default="1")
    detail = Column(JSON, nullable=True)


class Workspace(Base):
    """A unit of ownership and authorization. Every user has exactly one personal workspace (``personal_owner_id`` set,
    unique) and may own or belong to team workspaces. Roles live in ``workspace_members``."""

    __tablename__ = "workspaces"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    name = Column(String(120), nullable=False)
    kind = Column(String(16), nullable=False, default="team", server_default="team")  # personal | team
    owner_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    personal_owner_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, unique=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_workspace_members_workspace_user"),)

    id = Column(String(36), primary_key=True, default=_uuid_str)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(16), nullable=False)  # owner | admin | analyst | viewer
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class Dataset(Base):
    """Catalog entry for one stored, uploaded dataset (a managed SQLite file in storage). A row exists exactly as long as
    its file does: storage cleanup deletes the row with the file. ``storage_ref`` is internal and never returned by the API.
    The dataset always belongs to a workspace its creator is a member of (re-homed to their personal workspace otherwise)."""

    __tablename__ = "datasets"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    source_type = Column(String(32), nullable=False, default="upload", server_default="upload")
    file_format = Column(String(16), nullable=True)  # csv | json | xlsx | xlsm | sqlite
    storage_ref = Column(Text, nullable=False, unique=True)
    bytes = Column(BigInteger, nullable=True)
    table_count = Column(Integer, nullable=True)
    row_count = Column(BigInteger, nullable=True)
    schema_version = Column(Integer, nullable=False, default=1, server_default="1")  # bumped when a re-import changes the schema
    # Sprint 3: ``version`` is bumped on EVERY successful import (data swap); ``status`` is importing | ready;
    # ``fingerprint`` is the sha256 of the source file behind the current curated version.
    version = Column(Integer, nullable=False, default=1, server_default="1")
    status = Column(String(16), nullable=False, default="ready", server_default="ready")
    connector = Column(String(32), nullable=True)
    fingerprint = Column(String(64), nullable=True)
    # Sprint 4: a dataset imported from a database connection keeps its source (NULL for file uploads). ``sync_config`` holds
    # {mode, watermark_column, watermark_type, primary_key, source_schema}; detaching (deleting the connection) clears connection_id.
    connection_id = Column(String(36), nullable=True, index=True)
    source_table = Column(String(255), nullable=True)
    sync_config = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class Connection(Base):
    """A saved connection to an external database (Sprint 4), owned by a workspace. ``config`` holds non-secret settings
    (host, port, database, user, ssl_mode); the password lives only in ``secret_ciphertext`` (Fernet, bound to this row's id)
    and is never returned by any API. Disabled connections cannot be tested, browsed or synced."""

    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_connections_workspace_name"),)

    id = Column(String(36), primary_key=True, default=_uuid_str)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by = Column(String(36), nullable=True)   # no FK: the connection outlives the creator's membership
    name = Column(String(120), nullable=False)
    connector = Column(String(32), nullable=False)   # postgres | mysql
    config = Column(JSON, nullable=False)
    secret_ciphertext = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="active", server_default="active")   # active | disabled
    last_test_at = Column(DateTime(timezone=True), nullable=True)
    last_test_ok = Column(Boolean, nullable=True)
    last_test_error = Column(String(64), nullable=True)   # error code, never a driver message
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class IngestionCheckpoint(Base):
    """Where the next incremental run of a dataset starts. Written in the SAME transaction as the version swap, so a failed
    run never advances it (nothing is lost) and a re-run of the same window only re-reads rows (upserts keep it idempotent)."""

    __tablename__ = "ingestion_checkpoints"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    dataset_id = Column(String(36), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, unique=True)
    column_name = Column(String(255), nullable=False)
    column_type = Column(String(16), nullable=False)    # integer | timestamp
    value = Column(String(64), nullable=False)          # integer as text, timestamp as UTC ISO text
    # Sprint 5: structured state for sources that are not a single watermark (S3: {"objects": {key: [etag, modified, size, rows]}}).
    state = Column(JSON, nullable=True)
    run_id = Column(String(36), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class KnowledgeDocument(Base):
    """A text document in a workspace's knowledge hub (Sprint 7). Only the extracted text is kept, as ``KnowledgeChunk`` rows; the
    uploaded file itself is not retained. Deleting the document deletes its chunks, so it can no longer be retrieved or cited."""

    __tablename__ = "knowledge_documents"
    __table_args__ = (Index("uq_knowledge_documents_ws_fingerprint", "workspace_id", "fingerprint", unique=True),)

    id = Column(String(36), primary_key=True, default=_uuid_str)
    workspace_id = Column(String(36), nullable=False, index=True)
    created_by = Column(String(36), nullable=False)
    name = Column(String(255), nullable=False)
    file_format = Column(String(16), nullable=False)          # txt | md | pdf | docx
    bytes = Column(BigInteger, nullable=False, default=0, server_default="0")
    fingerprint = Column(String(64), nullable=False)          # sha256 of the uploaded bytes: the same file is not indexed twice
    chunk_count = Column(Integer, nullable=False, default=0, server_default="0")
    char_count = Column(Integer, nullable=False, default=0, server_default="0")
    page_count = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (Index("ix_knowledge_chunks_doc_ordinal", "document_id", "ordinal"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(String(36), ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False)
    workspace_id = Column(String(36), nullable=False, index=True)
    ordinal = Column(Integer, nullable=False)
    page = Column(Integer, nullable=True)
    heading = Column(String(200), nullable=True)
    text = Column(Text, nullable=False)


class Conversation(Base):
    """A private assistant chat of one user inside a workspace (Sprint 8). Only its creator can read it; deleting the user, the
    workspace or the chat deletes its messages."""

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_ws_user_updated", "workspace_id", "user_id", "updated_at"),)

    id = Column(String(36), primary_key=True, default=_uuid_str)
    workspace_id = Column(String(36), nullable=False)
    user_id = Column(String(36), nullable=False)
    title = Column(String(120), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class ConversationMessage(Base):
    """One turn of a ``Conversation``. Assistant turns keep the intent, the validated citations, the source passages shown to the user
    and the provider/trace ids; no secrets or API keys are stored."""

    __tablename__ = "conversation_messages"
    __table_args__ = (Index("ix_conversation_messages_conv", "conversation_id", "id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(16), nullable=False)                  # user | assistant
    content = Column(Text, nullable=False)
    intent = Column(String(24), nullable=True)                 # knowledge | data_question | smalltalk
    grounded = Column(Boolean, nullable=True)
    citations = Column(JSON, nullable=True)
    sources = Column(JSON, nullable=True)
    provider = Column(String(40), nullable=True)
    trace_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class AgentRun(Base):
    """One run of the controlled agent (Sprint 9): a goal, a validated plan of registered tool calls and its outcome. The plan is made
    once; tool output is never fed back to the model, so retrieved text cannot change what runs."""

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_ws_user_created", "workspace_id", "user_id", "created_at"),)

    id = Column(String(36), primary_key=True, default=_uuid_str)
    workspace_id = Column(String(36), nullable=False)
    user_id = Column(String(36), nullable=False)
    goal = Column(Text, nullable=False)
    status = Column(String(24), nullable=False, default="planned")   # planned | awaiting_approval | completed | failed | rejected | cancelled
    message = Column(Text, nullable=True)                            # the planner's note when no tool is needed, or the outcome summary
    error_code = Column(String(40), nullable=True)
    provider = Column(String(40), nullable=True)
    trace_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class AgentStep(Base):
    """One planned tool call. Write steps wait as ``awaiting_approval`` until a person with the tool's role approves the exact
    arguments stored here; the executed arguments are always these stored ones."""

    __tablename__ = "agent_steps"
    __table_args__ = (Index("ix_agent_steps_run_ordinal", "run_id", "ordinal"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    ordinal = Column(Integer, nullable=False)
    tool = Column(String(40), nullable=False)
    args = Column(JSON, nullable=False)
    why = Column(String(300), nullable=True)
    risk = Column(String(8), nullable=False)                         # read | write
    status = Column(String(20), nullable=False, default="pending")  # pending | awaiting_approval | succeeded | failed | rejected | skipped
    result = Column(JSON, nullable=True)
    error = Column(String(300), nullable=True)
    decided_by = Column(String(36), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)


class Investigation(Base):
    """A stored investigation report (Sprint 10): parameters, dataset version and the structured result (facts / hypotheses /
    recommendations / limitations). ``exports`` maps format -> Report id (downloadable by the creator via /api/reports/{id}/download)."""

    __tablename__ = "investigations"
    __table_args__ = (Index("ix_investigations_ws_created", "workspace_id", "created_at"), Index("ix_investigations_dataset", "dataset_id"))

    id = Column(String(36), primary_key=True, default=_uuid_str)
    workspace_id = Column(String(36), nullable=False)
    dataset_id = Column(String(36), nullable=False)
    created_by = Column(String(36), nullable=False)
    kind = Column(String(16), nullable=False)
    title = Column(String(255), nullable=False)
    dataset_version = Column(Integer, nullable=False, default=1, server_default="1")
    params = Column(JSON, nullable=False)
    report = Column(JSON, nullable=False)
    exports = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class DatasetQualityReport(Base):
    """Data-quality profile and findings for ONE successful ingestion run (Sprint 6). Deterministic checks compare the new version with
    the previous one and with the recent history of row counts. Holds names, counts and numeric/date summaries only -- never raw text
    values. One row per run; the oldest are trimmed (QUALITY_REPORTS_KEEP per dataset)."""

    __tablename__ = "dataset_quality_reports"
    __table_args__ = (Index("ix_dataset_quality_dataset_created", "dataset_id", "created_at"),)

    id = Column(String(36), primary_key=True, default=_uuid_str)
    dataset_id = Column(String(36), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    workspace_id = Column(String(36), nullable=False, index=True)
    run_id = Column(String(36), nullable=False, unique=True)
    dataset_version = Column(Integer, nullable=True)
    status = Column(String(8), nullable=False)               # ok | warn | error
    profile = Column(JSON, nullable=True)                     # {table: {rows, columns: {name: {...}}}}
    findings = Column(JSON, nullable=True)                    # [{rule, severity, table, column, message, value, baseline}]
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class DatasetSchedule(Base):
    """Run a connection-backed dataset's sync on an interval (Sprint 5). The scheduler claims a due row with one conditional UPDATE
    (so several scheduler processes never both fire it); a run that is still active is skipped, never doubled. After
    SCHEDULER_MAX_CONSECUTIVE_FAILURES failed runs, or when the owner loses access, the schedule disables itself and says why."""

    __tablename__ = "dataset_schedules"
    __table_args__ = (Index("ix_dataset_schedules_due", "enabled", "next_run_at"),)

    id = Column(String(36), primary_key=True, default=_uuid_str)
    dataset_id = Column(String(36), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, unique=True)
    interval_minutes = Column(Integer, nullable=False)
    mode = Column(String(16), nullable=False, default="incremental", server_default="incremental")
    enabled = Column(Boolean, nullable=False, default=True, server_default=sa_true())
    next_run_at = Column(DateTime(timezone=True), nullable=False)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_status = Column(String(16), nullable=True)           # queued | succeeded | failed | skipped
    last_job_id = Column(String(36), nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0, server_default="0")
    disabled_reason = Column(String(64), nullable=True)
    created_by = Column(String(36), nullable=False)           # runs act as this user; they must keep analyst+ in the workspace
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class IngestionRun(Base):
    """One import of a dataset (Sprint 3). At most ONE active (queued/running) run per dataset: a partial unique index, so the
    database enforces it even across processes. Counts cover the whole source; row-level rejections live in the dataset's own
    SQLite file (``_qs_quarantine``), never here. ``error_detail`` and ``table_stats`` hold names and counts only."""

    __tablename__ = "ingestion_runs"
    __table_args__ = (
        Index("ix_ingestion_runs_dataset_started", "dataset_id", "started_at"),
        Index("uq_ingestion_runs_active_dataset", "dataset_id", unique=True,
              postgresql_where=text("status IN ('queued', 'running')"), sqlite_where=text("status IN ('queued', 'running')")),
    )

    id = Column(String(36), primary_key=True, default=_uuid_str)
    dataset_id = Column(String(36), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    workspace_id = Column(String(36), nullable=False, index=True)
    user_id = Column(String(36), nullable=True)           # who started it; no FK so history outlives the account
    job_id = Column(String(36), nullable=True, index=True)
    connector = Column(String(32), nullable=False)
    mode = Column(String(16), nullable=False, default="full_refresh", server_default="full_refresh")
    status = Column(String(16), nullable=False, default="queued", server_default="queued")  # queued|running|succeeded|failed|skipped|interrupted
    source_name = Column(String(255), nullable=True)
    source_bytes = Column(BigInteger, nullable=True)
    source_fingerprint = Column(String(64), nullable=True)
    records_read = Column(BigInteger, nullable=False, default=0, server_default="0")
    records_inserted = Column(BigInteger, nullable=False, default=0, server_default="0")
    records_updated = Column(BigInteger, nullable=False, default=0, server_default="0")
    records_skipped = Column(BigInteger, nullable=False, default=0, server_default="0")
    records_rejected = Column(BigInteger, nullable=False, default=0, server_default="0")
    duplicate_rows = Column(BigInteger, nullable=False, default=0, server_default="0")  # detected, kept (report only)
    reject_reasons = Column(JSON, nullable=True)   # {reason_code: count}
    table_stats = Column(JSON, nullable=True)      # {table: {read, inserted, rejected}}
    schema_changes = Column(JSON, nullable=True)   # [{table, change, column, from, to, compatible}]
    dataset_version = Column(Integer, nullable=True)  # the version this run produced
    curated_ref = Column(Text, nullable=True)
    raw_ref = Column(Text, nullable=True)
    # Sprint 4: incremental runs record the watermark window and why a requested incremental became a full refresh.
    watermark_before = Column(String(64), nullable=True)
    watermark_after = Column(String(64), nullable=True)
    fallback_reason = Column(String(64), nullable=True)
    error_code = Column(String(64), nullable=True)
    error_detail = Column(String(300), nullable=True)
    started_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """Append-only record of who did what in a workspace. The application never updates or deletes a row (enforced in
    audit.py); rows go only when their workspace is deleted (account or workspace erasure). ``details`` holds allow-listed
    scalars only: never secrets, SQL, questions, data values or e-mail addresses."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_workspace_created", "workspace_id", "created_at"),)

    id = Column(String(36), primary_key=True, default=_uuid_str)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    actor_type = Column(String(16), nullable=False)  # user | agent | system
    actor_id = Column(String(36), nullable=True)     # no FK: entries outlive a departed member
    action = Column(String(64), nullable=False)
    target_type = Column(String(32), nullable=True)
    target_id = Column(String(64), nullable=True)
    details = Column(JSON, nullable=True)
    request_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


def init_db() -> None:
    """Create tables directly -- development convenience only.

    Sprint 1 correction: previously called unconditionally at startup
    (api.py's startup event), which meant production was relying on
    create_all() exactly like a fresh local dev database, silently masking
    the fact that schema changes wouldn't actually alter existing prod
    tables. Now a no-op outside development: production schema changes go
    through `alembic upgrade head` (migrations/), run as part of the
    deploy (see render.yaml's startCommand) before the app process starts,
    so this function never has anything to do by the time a production
    request is served.
    """
    if IS_PRODUCTION:
        return
    if engine.dialect.name == "postgresql":
        # Several uvicorn workers (docker compose, --workers N) all run this at startup; unserialised, concurrent
        # create_all() calls race on pg_type/pg_class and all but one worker crash with UniqueViolation
        # "already exists". A session-level advisory lock makes the others wait, then find the tables present.
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT pg_advisory_lock(7435210001)")
            try:
                Base.metadata.create_all(bind=conn)
                conn.commit()
            finally:
                conn.rollback()
                conn.exec_driver_sql("SELECT pg_advisory_unlock(7435210001)")
                conn.commit()
        return
    # SQLite has no advisory lock. With WEB_CONCURRENCY>1 two workers can both see a table as missing and both issue
    # CREATE TABLE; the loser used to crash at startup with "table ... already exists" (reproduced on fresh local
    # starts). create_all() skips tables that exist, so retrying after that error is safe and converges.
    from sqlalchemy.exc import OperationalError
    import time as _time
    for attempt in range(6):
        try:
            Base.metadata.create_all(bind=engine)
            return
        except OperationalError as exc:
            msg = str(exc).lower()
            if attempt == 5 or not ("already exists" in msg or "database is locked" in msg):
                raise
            _time.sleep(0.2 * (attempt + 1))


def get_db():
    """FastAPI dependency — yields a DB session and always closes it."""
    db = None
    try:
        db = SessionLocal()
        # Force SQLAlchemy to check out a real connection at the dependency boundary.
        # SessionLocal() itself is lazy and does not normally touch the pool.
        db.connection()
    except SATimeoutError as exc:
        try:
            db.close()
        except Exception:
            pass
        raise DatabaseBusyError("Database is temporarily busy") from exc
    try:
        yield db
    finally:
        db.close()


SESSION_FIELDS = [
    "db_path", "schema", "semantic", "retrieval", "sql", "sql_result",
    "question", "validation", "query_result", "reliability", "insights",
]


def session_to_dict(row: PipelineSession) -> dict:
    return {field: getattr(row, field) for field in SESSION_FIELDS}


class DatabaseBusyError(Exception):
    pass


class SessionNotFoundError(Exception):
    pass


class SessionConflictError(Exception):
    pass


def _bind_session_ref(session_id) -> None:
    """Attach a pseudonymous session reference to the request's log context (Sprint 4). Never raises."""
    try:
        from observability.context import bind_session
        bind_session(session_id)
    except Exception:  # noqa: BLE001
        pass


def create_session(db: OrmSession, user_id: str) -> PipelineSession:
    row = PipelineSession(id=_uuid_str(), user_id=user_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_or_create_session(db: OrmSession, user_id: str, session_id: str | None, *, create: bool = False) -> PipelineSession:
    """Get an owned session. Only the explicit session-creation endpoint may create rows."""
    _bind_session_ref(session_id)
    if not session_id:
        if create:
            return create_session(db, user_id)
        raise SessionNotFoundError("Session not found")
    row = db.query(PipelineSession).filter(
        PipelineSession.id == session_id, PipelineSession.user_id == user_id
    ).first()
    if row is None:
        raise SessionNotFoundError("Session not found")
    return row


def update_session(db: OrmSession, row: PipelineSession, **fields) -> PipelineSession:
    """Optimistic-lock a session update and merge once when fields do not conflict."""
    expected_version = int(row.version or 0)
    changed_fields = set(fields)
    values = dict(fields)
    values["version"] = expected_version + 1
    values["updated_at"] = _now()
    # Snapshot what *this* request read BEFORE rolling back: rollback expires the instance, and
    # re-loading ``row`` from the DB would make it identical to ``fresh`` and hide real conflicts.
    original = {name: getattr(row, name) for name in changed_fields}
    result = db.query(PipelineSession).filter(
        PipelineSession.id == row.id, PipelineSession.version == expected_version
    ).update(values, synchronize_session=False)
    if result == 1:
        db.commit()
        db.refresh(row)
        return row
    db.rollback()
    fresh = db.query(PipelineSession).filter(
        PipelineSession.id == row.id, PipelineSession.user_id == row.user_id
    ).first()
    if fresh is None:
        raise SessionNotFoundError("Session not found")
    conflict = any(getattr(fresh, name) != original[name] for name in changed_fields)
    if conflict:
        raise SessionConflictError("Session was changed by another request")
    fresh_values = dict(fields)
    fresh_values["version"] = int(fresh.version or 0) + 1
    fresh_values["updated_at"] = _now()
    result = db.query(PipelineSession).filter(
        PipelineSession.id == fresh.id, PipelineSession.version == int(fresh.version or 0)
    ).update(fresh_values, synchronize_session=False)
    if result != 1:
        db.rollback()
        raise SessionConflictError("Session was changed by another request")
    db.commit()
    db.refresh(fresh)
    return fresh


def _today_str() -> str:
    return _now().strftime("%Y-%m-%d")


def get_or_create_daily_usage(db: OrmSession, user_id: str) -> UsageDaily:
    """Fetch (or create) today's usage row for this user. A day naturally
    resets the budget just by no row existing for the new date yet -- no
    cron job or cleanup needed."""
    day = _today_str()
    row = (
        db.query(UsageDaily)
        .filter(UsageDaily.user_id == user_id, UsageDaily.day == day)
        .first()
    )
    if row is None:
        row = UsageDaily(id=_uuid_str(), user_id=user_id, day=day, requests=0, estimated_cost_cents=0, query_count=0)
        db.add(row)
        try:
            db.commit()
            db.refresh(row)
        except IntegrityError:
            # Another worker may have created the unique user/day row first.
            # Re-read it rather than turning a harmless first-request race into a 500.
            db.rollback()
            row = (
                db.query(UsageDaily)
                .filter(UsageDaily.user_id == user_id, UsageDaily.day == day)
                .first()
            )
            if row is None:
                raise
    return row


def reserve_query_slot(db: OrmSession, user_id: str, daily_limit: int | None) -> UsageDaily | None:
    """Atomically reserve one logical analytics query for the user/day.

    The row is persistent in the database, so usage survives restarts and is
    shared across workers. A SQL UPDATE predicate prevents two concurrent
    workers from both consuming the final available slot.
    """
    row = get_or_create_daily_usage(db, user_id)
    if daily_limit is not None:
        updated = (
            db.query(UsageDaily)
            .filter(UsageDaily.id == row.id, UsageDaily.query_count < daily_limit)
            .update({UsageDaily.query_count: UsageDaily.query_count + 1}, synchronize_session=False)
        )
        if updated != 1:
            db.rollback()
            return None
    else:
        row.query_count += 1
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def refund_query_slot(db: OrmSession, user_id: str) -> bool:
    """Refund one reserved daily query slot without ever decrementing below zero."""
    row = get_or_create_daily_usage(db, user_id)
    updated = (
        db.query(UsageDaily)
        .filter(UsageDaily.id == row.id, UsageDaily.query_count > 0)
        .update({UsageDaily.query_count: UsageDaily.query_count - 1}, synchronize_session=False)
    )
    db.commit()
    return updated == 1


def record_llm_usage(db: OrmSession, user_id: str, cost_cents: int) -> UsageDaily:
    row = get_or_create_daily_usage(db, user_id)
    row.requests += 1
    row.estimated_cost_cents += max(0, cost_cents)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def reserve_daily_budget(db: OrmSession, user_id: str, estimated_cents: float, limit_cents: float) -> bool:
    """ATOMICALLY reserve ``estimated_cents`` of today's AI budget in the database.

    One conditional UPDATE (``cost < limit``) is the whole check-and-reserve, so two concurrent requests can
    never both take the last of the budget -- on SQLite and PostgreSQL alike. The row already contains today's
    real spend (reserve/settle keep it that way). Same admission rule the DB path always had -- a request that
    STARTS under the limit may finish slightly over it -- but now the overshoot is bounded by ONE request's
    estimate instead of by however many requests raced the old read-then-check.
    Returns False (nothing reserved) once the limit is reached."""
    row = get_or_create_daily_usage(db, user_id)
    updated = (
        db.query(UsageDaily)
        .filter(UsageDaily.id == row.id, UsageDaily.estimated_cost_cents < limit_cents)
        .update({UsageDaily.estimated_cost_cents: UsageDaily.estimated_cost_cents + estimated_cents},
                synchronize_session=False)
    )
    db.commit()
    return updated == 1


def settle_daily_budget(db: OrmSession, user_id: str, reserved_cents: float, actual_cents: float,
                        *, count_request: bool) -> UsageDaily:
    """Replace a reservation made by reserve_daily_budget with the real cost (``actual_cents`` may be 0 to fully
    roll the reservation back). One atomic relative UPDATE: concurrent settlements compose correctly."""
    row = get_or_create_daily_usage(db, user_id)
    values = {UsageDaily.estimated_cost_cents: UsageDaily.estimated_cost_cents + (max(0.0, actual_cents) - reserved_cents)}
    if count_request:
        values[UsageDaily.requests] = UsageDaily.requests + 1
    db.query(UsageDaily).filter(UsageDaily.id == row.id).update(values, synchronize_session=False)
    db.commit()
    db.refresh(row)
    return row


def global_spend_cents_db(db: OrmSession) -> float:
    """Total of today's per-user ledgers (real spend + in-flight DB-mode reservations)."""
    from sqlalchemy import func as _f
    total = db.query(_f.coalesce(_f.sum(UsageDaily.estimated_cost_cents), 0.0)).filter(UsageDaily.day == _today_str()).scalar()
    return float(total or 0.0)


def record_ai_invocation(db: OrmSession, **fields) -> AIInvocation | None:
    """Persist one AIInvocation row. Observability must never break the request: any failure
    is rolled back and swallowed (logged), and the caller's response is unaffected."""
    import logging
    allowed = {c.name for c in AIInvocation.__table__.columns} - {"id", "created_at"}
    clean = {k: v for k, v in fields.items() if k in allowed}
    try:
        row = AIInvocation(**clean)
        db.add(row)
        db.commit()
        return row
    except Exception:  # noqa: BLE001
        db.rollback()
        logging.getLogger("querysure").exception("Could not record AIInvocation")
        return None
