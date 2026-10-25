"""Redis/RQ-backed background jobs for heavy ingestion, reports and email.

RQ is used because it is deliberately small, Redis-native, easy to operate with
an existing Redis deployment, and provides a dedicated worker process without
introducing another broker or service architecture.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone

import redis
from rq import Queue

import logging

import storage
from config import REDIS_URL, IS_PRODUCTION, JOB_STALE_SECONDS
from observability import context as obs_context, errors as obs_errors, events as obs_events, gauges as obs_gauges
from observability import metrics as obs_metrics
from db import Job, PipelineSession, SessionLocal, SessionConflictError, get_or_create_session, update_session
from modules import load_database, build_semantic_layer, build_vector_store, clear_cache_key, generate_report
from modules.ingestion import ingest_to_sqlite
from modules.schema_engine import SchemaTooLarge

QUEUE_NAME = "querysure"
MAX_ATTEMPTS = 3
logger = logging.getLogger("querysure.jobs")
STALE_ERROR_CODE = "JOB_STALE"


def _now():
    return datetime.now(timezone.utc)


def _redis():
    if not REDIS_URL:
        return None
    client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    client.ping()
    return client


def _update_job(db, job, **values):
    for key, value in values.items():
        setattr(job, key, value)
    job.updated_at = _now()
    db.commit()
    db.refresh(job)
    return job


def _cleanup_superseded(db, old_ref, new_ref, session_id):
    if not old_ref or old_ref == new_ref or not storage.is_managed(old_ref):
        return
    if db.query(PipelineSession.id).filter(PipelineSession.db_path == old_ref, PipelineSession.id != session_id).first():
        return
    storage.delete_dataset(old_ref)


def _run_upload(db, job, payload):
    object_ref = payload["object_ref"]
    ext = payload["ext"]
    fname = payload["fname"]
    session_id = payload["session_id"]
    user_id = job.user_id
    raw_path = None
    path = None
    db_ref = None
    dataset_bytes = None
    try:
        if job.result_ref:
            saved = json.loads(job.result_ref)
            db_ref = saved.get("db_ref")
        if db_ref:
            path = storage.ensure_local(db_ref, user_id)
            dataset_bytes = os.path.getsize(path)
            schema = load_database(path)
            semantic = build_semantic_layer(schema)
            build_vector_store(semantic, cache_key=session_id)
        else:
            raw_path = storage.ensure_staged_local(object_ref, user_id, ext)
            path = raw_path if ext in {".db", ".sqlite", ".sqlite3"} else os.path.join(storage.UPLOAD_DIR, f".job-{uuid.uuid4().hex}.db")
            if ext in {".db", ".sqlite", ".sqlite3"}:
                from modules.ingestion import validate_sqlite
                validate_sqlite(raw_path)
            else:
                ingest_to_sqlite(raw_path, ext, path, os.path.splitext(fname)[0])
            row = get_or_create_session(db, user_id, session_id)
            schema = load_database(path)
            semantic = build_semantic_layer(schema)
            build_vector_store(semantic, cache_key=row.id)
            dataset_bytes = os.path.getsize(path)
            db_ref = storage.store_dataset(path, user_id)
            _update_job(db, job, result_ref=json.dumps({"db_ref": db_ref}, separators=(",", ":")))
        row = get_or_create_session(db, user_id, session_id)
        old_ref = row.db_path
        clear_cache_key(row.id)
        row = update_session(db, row, db_path=db_ref, dataset_bytes=dataset_bytes, schema=schema, semantic=semantic,
                             retrieval=None, sql=None, sql_result=None, question=None,
                             validation=None, query_result=None, reliability=None, insights=None)
        _cleanup_superseded(db, old_ref, db_ref, row.id)
        return {"session_id": row.id, "db_name": fname, "table_count": schema["table_count"],
                "total_rows": schema["total_rows"], "relationships": len(schema["relationships"]),
                "db_size": schema["db_size"], "tables": _schema_summary(schema),
                "health_score": _calc_health(schema)}
    finally:
        # Local backend + SQLite upload: the staged file *is* the stored dataset (db_ref == raw_path).
        # Deleting it would orphan the session's db_path and produce DATASET_UNAVAILABLE (410).
        if raw_path and raw_path != db_ref and os.path.exists(raw_path):
            try: os.remove(raw_path)
            except OSError: pass
        if path and path != raw_path and path != db_ref and os.path.exists(path):
            try: os.remove(path)
            except OSError: pass


def _schema_summary(schema):
    return [{"name": name, "rows": info["row_count"], "columns": [{"name": c["name"], "type": c["type"], "pk": c["pk"], "fk": any(fk["from"] == c["name"] for fk in info["foreign_keys"])} for c in info["columns"]], "foreign_keys": info["foreign_keys"]} for name, info in schema["tables"].items()]


def _calc_health(schema):
    score = 100
    for info in schema["tables"].values():
        if not any(c["pk"] for c in info["columns"]): score -= 5
        if info["row_count"] == 0: score -= 5
    return max(0, min(100, score))


_REPORT_ARTIFACTS = (("json", "json_path"), ("html", "html_path"), ("pdf", "pdf_path"),
                     ("rows_csv", "rows_csv"), ("summary_csv", "summary_csv"))


def _run_report(db, job, payload):
    """Generate report artifacts in a throw-away temp dir, move them to durable object storage, record one
    ``reports`` row per artifact and delete the temp files. The result never contains filesystem paths or
    object keys -- only report ids usable with GET /api/reports/{id}/download."""
    import tempfile
    import report_storage
    from db import Report

    row = get_or_create_session(db, job.user_id, payload["session_id"])
    if job.result_ref:
        return json.loads(job.result_ref)
    uploaded_keys: list = []
    created: list = []
    try:
        with tempfile.TemporaryDirectory(prefix="qs-report-") as tmp:
            report = generate_report(
                question=row.question, sql_result=row.sql_result or {"sql": row.sql}, validation_result=row.validation,
                query_result=row.query_result, reliability_result=row.reliability, insight_result=row.insights,
                semantic=row.semantic, output_dir=tmp,
            )
            for kind, field in _REPORT_ARTIFACTS:
                local_path = report.get(field)
                if not local_path or not os.path.isfile(local_path):
                    continue
                key, size = report_storage.store_report_file(local_path, job.user_id, kind)
                uploaded_keys.append(key)
                created.append(Report(user_id=job.user_id, session_id=row.id, key=key, kind=kind, size=size))
        # temp dir (and every local artifact in it) is gone here
        for r in created:
            db.add(r)
        db.commit()
    except Exception:
        db.rollback()
        for key in uploaded_keys:
            report_storage.delete_report_object(key)
        raise
    result = {k: v for k, v in report.items() if k not in {f for _, f in _REPORT_ARTIFACTS}}
    result["session_id"] = row.id
    result["report_ids"] = {r.kind: r.id for r in created}
    result["report_id"] = result["report_ids"].get("json") or (created[0].id if created else None)
    _update_job(db, job, result_ref=json.dumps(result, separators=(",", ":")))
    return result


def _run_email(db, job, payload):
    from email_provider import get_email_provider, send_logged
    send_logged(get_email_provider(), to=payload["to"], subject=payload["subject"], text=payload["text"],
                html=payload.get("html"))
    return {"sent": True}


def _dispatch(db, job, payload):
    if job.kind == "dataset_ingestion": return _run_upload(db, job, payload)
    if job.kind == "report_generation": return _run_report(db, job, payload)
    if job.kind == "auth_email": return _run_email(db, job, payload)
    raise ValueError("Unsupported job kind")


def execute_job(job_id: str):
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job or job.status == "succeeded":
            return
        payload = json.loads(job.result_ref) if job.kind == "_payload" and job.result_ref else None
        # Payloads are stored in Redis job args, while result_ref remains the result field.
        # This branch is intentionally unused; execute_job receives payload through a private cache below.
    finally:
        db.close()
    raise RuntimeError("execute_job must be invoked through _execute_job")


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _execute_job(job_id: str, payload: dict):
    db = SessionLocal()
    started = time.monotonic()
    kind = "unknown"
    outcome = None
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job or job.status == "succeeded":
            return
        kind = job.kind
        obs_context.new_context(None)
        obs_context.bind_job(job.id)
        obs_context.bind_user(job.user_id)
        if job.status == "failed" and job.error_code == STALE_ERROR_CODE:
            # The reaper already failed this job and released its quota; running it now would resurrect a job the user was
            # told had failed. Drop the staged input and stop.
            if job.kind == "dataset_ingestion":
                storage.delete_staged(payload.get("object_ref"), job.user_id)
            outcome = "skipped_stale"
            return
        queue_wait_ms = max(0.0, (_now() - _aware(job.created_at)).total_seconds() * 1000)
        obs_gauges.set_current_job(job.kind)
        _update_job(db, job, status="running", error_code=None)
        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                result = _dispatch(db, job, payload)
                if job.kind == "dataset_ingestion":
                    storage.delete_staged(payload.get("object_ref"), job.user_id)
                _update_job(db, job, status="succeeded", error_code=None, result_ref=json.dumps(result, separators=(",", ":")))
                outcome = "succeeded"
                obs_metrics.record_job(kind, "succeeded", queue_wait_ms, (time.monotonic() - started) * 1000)
                logger.info("job succeeded", extra={"event": "job_succeeded", "kind": kind, "attempts": attempt + 1,
                                                    "queue_wait_ms": round(queue_wait_ms), "duration_ms": round((time.monotonic() - started) * 1000)})
                return result
            except Exception as exc:
                last_error = exc
                db.rollback()
                # Type + fingerprint only (never the message: ingestion errors can quote file content).
                obs_errors.capture_exception(exc, component="worker")
                # A ValueError from dataset ingestion is a permanent validation failure
                # (malformed/oversized/unsupported file). Retrying cannot succeed -- the staged
                # input is consumed by the first attempt -- and would only mask the real,
                # user-actionable message, so fail immediately.
                if job.kind == "dataset_ingestion" and isinstance(exc, ValueError):
                    break
                # A rejected key, unverified sender or missing setting fails the same way every time: retrying only
                # delays the failure (and, for SMTP, held a worker for up to a minute per message).
                if job.kind == "auth_email" and getattr(exc, "transient", True) is False:
                    break
                if attempt + 1 < MAX_ATTEMPTS:
                    _update_job(db, job, status="retrying", error_code="JOB_RETRYING")
                    time.sleep(2 ** attempt)
        if job.kind == "dataset_ingestion":
            storage.delete_staged(payload.get("object_ref"), job.user_id)
        # Only ingestion ValueErrors are user-actionable validation messages. Any other ValueError subclass (e.g.
        # ssl.SSLCertVerificationError from an SMTP job, which is both OSError and ValueError) can carry hostnames and
        # certificate/driver detail and is persisted in jobs.result_ref, so it gets the generic text.
        safe_error = (str(last_error) if job.kind == "dataset_ingestion" and isinstance(last_error, ValueError)
                      else "The background operation could not be completed. Please try again.")
        code = "INVALID_UPLOAD" if job.kind == "dataset_ingestion" and isinstance(last_error, ValueError) else "JOB_FAILED"
        _update_job(db, job, status="failed", error_code=code, result_ref=json.dumps({"error": safe_error}, separators=(",", ":")))
        outcome = "failed"
        # A user's invalid file is the product working, not a platform failure: it does not count against the job SLO.
        obs_metrics.record_job(kind, "rejected" if code == "INVALID_UPLOAD" else "failed", queue_wait_ms, (time.monotonic() - started) * 1000)
        logger.warning("job failed", extra={"event": "job_failed", "kind": kind, "error_code": code})
        return None
    finally:
        obs_gauges.set_current_job(None)
        obs_context.clear()
        db.close()
        # RQ runs each job in a forked work horse that exits right after the job: push buffered metrics before it does.
        try:
            from observability.store import get_store
            get_store().flush()
        except Exception:  # noqa: BLE001
            pass


def reap_stale_jobs(now=None) -> int:
    """Fail background jobs stuck in queued/running/retrying for longer than JOB_STALE_SECONDS (worker killed mid-job, Redis
    flushed, enqueue lost). Failing them releases the quota reservation that in-flight uploads hold (quotas.py counts only
    queued/running), so a lost job can never lock a user out of uploading. Returns how many were reaped."""
    from datetime import timedelta
    cutoff = (now or _now()) - timedelta(seconds=JOB_STALE_SECONDS)
    db = SessionLocal()
    try:
        rows = db.query(Job).filter(Job.status.in_(("queued", "running", "retrying")), Job.updated_at < cutoff).all()
        for job in rows:
            job.status = "failed"
            job.error_code = STALE_ERROR_CODE
            job.result_ref = json.dumps({"error": "The background operation did not complete. Please try again."}, separators=(",", ":"))
            job.updated_at = _now()
            obs_metrics.record_job(job.kind, "failed")
        if rows:
            db.commit()
            obs_events.record_event("stale_jobs_reaped", severity="warning", component="worker", detail={"reaped": len(rows)})
        return len(rows)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("stale job reaper failed", extra={"event": "reaper_failed"})
        return 0
    finally:
        db.close()


def enqueue_job(db, user_id: str, kind: str, payload: dict) -> Job:
    # In-flight uploads count against the owner's storage quota from creation (see quotas.py): record the
    # target session and staged size on the job row itself, inside the same transaction as the quota check.
    is_upload = kind == "dataset_ingestion"
    job = Job(id=str(uuid.uuid4()), user_id=user_id, kind=kind, status="queued", created_at=_now(), updated_at=_now(),
              session_id=payload.get("session_id") if is_upload else None,
              reserved_bytes=payload.get("size") if is_upload else None)
    db.add(job)
    db.commit()
    db.refresh(job)
    try:
        client = _redis()
        if client is None:
            if IS_PRODUCTION:
                raise RuntimeError("Redis is required for background jobs in production")
            _execute_job(job.id, payload)
            db.refresh(job)
            return job
        # A local filesystem object reference is safe only for the inline dev path.
        # Production always uses S3, so a real worker never receives an API-local path.
        if kind == "dataset_ingestion" and storage.BACKEND != "s3":
            if IS_PRODUCTION:
                raise RuntimeError("Production queued uploads require S3-backed durable staging")
            _execute_job(job.id, payload)
            db.refresh(job)
            return job
        q = Queue(QUEUE_NAME, connection=client)
        q.enqueue(_execute_job, job.id, payload, job_timeout="15m")
        return job
    except Exception:
        db.rollback()
        if IS_PRODUCTION:
            _update_job(db, job, status="failed", error_code="QUEUE_UNAVAILABLE")
            raise
        _execute_job(job.id, payload)
        db.refresh(job)
        return job


_worker_seen = {"at": 0.0, "alive": False}
WORKER_CHECK_SECONDS = 30.0


def worker_alive() -> bool:
    """A background worker published its heartbeat recently (cached for WORKER_CHECK_SECONDS)."""
    now = time.monotonic()
    if now - _worker_seen["at"] < WORKER_CHECK_SECONDS:
        return _worker_seen["alive"]
    try:
        alive = any(str(d.get("proc", "")).startswith("worker:") for d in obs_gauges.read_all())
    except Exception:  # noqa: BLE001
        alive = False
    _worker_seen.update(at=now, alive=alive)
    return alive


def dispatch_auth_email(db, user_id: str, payload: dict, background_tasks=None) -> str:
    """Send a verification / reset e-mail without making the HTTP request wait for the mail server.

    "queue": the background worker sends it (needs a running worker). "background": the web process sends it right
    after the response is returned (FastAPI BackgroundTasks), for deployments without a worker such as Render's free
    plan. "auto" (default): queue when a worker is alive, otherwise background. Either way the job row records the
    outcome, the token is already stored, and the link is never written to the database or logs."""
    import config as _cfg
    mode = _cfg.AUTH_EMAIL_DELIVERY
    if mode == "auto":
        mode = "queue" if worker_alive() else "background"
    if mode == "queue" or background_tasks is None:
        enqueue_job(db, user_id, "auth_email", payload)
        return "queue"
    job = Job(id=str(uuid.uuid4()), user_id=user_id, kind="auth_email", status="queued", created_at=_now(), updated_at=_now())
    db.add(job)
    db.commit()
    background_tasks.add_task(_execute_job, job.id, payload)
    return "background"
