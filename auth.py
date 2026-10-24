"""QuerySure authentication primitives.

Keeps the existing bcrypt + bearer JWT contract and adds email verification,
password reset, and token-version invalidation without introducing a second
identity/session system.
"""
from __future__ import annotations

import hashlib
import re
import json
import secrets
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
import jwt
from jwt import PyJWTError as JWTError  # PyJWT (replaced python-jose, which has unfixed advisories)
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.orm import Session as OrmSession

from collections import defaultdict  # noqa: F401  (patched/inspected by tests)
import config as _cfg
from config import JWT_SECRET_KEY, JWT_ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES, GOOGLE_CLIENT_ID, REDIS_URL
from db import AuthSession, User, get_db


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)
_MAX_PASSWORD_BYTES = 72
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_GOOGLE_ONLY_PREFIX = "GOOGLE_ONLY$"
_DUMMY_BCRYPT_HASH = "$2b$12$mj3YGOssatiTR2TLcDw7l.aFLsI.OgukIgbvH77zqUT6RvnIBsEwO"
_LOGIN_THROTTLE_TTL = 300
_LOGIN_MAX_BACKOFF = 30
_login_lock = threading.Lock()
_login_failures: dict[str, tuple[int, float]] = {}
_login_redis = None
_login_redis_failed = False

def _login_redis_client():
    global _login_redis, _login_redis_failed
    if _login_redis_failed or not REDIS_URL:
        return None
    if _login_redis is None:
        try:
            import redis
            _login_redis = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=0.25)
        except Exception:
            _login_redis_failed = True
            return None
    return _login_redis

def _login_key(email: str) -> str:
    return "querysure:login-fail:" + hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()

def login_throttle_remaining(email: str) -> int:
    key = _login_key(email)
    now = time.time()
    client = _login_redis_client()
    try:
        if client:
            raw = client.get(key)
            if raw:
                item = json.loads(raw)
                count = int(item.get("count", 0))
                last = float(item.get("last", 0))
                return max(0, int(min(_LOGIN_MAX_BACKOFF, 2 ** max(0, count - 1)) - (now - last)))
    except Exception:
        pass
    with _login_lock:
        count, last = _login_failures.get(key, (0, 0.0))
        remaining = max(0, int(min(_LOGIN_MAX_BACKOFF, 2 ** max(0, count - 1)) - (now - last)))
        if now - last > _LOGIN_THROTTLE_TTL:
            _login_failures.pop(key, None)
            return 0
        return remaining

def record_login_failure(email: str) -> None:
    key = _login_key(email)
    now = time.time()
    client = _login_redis_client()
    try:
        if client:
            raw = client.get(key)
            item = json.loads(raw) if raw else {}
            count = int(item.get("count", 0)) + 1
            client.setex(key, _LOGIN_THROTTLE_TTL, json.dumps({"count": count, "last": now}))
            return
    except Exception:
        pass
    with _login_lock:
        count, _ = _login_failures.get(key, (0, 0.0))
        _login_failures[key] = (count + 1, now)

def clear_login_failures(email: str) -> None:
    key = _login_key(email)
    client = _login_redis_client()
    try:
        if client:
            client.delete(key)
            return
    except Exception:
        pass
    with _login_lock:
        _login_failures.pop(key, None)



def normalize_email(email: str) -> str:
    return str(email).strip().lower()


