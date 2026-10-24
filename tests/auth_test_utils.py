from __future__ import annotations

import re


def _latest_auth_job(email: str):
    from db import Job, SessionLocal, User

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).one()
        job = (
            db.query(Job)
            .filter(Job.user_id == user.id, Job.kind == "auth_email")
            .order_by(Job.created_at.desc())
            .first()
        )
        assert job is not None, f"No auth email job found for {email}"
        return job.id, job.status, job.created_at, user.id


def _find_rq_job_for_app_job(queue, app_job_id: str):
    """Find the RQ wrapper whose first argument is the persisted application Job.id.

    ``enqueue_job`` persists its own UUID and then calls ``Queue.enqueue`` without passing
    ``job_id=...``. RQ therefore assigns a separate wrapper ID. The application Job.id is the
    stable correlation key; it is not the RQ job ID.
    """
    for rq_job_id in queue.get_job_ids():
        rq_job = queue.fetch_job(rq_job_id)
        if rq_job and rq_job.args and rq_job.args[0] == app_job_id:
            return rq_job
    return None


def _queued_payload_token(job_id: str, *, kind: str) -> str | None:
    """Read the raw token from the exact queued auth-email payload, when still available.

    This is test-only correlation data. Production never stores or logs the raw token in the
    database; the helper uses the queued job payload to identify the exact message for the
    registration/reset operation under test.
    """
    from jobs import QUEUE_NAME
    from config import REDIS_URL
    import redis
    from rq import Queue

    if not REDIS_URL:
        # Development inline fallback: no queue exists; the job already ran and the
        # token is recovered from the SMTP capture by the caller.
        return None
    try:
        connection = redis.Redis.from_url(REDIS_URL)
        queue = Queue(QUEUE_NAME, connection=connection)
        rq_job = _find_rq_job_for_app_job(queue, job_id)
    except redis.exceptions.RedisError:
        return None
    if rq_job is None or len(rq_job.args) < 2:
        return None
    payload = rq_job.args[1]
    text = str(payload.get("text", "")) if isinstance(payload, dict) else ""
    marker = "verify_token=" if kind == "verification" else "reset_token="
    match = re.search(re.escape(marker) + r"([A-Za-z0-9_-]+)", text)
    return match.group(1) if match else None


def drain_queued_auth_email_for_email(email: str) -> str:
    """Execute the newest auth-email RQ job for an address deterministically.

    Tests execute the same ``_execute_job`` callable used by the real worker. The helper locates
    the RQ wrapper by the persisted application Job.id rather than assuming the two IDs are equal.
    """
    from jobs import QUEUE_NAME
    from config import REDIS_URL
    import redis
    from rq import Queue

    job_id, status, _, _ = _latest_auth_job(email)
    if status in {"succeeded", "failed"}:
        return job_id
    assert REDIS_URL, f"Auth email job {job_id} is {status!r} but no REDIS_URL/queue is configured"

    connection = redis.Redis.from_url(REDIS_URL)
    queue = Queue(QUEUE_NAME, connection=connection)
    rq_job = _find_rq_job_for_app_job(queue, job_id)
    assert rq_job is not None, f"Queued auth email job {job_id} is no longer available"
    assert rq_job.args and rq_job.args[0] == job_id, f"RQ job payload does not match DB job {job_id}"
    try:
        rq_job.perform()
    finally:
        rq_job.delete()
    return job_id


def _captured_messages():
    from tests.conftest import SMTP_CAPTURE
    return SMTP_CAPTURE.messages()


def _decoded_message_text(raw: str) -> str:
    """Return the decoded text of a captured SMTP message.

    The provider emits RFC-compliant MIME; long lines (e.g. the verification URL) are
    quoted-printable encoded on the wire (``=3D``, soft line breaks). Real mail clients
    decode this, so the test helper must as well rather than regex the raw wire bytes.
    """
    from email import message_from_string, policy

    msg = message_from_string(raw, policy=policy.default)
    parts = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        try:
            parts.append(part.get_content())
        except Exception:
            payload = part.get_payload(decode=True) or b""
            parts.append(payload.decode("utf-8", errors="replace"))
    return "\n".join(str(p) for p in parts) if parts else raw


def _token_hash_matches_current_record(email: str, token: str, *, kind: str) -> bool:
    from auth import hash_token
    from db import EmailVerificationToken, PasswordResetToken, SessionLocal, User

    model = EmailVerificationToken if kind == "verification" else PasswordResetToken
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).one()
        row = (
            db.query(model)
            .filter(model.user_id == user.id)
            .order_by(model.created_at.desc())
            .first()
        )
        return bool(row and row.used_at is None and hash_token(token) == row.token_hash)


def latest_token_for_email(email: str, *, kind: str) -> str:
    """Return the raw token from the SMTP message for the current auth operation.

    Selection is based on the current DB token hash, not merely recipient or message order.
    This prevents a delayed/stale SMTP delivery for the same address from being mistaken for
    the token generated by the current registration/resend/reset operation.
    """
    job_id, _, _, _ = _latest_auth_job(email)
    expected = _queued_payload_token(job_id, kind=kind)
    drain_queued_auth_email_for_email(email)

    marker = "verify_token=" if kind == "verification" else "reset_token="
    needle = email.lower()
    candidates = []
    for message in reversed(_captured_messages()):
        recipients = {value.lower() for value in message["recipients"]}
        if needle not in recipients:
            continue
        match = re.search(re.escape(marker) + r"([A-Za-z0-9_-]+)", _decoded_message_text(message["data"]))
        if match:
            token = match.group(1)
            candidates.append(token)
            if expected and token == expected:
                return token
            if _token_hash_matches_current_record(email, token, kind=kind):
                return token

    if expected and _token_hash_matches_current_record(email, expected, kind=kind):
        return expected

    raise AssertionError(
        f"No current {kind} email found for {email}; captured {len(candidates)} candidate message(s)"
    )


def verify_registered_email(client, email: str) -> None:
    token = latest_token_for_email(email, kind="verification")
    response = client.post("/api/auth/verify-email", params={"token": token})
    assert response.status_code == 200, response.text
