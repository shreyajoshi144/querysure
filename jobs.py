"""Redis/RQ-backed background jobs for heavy ingestion, reports and email.

RQ is used because it is deliberately small, Redis-native, easy to operate with
an existing Redis deployment, and provides a dedicated worker process without
introducing another broker or service architecture.

Deployments without a worker (Render's free plan) run the same jobs in the web process right after the response
(JOB_EXECUTION=in_process, or auto with no worker heartbeat). Those jobs are not durable: they live only as long as the
process, so they keep a heartbeat on their row, and a job whose heartbeat stops with no worker alive is failed as
JOB_INTERRUPTED instead of staying queued.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import redis
from rq import Queue

import logging

import storage
import config
from config import REDIS_URL, IS_PRODUCTION, JOB_STALE_SECONDS
from observability import context as obs_context, errors as obs_errors, events as obs_events, gauges as obs_gauges
from observability import metrics as obs_metrics
from db import Job, PipelineSession, SessionLocal, get_or_create_session, update_session
from modules import load_database, build_semantic_layer, build_vector_store, clear_cache_key, generate_report

QUEUE_NAME = "querysure"
MAX_ATTEMPTS = 3
logger = logging.getLogger("querysure.jobs")
STALE_ERROR_CODE = "JOB_STALE"
INTERRUPTED_ERROR_CODE = "JOB_INTERRUPTED"
INTERRUPTED_MESSAGE = ("The operation stopped before it finished (the server restarted or no background worker is running). "
                       "Please try again.")
ACTIVE_STATUSES = ("queued", "running", "retrying")
# Failed by the reaper / orphan check while possibly still running somewhere: such a job's late result is discarded.
DEAD_ERROR_CODES = (STALE_ERROR_CODE, INTERRUPTED_ERROR_CODE)


class JobAbandoned(Exception):
    """The job was reported as failed (JOB_STALE / JOB_INTERRUPTED) while it was still running: the user was told to retry,
    so its late result must not switch their dataset, add reports or overwrite the failure."""


def _abandoned(db, job) -> bool:
    db.refresh(job)
    return job.status == "failed" and job.error_code in DEAD_ERROR_CODES
# Jobs that load a whole dataset or render a report: one at a time per process when run in-process (memory).
HEAVY_KINDS = {"dataset_ingestion", "report_generation"}


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
    import workspaces
    workspaces.forget_storage_ref(db, old_ref)


_FILE_FORMATS = {".csv": "csv", ".json": "json", ".xlsx": "xlsx", ".xlsm": "xlsm",
                 ".db": "sqlite", ".sqlite": "sqlite", ".sqlite3": "sqlite"}


def _run_connection_sync(db, job, payload):
    """Import (or incrementally sync) one table from a saved database connection (ingest_db.py)."""
    import audit
    import ingest_db
    import ingest_records
    import quotas
    from connectors import registry
    from db import Connection, Dataset, User
    user = db.get(User, job.user_id)
    connection = db.get(Connection, payload["connection_id"])
    dataset_id = payload.get("dataset_id")
    workspace_id = payload["workspace_id"]
    if connection is None:
        raise ValueError("The connection no longer exists.")
    dataset = db.get(Dataset, dataset_id) if dataset_id else None
    if dataset_id and dataset is None:
        raise ValueError("Dataset not found.")

    def abort():
        if _abandoned(db, job):
            raise JobAbandoned()

    def quota_check(new_bytes, target):
        creator = db.get(User, target.created_by) or user
        try:
            quotas.check_dataset_replacement(db, creator, new_bytes, target.id)
        except quotas.QuotaExceeded as exc:
            import ingest
            raise ingest.IngestQuota(exc.message) from exc

    audit.record(workspace_id, "ingestion.start", actor_id=job.user_id, target_type="dataset", target_id=dataset_id,
                 details={"kind": payload["mode"], "dataset_id": dataset_id})
    pipeline = ingest_records.sync if getattr(registry.source(connection.connector), "pipeline", "keyset") != "keyset" else ingest_db.sync
    try:
        res = pipeline(db, user=user, workspace_id=workspace_id, connection=connection, table_name=payload["table"],
                             requested_mode=payload["mode"], dataset=dataset, watermark_column=payload.get("watermark_column"),
                             name=payload.get("name"), job_id=job.id, abort=abort, quota_check=quota_check)
    except JobAbandoned:
        raise
    except Exception as exc:
        audit.record(workspace_id, "ingestion.fail", actor_id=job.user_id, target_type="dataset", target_id=dataset_id,
                     details={"error_code": getattr(exc, "code", None) or "IMPORT_FAILED"})
        raise
    ds, run = res.dataset, res.run
    audit.record(ds.workspace_id, "ingestion.finish", actor_id=job.user_id, target_type="dataset", target_id=ds.id,
                 details={"run_id": run.id, "connector": run.connector, "dataset_id": ds.id, "inserted": run.records_inserted,
                          "updated": run.records_updated, "skipped": run.records_skipped, "rejected": run.records_rejected,
                          "mode": run.mode, "fallback_reason": run.fallback_reason, "status": run.status, "version": ds.version})
    if run.schema_changes:
        audit.record(ds.workspace_id, "ingestion.schema_change", actor_id=job.user_id, target_type="dataset", target_id=ds.id,
                     details={"run_id": run.id, "schema_changes": len(run.schema_changes)})
    if not dataset_id:
        audit.record(ds.workspace_id, "dataset.upload", actor_id=job.user_id, target_type="dataset", target_id=ds.id,
                     details={"format": ds.file_format, "tables": ds.table_count, "rows": ds.row_count, "bytes": ds.bytes})
    return {"dataset_id": ds.id, "workspace_id": ds.workspace_id, "run_id": run.id, "run_status": run.status, "mode": run.mode,
            "fallback_reason": run.fallback_reason, "records_read": run.records_read, "records_inserted": run.records_inserted,
            "records_updated": run.records_updated, "records_skipped": run.records_skipped,
            "records_rejected": run.records_rejected}


def _run_upload(db, job, payload):
    """Import an uploaded source through the layered pipeline (ingest.py): a new dataset (optionally linked to the uploading
    session) or, with ``dataset_id`` in the payload, a new version of an existing one. The session link is the last step: a
    failure there leaves a valid, openable dataset behind, never a half-imported one."""
    if payload.get("connection_id"):
        return _run_connection_sync(db, job, payload)
    import audit
    import ingest
    import workspaces
    from db import Dataset, IngestionRun, User
    ext, fname = payload["ext"], payload["fname"]
    session_id, dataset_id = payload.get("session_id"), payload.get("dataset_id")
    user = db.get(User, job.user_id)
    workspace_id = payload.get("workspace_id") or workspaces.personal_workspace_id(job.user_id)
    saved = json.loads(job.result_ref) if job.result_ref else {}
    raw_path = None
    try:
        ds = db.get(Dataset, saved["dataset_id"]) if saved.get("dataset_id") else None
        if ds is None or ds.status != "ready":
            audit.record(workspace_id, "ingestion.start", actor_id=job.user_id, target_type="dataset", target_id=dataset_id,
                         details={"kind": "reimport" if dataset_id else "new", "dataset_id": dataset_id})
            raw_path = storage.ensure_staged_local(payload["object_ref"], job.user_id, ext)

            def abort():
                if _abandoned(db, job):
                    raise JobAbandoned()

            def quota_check(new_bytes, target):
                import quotas
                creator = db.get(User, target.created_by) or user
                try:
                    quotas.check_dataset_replacement(db, creator, new_bytes, target.id)
                except quotas.QuotaExceeded as exc:
                    raise ingest.IngestQuota(exc.message) from exc

            try:
                res = ingest.import_source(db, user=user, workspace_id=workspace_id, dataset_id=dataset_id, raw_path=raw_path,
                                           ext=ext, fname=fname, job_id=job.id, abort=abort,
                                           quota_check=quota_check if dataset_id else None)
            except JobAbandoned:
                raise
            except Exception as exc:
                audit.record(workspace_id, "ingestion.fail", actor_id=job.user_id, target_type="dataset", target_id=dataset_id,
                             details={"error_code": getattr(exc, "code", None) or ("INVALID_SOURCE" if isinstance(exc, ValueError)
                                                                                   else "IMPORT_FAILED")})
                raise
            ds, run = res.dataset, res.run
            _update_job(db, job, result_ref=json.dumps({"dataset_id": ds.id, "run_id": run.id}, separators=(",", ":")))
            ev = {"run_id": run.id, "connector": run.connector, "dataset_id": ds.id, "inserted": run.records_inserted,
                  "rejected": run.records_rejected, "status": run.status, "version": ds.version}
            audit.record(ds.workspace_id, "ingestion.finish", actor_id=job.user_id, target_type="dataset", target_id=ds.id, details=ev)
            if run.schema_changes:
                audit.record(ds.workspace_id, "ingestion.schema_change", actor_id=job.user_id, target_type="dataset",
                             target_id=ds.id, details={"run_id": run.id, "schema_changes": len(run.schema_changes)})
            if not dataset_id:
                audit.record(ds.workspace_id, "dataset.upload", actor_id=job.user_id, target_type="dataset", target_id=ds.id,
                             details={"format": ds.file_format, "tables": ds.table_count, "rows": ds.row_count, "bytes": ds.bytes,
                                      "session_id": session_id})
        run = (db.query(IngestionRun).filter(IngestionRun.id == saved["run_id"]).first() if saved.get("run_id") else None) or \
            db.query(IngestionRun).filter(IngestionRun.dataset_id == ds.id).order_by(IngestionRun.started_at.desc()).first()
        result = {"dataset_id": ds.id, "workspace_id": ds.workspace_id, "run_id": run.id if run else None,
                  "run_status": run.status if run else None, "records_read": run.records_read if run else None,
                  "records_rejected": run.records_rejected if run else None}
        if not session_id:
            return result
        try:
            row = get_or_create_session(db, job.user_id, session_id)
            old_ref = row.db_path
            schema = load_database(storage.ensure_local_workspace(ds.storage_ref))
            semantic = build_semantic_layer(schema)
            clear_cache_key(row.id)
            build_vector_store(semantic, cache_key=row.id)
            row = update_session(db, row, db_path=ds.storage_ref, dataset_bytes=None, dataset_id=ds.id, dataset_version=ds.version,
                                 schema=schema, semantic=semantic, retrieval=None, sql=None, sql_result=None, question=None,
                                 validation=None, query_result=None, reliability=None, insights=None)
        except Exception:
            db.rollback()
            if not dataset_id:   # the user is told this upload failed: do not leave its dataset behind, unseen and counted
                try:
                    storage.delete_workspace_tree(ds.storage_ref)
                    ingest.discard_dataset_row(db, ds)
                except Exception:  # noqa: BLE001 - the original failure is what matters
                    db.rollback()
            raise
        _cleanup_superseded(db, old_ref, ds.storage_ref, row.id)
        return {"session_id": row.id, "db_name": fname, "table_count": schema["table_count"],
                "total_rows": schema["total_rows"], "relationships": len(schema["relationships"]),
                "db_size": schema["db_size"], "tables": _schema_summary(schema),
                "health_score": _calc_health(schema), **result}
    finally:
        if raw_path and os.path.exists(raw_path):
            try: os.remove(raw_path)
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
        if _abandoned(db, job):
            raise JobAbandoned()
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
    _audit_report(db, job.user_id, row, result["report_id"], len(created))
    return result


def _audit_report(db, user_id, row, report_id, artifact_count):
    """Audit a generated report in the workspace that catalogs the session's dataset (personal otherwise)."""
    try:
        import audit
        import workspaces
        from db import Dataset
        ds = db.get(Dataset, row.dataset_id) if row.dataset_id else None
        ws_id = ds.workspace_id if ds is not None else workspaces.personal_workspace_id(user_id)
        audit.record(ws_id, "report.create", actor_id=user_id, target_type="report", target_id=report_id,
                     details={"count": artifact_count, "session_id": row.id, "dataset_id": ds.id if ds is not None else None})
    except Exception as exc:  # noqa: BLE001 - auditing must never fail a report
        logger.warning("report audit failed: %s", type(exc).__name__)


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
        if job.status == "failed" and job.error_code in DEAD_ERROR_CODES:
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
                if _abandoned(db, job):
                    raise JobAbandoned()
                _update_job(db, job, status="succeeded", error_code=None, result_ref=json.dumps(result, separators=(",", ":")))
                outcome = "succeeded"
                obs_metrics.record_job(kind, "succeeded", queue_wait_ms, (time.monotonic() - started) * 1000)
                logger.info("job succeeded", extra={"event": "job_succeeded", "kind": kind, "attempts": attempt + 1,
                                                    "queue_wait_ms": round(queue_wait_ms), "duration_ms": round((time.monotonic() - started) * 1000)})
                return result
            except JobAbandoned:
                db.rollback()
                if job.kind == "dataset_ingestion":
                    storage.delete_staged(payload.get("object_ref"), job.user_id)
                outcome = "abandoned"
                logger.warning("late result of a failed job discarded", extra={"event": "job_abandoned", "kind": kind})
                return None
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
                    if _abandoned(db, job):
                        break
                    _update_job(db, job, status="retrying", error_code="JOB_RETRYING")
                    time.sleep(2 ** attempt)
        if job.kind == "dataset_ingestion":
            storage.delete_staged(payload.get("object_ref"), job.user_id)
        if _abandoned(db, job):
            outcome = "abandoned"
            return None
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
        return len(rows) + fail_orphaned_jobs(db, now=now)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("stale job reaper failed", extra={"event": "reaper_failed"})
        return 0
    finally:
        db.close()