def _validate_password(v: str) -> str:
    if len(v) < 12:
        raise ValueError("Password must be at least 12 characters long.")
    if len(v.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        raise ValueError(f"Password must be at most {_MAX_PASSWORD_BYTES} bytes long.")
    if not re.search(r"[a-z]", v) or not re.search(r"[A-Z]", v):
        raise ValueError("Password must contain uppercase and lowercase letters.")
    if not re.search(r"\d", v) or not re.search(r"[^A-Za-z0-9]", v):
        raise ValueError("Password must contain a number and a special character.")
    return v


class UserCreate(BaseModel):
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def normalize_and_validate_email(cls, v: EmailStr) -> str:
        value = normalize_email(str(v))
        if not _EMAIL_RE.fullmatch(value):
            raise ValueError("Enter a valid email address.")
        return value

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        return _validate_password(v)


class EmailRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalize_email_value(cls, v: EmailStr) -> str:
        return normalize_email(str(v))


class LoginRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email_value(cls, v: EmailStr) -> str:
        return normalize_email(str(v))


class PasswordRequest(BaseModel):
    password: str

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        return _validate_password(v)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    email: str
    email_verified: bool
    plan: str = "free"
    created_at: datetime

    class Config:
        from_attributes = True


def hash_password(password: str) -> str:
    pw_bytes = password.encode("utf-8")
    if len(pw_bytes) > _MAX_PASSWORD_BYTES:
        raise ValueError(f"Password must be at most {_MAX_PASSWORD_BYTES} bytes long.")
    return bcrypt.hashpw(pw_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        if hashed.startswith(_GOOGLE_ONLY_PREFIX):
            bcrypt.checkpw(plain.encode("utf-8"), _DUMMY_BCRYPT_HASH.encode("utf-8"))
            return False
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(
    subject: str,
    expires_minutes: Optional[int] = None,
    auth_version: int = 0,
    session_id: Optional[str] = None,
) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes if expires_minutes is not None else ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {"sub": subject, "exp": expire, "ver": auth_version}
    if session_id:
        # ``sid`` ties the token to a server-side AuthSession row, which is what makes it revocable.
        payload["sid"] = session_id
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_token_claims(token: str) -> dict:
    """Verified claims of an access token (signature + expiry checked). Raises 401 for anything invalid."""
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    if not payload.get("sub"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
    try:
        payload["ver"] = int(payload.get("ver", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
    sid = payload.get("sid")
    if sid is not None and (not isinstance(sid, str) or len(sid) > 64):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
    return payload


def decode_token(token: str) -> tuple[str, int]:
    claims = decode_token_claims(token)
    return str(claims["sub"]), claims["ver"]


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    """Aware-UTC view of a stored datetime (SQLite returns naive values for DateTime(timezone=True))."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Server-side sessions (revocable tokens)
# ---------------------------------------------------------------------------
def issue_session_token(db: OrmSession, user: User, expires_minutes: Optional[int] = None) -> str:
    """Create an AuthSession row and a token bound to it. Commits. Keeps at most AUTH_MAX_ACTIVE_SESSIONS live sessions per
    user (oldest revoked first) and prunes long-dead rows for this user, so the table stays bounded."""
    now = datetime.now(timezone.utc)
    minutes = expires_minutes if expires_minutes is not None else ACCESS_TOKEN_EXPIRE_MINUTES
    session = AuthSession(user_id=user.id, expires_at=now + timedelta(minutes=minutes))
    db.add(session)
    db.flush()
    stale_before = now - timedelta(days=1)
    db.query(AuthSession).filter(
        AuthSession.user_id == user.id,
        ((AuthSession.expires_at < stale_before) | (AuthSession.revoked_at < stale_before)),
    ).delete(synchronize_session=False)
    live = (
        db.query(AuthSession.id)
        .filter(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None), AuthSession.expires_at > now)
        .order_by(AuthSession.created_at.desc(), AuthSession.id)
        .all()
    )
    overflow = [row.id for row in live[_cfg.AUTH_MAX_ACTIVE_SESSIONS:]]
    if overflow:
        db.query(AuthSession).filter(AuthSession.id.in_(overflow)).update(
            {AuthSession.revoked_at: now, AuthSession.revoked_reason: "limit"}, synchronize_session=False
        )
    token = create_access_token(
        subject=user.id, expires_minutes=minutes, auth_version=int(user.auth_version or 0), session_id=session.id
    )
    db.commit()
    return token


def revoke_session(db: OrmSession, session_id: Optional[str], user_id: str, reason: str = "logout") -> bool:
    """Revoke one session of ``user_id``. Idempotent; returns whether a live session was revoked."""
    if not session_id:
        return False
    updated = db.query(AuthSession).filter(
        AuthSession.id == session_id, AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
    ).update({AuthSession.revoked_at: datetime.now(timezone.utc), AuthSession.revoked_reason: reason}, synchronize_session=False)
    db.commit()
    return bool(updated)


def revoke_all_sessions(db: OrmSession, user: User, reason: str = "logout_all", commit: bool = True) -> None:
    """Invalidate EVERY token of this user: bump auth_version (kills pre-Sprint-5 tokens too) and revoke all session rows."""
    user.auth_version = int(user.auth_version or 0) + 1
    db.query(AuthSession).filter(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)).update(
        {AuthSession.revoked_at: datetime.now(timezone.utc), AuthSession.revoked_reason: reason}, synchronize_session=False
    )
    if commit:
        db.commit()


def purge_dead_sessions(db: OrmSession, *, now: Optional[datetime] = None, older_than_days: int = 1) -> int:
    """Delete sessions that expired or were revoked more than ``older_than_days`` ago (maintenance job)."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=older_than_days)
    deleted = db.query(AuthSession).filter(
        (AuthSession.expires_at < cutoff) | (AuthSession.revoked_at < cutoff)
    ).delete(synchronize_session=False)
    db.commit()
    return int(deleted or 0)


_GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
_GOOGLE_CLOCK_SKEW_SECONDS = 60
_GOOGLE_REFRESH_MIN_INTERVAL = 30.0  # throttle forced refreshes so unknown kids cannot hammer Google
_google_certs_lock = threading.Lock()
_google_certs: dict[str, object] = {}
_google_certs_expires_at = 0.0
_google_certs_last_fetch = 0.0


class GoogleTokenError(ValueError):
    """The credential is not a valid Google ID token for this deployment (client error, 401)."""


class GoogleUnavailableError(RuntimeError):
    """Google's signing keys could not be fetched (upstream problem, 503). Not the caller's fault."""


def _fetch_google_certs() -> tuple[dict[str, object], int]:
    request = urllib.request.Request(
        _GOOGLE_CERTS_URL,
        headers={"User-Agent": "QuerySure/2.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            cache_control = response.headers.get("Cache-Control", "")
    except Exception as exc:
        raise GoogleUnavailableError("Could not fetch Google signing keys") from exc
    certs = payload.get("keys") if isinstance(payload, dict) else None
    if certs is None and isinstance(payload, dict):
        certs = payload.get("certs")
    if isinstance(certs, list):
        snapshot = {item.get("kid"): item for item in certs if isinstance(item, dict) and item.get("kid")}
    elif isinstance(certs, dict):
        snapshot = certs
    else:
        raise GoogleUnavailableError("Google certificate response is invalid")
    if not snapshot:
        raise GoogleUnavailableError("Google certificate response contained no keys")
    max_age = 300
    match = re.search(r"max-age=(\d+)", cache_control)
    if match:
        max_age = max(60, min(int(match.group(1)), 86400))
    return snapshot, max_age


def _google_public_key(key):
    """RSA public key from a Google key entry: a JWK dict (v3 certs endpoint) or a PEM certificate/public key."""
    if isinstance(key, dict):
        return jwt.PyJWK(key, algorithm="RS256").key
    data = key.encode("utf-8") if isinstance(key, str) else bytes(key)
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    if b"BEGIN CERTIFICATE" in data:
        return x509.load_pem_x509_certificate(data).public_key()
    return serialization.load_pem_public_key(data)


def _google_certs_snapshot(force: bool = False) -> dict[str, object]:
    """Return Google's signing keys, cached per Cache-Control. ``force`` re-fetches (key rotation) but is throttled."""
    global _google_certs, _google_certs_expires_at, _google_certs_last_fetch
    now = time.time()
    if _google_certs and now < _google_certs_expires_at and not force:
        return _google_certs
    with _google_certs_lock:
        now = time.time()
        if _google_certs and now < _google_certs_expires_at and not force:
            return _google_certs
        if force and _google_certs and now - _google_certs_last_fetch < _GOOGLE_REFRESH_MIN_INTERVAL:
            return _google_certs
        try:
            snapshot, max_age = _fetch_google_certs()
        except GoogleUnavailableError:
            if _google_certs:
                # Serve stale keys briefly rather than failing every sign-in during a short Google outage.
                _google_certs_expires_at = now + 30
                _google_certs_last_fetch = now
                return _google_certs
            raise
        _google_certs = snapshot
        _google_certs_expires_at = now + max_age
        _google_certs_last_fetch = now
        return snapshot


def verify_google_id_token(credential: str) -> dict:
    """Verify a Google Identity Services ID token. Returns the verified claims.

    Checks: RS256 signature against Google's published keys (selected by ``kid``), issuer, audience == configured
    GOOGLE_CLIENT_ID, ``azp`` (when present) == GOOGLE_CLIENT_ID, ``exp``/``nbf``/``iat`` with a small clock-skew
    allowance, a stable ``sub``, and a verified ``email``.

    Raises GoogleTokenError (invalid credential), GoogleUnavailableError (Google keys unreachable), or ValueError
    ("not configured") when GOOGLE_CLIENT_ID is unset.
    """
    if not GOOGLE_CLIENT_ID:
        raise ValueError("Google sign-in is not configured")
    token = (credential or "").strip()
    if not token:
        raise GoogleTokenError("Missing Google ID token")
    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:
        raise GoogleTokenError("Malformed Google ID token") from exc
    if header.get("alg") != "RS256":
        raise GoogleTokenError("Unexpected Google token algorithm")
    kid = header.get("kid")
    if not kid:
        raise GoogleTokenError("Google ID token has no key id")
    certs = _google_certs_snapshot()
    key = certs.get(kid)
    if not key:
        # A rotated Google key may not be in the cached snapshot. Refresh (throttled).
        certs = _google_certs_snapshot(force=True)
        key = certs.get(kid)
    if not key:
        raise GoogleTokenError("Google signing key not found")
    try:
        claims = jwt.decode(
            token,
            _google_public_key(key),
            algorithms=["RS256"],
            audience=GOOGLE_CLIENT_ID,
            # Issuer is checked below (Google uses two spellings). PyJWT does not check at_hash, which GIS
            # credentials cannot satisfy anyway (they carry no access token).
            leeway=_GOOGLE_CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "sub", "aud"]},
        )
    except Exception as exc:
        raise GoogleTokenError("Invalid Google ID token") from exc
    if claims.get("iss") not in _GOOGLE_ISSUERS:
        raise GoogleTokenError("Unexpected Google token issuer")
    azp = claims.get("azp")
    if azp and azp != GOOGLE_CLIENT_ID:
        raise GoogleTokenError("Google token was issued to a different client")
    # Google sends a boolean, but some libraries/proxies stringify it.
    if claims.get("email_verified") not in (True, "true", "True"):
        raise GoogleTokenError("Google email is not verified")
    if not claims.get("sub") or not claims.get("email"):
        raise GoogleTokenError("Google identity is incomplete")
    claims["email_verified"] = True
    return claims


def make_google_only_password() -> str:
    """Value for ``hashed_password`` on accounts created through Google: no password can ever match it
    (``verify_password`` short-circuits on the prefix) until the user sets one via password reset."""
    return _GOOGLE_ONLY_PREFIX + secrets.token_urlsafe(32)


def is_google_only_account(user: User) -> bool:
    return bool(user.google_subject and str(user.hashed_password).startswith(_GOOGLE_ONLY_PREFIX))


def authenticate_user(db: OrmSession, email: str, password: str) -> Optional[User]:
    normalized = normalize_email(email)
    if login_throttle_remaining(normalized) > 0:
        return None
    user = db.query(User).filter(User.email == normalized).first()
    if user and user.is_active:
        valid = verify_password(password, user.hashed_password)
    else:
        # Always perform an equivalent bcrypt operation for unknown/inactive
        # accounts so account enumeration does not get a cheap timing signal.
        valid = verify_password(password, _DUMMY_BCRYPT_HASH)
        valid = False
    if not user or not user.is_active or not valid:
        record_login_failure(normalized)
        return None
    clear_login_failures(normalized)
    return user


def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: OrmSession = Depends(get_db),
) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    claims = decode_token_claims(token)
    user_id, token_version, sid = str(claims["sub"]), claims["ver"], claims.get("sid")
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    if token_version != int(user.auth_version or 0):
        raise HTTPException(status_code=401, detail="Session expired; please sign in again")
    if sid is None:
        # Pre-Sprint-5 token: cannot be revoked individually. Refused when AUTH_REQUIRE_SESSION_ID (default in production).
        if _cfg.AUTH_REQUIRE_SESSION_ID:
            raise HTTPException(status_code=401, detail="Session expired; please sign in again")
    else:
        session = db.get(AuthSession, sid)
        if (
            session is None
            or session.user_id != user.id
            or session.revoked_at is not None
            or _utc(session.expires_at) <= datetime.now(timezone.utc)
        ):
            raise HTTPException(status_code=401, detail="Session expired; please sign in again")
    try:  # Sprint 4: pseudonymous user reference for log correlation (never the id or e-mail itself)
        from observability.context import bind_user
        bind_user(user.id)
    except Exception:  # noqa: BLE001
        pass
    return user


def new_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