def enqueue_job(db, user_id: str, kind: str, payload: dict, background_tasks=None) -> Job:
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
        if execution_mode() == "in_process":
            _start_in_process(job, payload, background_tasks)
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
    _start_in_process(job, payload, background_tasks)
    return "background"


def execution_mode() -> str:
    """Where the next upload / report job runs: "queue" or "in_process" (JOB_EXECUTION, with auto resolved)."""
    mode = config.JOB_EXECUTION
    if mode == "auto":
        return "queue" if worker_alive() else "in_process"
    return mode


# Jobs this process has accepted to run in-process and not yet finished (job id -> monotonic time accepted). Their rows get a
# heartbeat (updated_at) so other processes can tell a live in-process job from one lost in a restart. An entry older than
# JOB_STALE_SECONDS (longer than any job may run) gets no more heartbeats -- e.g. a background task Starlette never started
# because the client disconnected -- so the orphan check fails it instead of it staying "queued" forever.
_inflight: dict = {}
_inflight_lock = threading.Lock()
_heavy_slots = {"sem": None}
_heartbeat = {"thread": None, "stop": threading.Event()}


def _heavy_semaphore() -> threading.Semaphore:
    with _inflight_lock:
        if _heavy_slots["sem"] is None:
            _heavy_slots["sem"] = threading.BoundedSemaphore(config.IN_PROCESS_JOB_CONCURRENCY)
        return _heavy_slots["sem"]


def _run_in_process(job_id: str, payload: dict, kind: str):
    try:
        if kind in HEAVY_KINDS:
            with _heavy_semaphore():
                return _execute_job(job_id, payload)
        return _execute_job(job_id, payload)
    finally:
        with _inflight_lock:
            _inflight.pop(job_id, None)


def _start_in_process(job, payload: dict, background_tasks) -> None:
    """Run `job` in this process: after the HTTP response when FastAPI BackgroundTasks are given, otherwise now."""
    with _inflight_lock:
        _inflight[job.id] = time.monotonic()
    _ensure_heartbeat()
    logger.info("job runs in-process", extra={"event": "job_in_process", "kind": job.kind})
    if background_tasks is None:
        _run_in_process(job.id, payload, job.kind)
    else:
        background_tasks.add_task(_run_in_process, job.id, payload, job.kind)


def _live_inflight_ids() -> list:
    oldest = time.monotonic() - JOB_STALE_SECONDS
    with _inflight_lock:
        return [job_id for job_id, accepted in _inflight.items() if accepted >= oldest]


def touch_inflight_jobs() -> int:
    """Refresh the heartbeat of this process's unfinished in-process jobs. Returns how many rows were touched."""
    ids = _live_inflight_ids()
    if not ids:
        return 0
    db = SessionLocal()
    try:
        n = (db.query(Job).filter(Job.id.in_(ids), Job.status.in_(ACTIVE_STATUSES))
             .update({Job.updated_at: _now()}, synchronize_session=False))
        db.commit()
        return n
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("job heartbeat failed", extra={"event": "job_heartbeat_failed"})
        return 0
    finally:
        db.close()


def _heartbeat_loop(stop: threading.Event) -> None:
    # Event.wait, not time.sleep: the retry back-off in _execute_job uses time.sleep, and a shared sleep would couple the two
    # (a test that patches time.sleep made this thread spin and recorded its 15 s waits as retry back-offs in CI).
    while not stop.wait(config.JOB_HEARTBEAT_SECONDS):
        touch_inflight_jobs()


def _ensure_heartbeat() -> None:
    with _inflight_lock:
        if _heartbeat["thread"] is not None:
            return

        _heartbeat["thread"] = threading.Thread(target=_heartbeat_loop, args=(_heartbeat["stop"],), name="qs-job-heartbeat",
                                                daemon=True)
        _heartbeat["thread"].start()


def fail_orphaned_jobs(db, job_id: str | None = None, now=None) -> int:
    """Fail queued/running jobs that nothing can finish: no worker is alive and the row's heartbeat is older than
    JOB_ORPHAN_SECONDS (an in-process job lost in a restart, or a job queued for a worker that is not running).
    Releases upload quota reservations like the stale reaper. One conditional UPDATE, so a job that heartbeats or finishes
    concurrently is never overwritten. Returns how many jobs were failed."""
    if worker_alive():
        return 0
    cutoff = (now or _now()) - timedelta(seconds=config.JOB_ORPHAN_SECONDS)
    mine = _live_inflight_ids()
    q = db.query(Job).filter(Job.status.in_(ACTIVE_STATUSES), Job.updated_at < cutoff)
    if job_id is not None:
        q = q.filter(Job.id == job_id)
    if mine:
        q = q.filter(~Job.id.in_(mine))
    found = q.with_entities(Job.id, Job.kind).all()
    kinds = [k for _, k in found]
    if not kinds:
        return 0
    n = q.update({Job.status: "failed", Job.error_code: INTERRUPTED_ERROR_CODE,
                  Job.result_ref: json.dumps({"error": INTERRUPTED_MESSAGE}, separators=(",", ":")),
                  Job.updated_at: _now()}, synchronize_session=False)
    db.commit()
    for kind in kinds[:n]:
        obs_metrics.record_job(kind, "failed")
    try:  # a run whose job died is interrupted now, which frees the dataset for the next import
        import ingest
        ingest.mark_runs_interrupted(db, [i for i, k in found if k == "dataset_ingestion"])
    except Exception:  # noqa: BLE001
        db.rollback()
    logger.warning("orphaned jobs failed", extra={"event": "jobs_interrupted", "count": n})
    return n
