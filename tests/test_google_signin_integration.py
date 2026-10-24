"""Google Sign-In end to end: real RS256-signed ID tokens (generated locally, served through a JWKS-shaped key set,
exactly as Google publishes them) go through the real verifier and the real API routes. Only Google's key endpoint is
replaced; signature, issuer, audience, azp, expiry and email checks all run for real."""
from __future__ import annotations

import time
import uuid
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import json

import jwt

import auth
from db import AuthSession, SessionLocal, User
from tests.auth_test_utils import verify_registered_email

CLIENT_ID = "123-test.apps.googleusercontent.com"
KID = "test-key-1"
RUN = uuid.uuid4().hex[:8]  # the test DB is shared across test modules/runs; keep fixture identities unique


def _pem(private_key) -> str:
    return private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _jwk_for(private_key, kid=KID) -> dict:
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    data.update(kid=kid, use="sig", alg="RS256")
    return data


@pytest.fixture(scope="module")
def google_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def google(google_key, client):
    """Configure GOOGLE_CLIENT_ID and serve Google's key set (JWKS format) for the duration of a test."""
    keys = {KID: _jwk_for(google_key)}
    with patch("api.GOOGLE_CLIENT_ID", CLIENT_ID), patch("auth.GOOGLE_CLIENT_ID", CLIENT_ID), \
            patch("auth._google_certs_snapshot", side_effect=lambda force=False: keys):
        yield client


def make_token(private_key, *, kid=KID, alg="RS256", **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "azp": CLIENT_ID,
        "aud": CLIENT_ID,
        "sub": "g-" + uuid.uuid4().hex[:12],
        "email": f"g-{uuid.uuid4().hex[:8]}@example.com",
        "email_verified": True,
        "nbf": now - 5,
        "iat": now,
        "exp": now + 3600,
        "jti": uuid.uuid4().hex,
    }
    claims.update(overrides)
    for k in [k for k, v in claims.items() if v is None]:
        del claims[k]
    return jwt.encode(claims, _pem(private_key), algorithm=alg, headers={"kid": kid})


def sign_in(client, token, field="credential"):
    return client.post("/api/auth/google", json={field: token})


def bearer(resp):
    return {"Authorization": "Bearer " + resp.json()["access_token"]}


def _count_users(email):
    with SessionLocal() as db:
        return db.query(User).filter(User.email == email).count()


# ------------------------------------------------------------------ new signup / session

def test_new_google_user_is_created_verified_and_gets_a_working_session(google, google_key):
    email, sub = f"newgoogle-{RUN}@example.com", f"sub-new-1-{RUN}"
    r = sign_in(google, make_token(google_key, email=email, sub=sub))
    assert r.status_code == 200, r.text
    assert r.json()["token_type"] == "bearer"
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).one()
        assert user.google_subject == sub
        assert user.email_verified is True  # Google verified the mailbox
        assert auth.is_google_only_account(user)  # nobody can guess/use the password slot
        assert db.query(AuthSession).filter(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)).count() == 1
    me = google.get("/api/auth/me", headers=bearer(r))
    assert me.status_code == 200 and me.json()["email"] == email


def test_google_only_account_cannot_password_login(google, google_key):
    email = f"nopass-{RUN}@example.com"
    assert sign_in(google, make_token(google_key, email=email)).status_code == 200
    for guess in ("", "GOOGLE_ONLY$", "StrongPassword123!"):
        r = google.post("/api/auth/login", json={"email": email, "password": guess or "x" * 12})
        assert r.status_code in (401, 422)


def test_email_is_normalised_so_case_differences_do_not_duplicate(google, google_key):
    sub = f"sub-case-{RUN}"
    a = sign_in(google, make_token(google_key, email=f"MixedCase-{RUN}@Example.com", sub=sub))
    b = sign_in(google, make_token(google_key, email=f"mixedcase-{RUN}@example.com", sub=sub))
    assert a.status_code == b.status_code == 200, (a.text, b.text)
    assert _count_users(f"mixedcase-{RUN}@example.com") == 1


def test_repeat_sign_in_reuses_account_and_creates_fresh_sessions(google, google_key):
    email, sub = f"repeat-{RUN}@example.com", f"sub-repeat-{RUN}"
    r1 = sign_in(google, make_token(google_key, email=email, sub=sub))
    r2 = sign_in(google, make_token(google_key, email=email, sub=sub))
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["access_token"] != r2.json()["access_token"]
    assert _count_users(email) == 1
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).one()
        assert db.query(AuthSession).filter(AuthSession.user_id == user.id).count() == 2


def test_returning_user_matched_by_subject_even_if_google_email_changed(google, google_key):
    sub = f"sub-stable-{RUN}"
    first = sign_in(google, make_token(google_key, email=f"old-addr-{RUN}@example.com", sub=sub))
    second = sign_in(google, make_token(google_key, email=f"new-addr-{RUN}@example.com", sub=sub))
    assert first.status_code == second.status_code == 200
    assert _count_users(f"new-addr-{RUN}@example.com") == 0  # no duplicate account under the new address
    assert google.get("/api/auth/me", headers=bearer(second)).json()["email"] == f"old-addr-{RUN}@example.com"


def test_google_session_respects_sign_out_and_logout_all(google, google_key):
    email, sub = f"revoke-{RUN}@example.com", f"sub-revoke-{RUN}"
    a = sign_in(google, make_token(google_key, email=email, sub=sub))
    b = sign_in(google, make_token(google_key, email=email, sub=sub))
    assert google.post("/api/auth/sign-out", headers=bearer(a)).status_code == 200
    assert google.get("/api/auth/me", headers=bearer(a)).status_code == 401  # revoked immediately
    assert google.get("/api/auth/me", headers=bearer(b)).status_code == 200  # other session untouched
    assert google.post("/api/auth/logout-all", headers=bearer(b)).status_code == 200
    assert google.get("/api/auth/me", headers=bearer(b)).status_code == 401
    # Signing in with Google again after revocation works.
    c = sign_in(google, make_token(google_key, email=email, sub=sub))
    assert c.status_code == 200 and google.get("/api/auth/me", headers=bearer(c)).status_code == 200


def test_google_users_are_isolated_from_each_other(google, google_key):
    a = sign_in(google, make_token(google_key, email=f"iso-a-{RUN}@example.com", sub=f"iso-a-{RUN}"))
    b = sign_in(google, make_token(google_key, email=f"iso-b-{RUN}@example.com", sub=f"iso-b-{RUN}"))
    assert google.get("/api/auth/me", headers=bearer(a)).json()["email"] == f"iso-a-{RUN}@example.com"
    assert google.get("/api/auth/me", headers=bearer(b)).json()["email"] == f"iso-b-{RUN}@example.com"


def test_inactive_google_account_is_refused(google, google_key):
    email, sub = f"inactive-{RUN}@example.com", f"sub-inactive-{RUN}"
    assert sign_in(google, make_token(google_key, email=email, sub=sub)).status_code == 200
    with SessionLocal() as db:
        db.query(User).filter(User.email == email).update({"is_active": 0})
        db.commit()
    r = sign_in(google, make_token(google_key, email=email, sub=sub))
    assert r.status_code == 403 and r.json()["error_code"] == "ACCOUNT_INACTIVE"


# ------------------------------------------------------------------ existing accounts

def _register(client, email, password="StrongPassword123!", verify=True):
    assert client.post("/api/auth/register", json={"email": email, "password": password}).status_code == 200
    if verify:
        verify_registered_email(client, email)
    return password


def test_password_account_is_never_silently_linked_or_duplicated(google, google_key):
    """Policy kept from Sprint 5: even a VERIFIED password account is not linked by e-mail alone; the owner links
    explicitly (signed in) via POST /api/auth/google/link. No duplicate account is created either way."""
    email = f"verified-local-{RUN}@example.com"
    password = _register(google, email)
    r = sign_in(google, make_token(google_key, email=email, sub=f"sub-link-auto-{RUN}"))
    assert r.status_code == 409 and r.json()["error_code"] == "GOOGLE_ACCOUNT_LINK_REQUIRED"
    assert _count_users(email) == 1
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).one()
        assert user.google_subject is None
        assert not auth.is_google_only_account(user)  # the existing password keeps working
    assert google.post("/api/auth/login", json={"email": email, "password": password}).status_code == 200


def test_unverified_password_account_is_not_linked_by_google_sign_in(google, google_key):
    email = f"unverified-local-{RUN}@example.com"
    _register(google, email, verify=False)
    r = sign_in(google, make_token(google_key, email=email, sub=f"sub-unverified-{RUN}"))
    assert r.status_code == 409 and r.json()["error_code"] == "GOOGLE_ACCOUNT_LINK_REQUIRED"
    with SessionLocal() as db:
        assert db.query(User).filter(User.email == email).one().google_subject is None
    assert _count_users(email) == 1


def test_account_linked_to_another_google_identity_is_not_taken_over(google, google_key):
    email = f"owned-{RUN}@example.com"
    assert sign_in(google, make_token(google_key, email=email, sub=f"sub-owner-{RUN}")).status_code == 200
    r = sign_in(google, make_token(google_key, email=email, sub=f"sub-attacker-{RUN}"))
    assert r.status_code == 409 and r.json()["error_code"] == "GOOGLE_ACCOUNT_LINK_REQUIRED"
    with SessionLocal() as db:
        assert db.query(User).filter(User.email == email).one().google_subject == f"sub-owner-{RUN}"


def test_explicit_link_endpoint_still_requires_matching_email_and_unique_subject(google, google_key):
    email = f"linker-{RUN}@example.com"
    password = _register(google, email)
    headers = bearer(google.post("/api/auth/login", json={"email": email, "password": password}))
    mismatch = google.post("/api/auth/google/link", headers=headers,
                           json={"credential": make_token(google_key, email=f"someone-else-{RUN}@example.com")})
    assert mismatch.status_code == 403 and mismatch.json()["error_code"] == "GOOGLE_EMAIL_MISMATCH"
    taken = sign_in(google, make_token(google_key, email=f"taken-{RUN}@example.com", sub=f"sub-taken-{RUN}"))
    assert taken.status_code == 200
    in_use = google.post("/api/auth/google/link", headers=headers,
                         json={"credential": make_token(google_key, email=email, sub=f"sub-taken-{RUN}")})
    assert in_use.status_code == 409 and in_use.json()["error_code"] == "GOOGLE_IDENTITY_IN_USE"
    ok = google.post("/api/auth/google/link", headers=headers,
                     json={"credential": make_token(google_key, email=email, sub=f"sub-linker-{RUN}")})
    assert ok.status_code == 200
    again = google.post("/api/auth/google/link", headers=headers,
                        json={"credential": make_token(google_key, email=email, sub=f"sub-linker-{RUN}")})
    assert again.status_code == 409 and again.json()["error_code"] == "GOOGLE_ALREADY_LINKED"


def test_link_endpoint_requires_authentication(google, google_key):
    r = google.post("/api/auth/google/link", json={"credential": make_token(google_key)})
    assert r.status_code == 401


# ------------------------------------------------------------------ token validation (real verifier)

def test_wrong_audience_is_rejected(google, google_key):
    r = sign_in(google, make_token(google_key, aud="someone-elses-client.apps.googleusercontent.com"))
    assert r.status_code == 401 and r.json()["error_code"] == "INVALID_GOOGLE_TOKEN"


def test_wrong_authorized_party_is_rejected(google, google_key):
    r = sign_in(google, make_token(google_key, azp="another-client.apps.googleusercontent.com"))
    assert r.status_code == 401


def test_wrong_issuer_is_rejected(google, google_key):
    r = sign_in(google, make_token(google_key, iss="https://evil.example.com"))
    assert r.status_code == 401


def test_both_google_issuer_spellings_are_accepted(google, google_key):
    r = sign_in(google, make_token(google_key, iss="accounts.google.com"))
    assert r.status_code == 200


def test_expired_token_is_rejected(google, google_key):
    r = sign_in(google, make_token(google_key, exp=int(time.time()) - 600))
    assert r.status_code == 401 and r.json()["error_code"] == "INVALID_GOOGLE_TOKEN"


def test_not_yet_valid_token_beyond_skew_is_rejected(google, google_key):
    r = sign_in(google, make_token(google_key, nbf=int(time.time()) + 600))
    assert r.status_code == 401


def test_small_clock_skew_and_at_hash_are_tolerated(google, google_key):
    # Real GIS tokens can arrive a few seconds "early" on a skewed server clock; python-jose also rejects any token
    # carrying at_hash unless an access token is supplied, which GIS never provides.
    r = sign_in(google, make_token(google_key, nbf=int(time.time()) + 5, iat=int(time.time()) + 5, at_hash="abc123"))
    assert r.status_code == 200, r.text


def test_unverified_google_email_is_rejected(google, google_key):
    assert sign_in(google, make_token(google_key, email_verified=False)).status_code == 401
    assert sign_in(google, make_token(google_key, email_verified=None)).status_code == 401


def test_stringified_email_verified_true_is_accepted(google, google_key):
    assert sign_in(google, make_token(google_key, email_verified="true")).status_code == 200


def test_missing_subject_or_email_is_rejected(google, google_key):
    assert sign_in(google, make_token(google_key, sub=None)).status_code == 401
    assert sign_in(google, make_token(google_key, email=None)).status_code == 401


def test_token_signed_by_a_different_key_is_rejected(google, google_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    email = f"badsig-{RUN}@example.com"
    r = sign_in(google, make_token(other, email=email))  # right kid, wrong key => bad signature
    assert r.status_code == 401
    assert _count_users(email) == 0


def test_unknown_key_id_is_rejected(google, google_key):
    assert sign_in(google, make_token(google_key, kid="rotated-away")).status_code == 401


def test_hs256_and_garbage_tokens_are_rejected(google):
    forged = jwt.encode({"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "x",
                         "email": f"forged-{RUN}@example.com", "email_verified": True, "exp": int(time.time()) + 600},
                        "secret", algorithm="HS256", headers={"kid": KID})
    assert sign_in(google, forged).status_code == 401
    assert sign_in(google, "not-a-jwt").status_code == 401
    assert _count_users(f"forged-{RUN}@example.com") == 0


def test_oversized_credential_is_rejected_cleanly(google):
    r = sign_in(google, "a" * 20000)
    assert r.status_code == 401


# ------------------------------------------------------------------ request shape (the 422 family)

def test_missing_or_blank_credential_is_the_route_error_not_a_generic_validation_error(google):
    for body in ({}, {"credential": ""}, {"credential": "   "}, {"credential": None}, {"credential": 123}):
        r = google.post("/api/auth/google", json=body)
        assert r.status_code == 422, (body, r.text)
        assert r.json()["error_code"] == "GOOGLE_CREDENTIAL_REQUIRED"
    assert google.post("/api/auth/google", json=["x"]).json()["error_code"] == "GOOGLE_CREDENTIAL_REQUIRED"


def test_alternate_credential_field_names_are_accepted(google, google_key):
    for field in ("id_token", "idToken", "token"):
        email = f"alias-{field.lower()}@example.com"
        r = sign_in(google, make_token(google_key, email=email), field=field)
        assert r.status_code == 200, (field, r.text)


def test_extra_fields_are_ignored(google, google_key):
    r = google.post("/api/auth/google", json={"credential": make_token(google_key), "select_by": "btn", "clientId": "x"})
    assert r.status_code == 200


# ------------------------------------------------------------------ configuration / upstream failures

def test_unconfigured_deployment_returns_503_not_500(client):
    with patch("api.GOOGLE_CLIENT_ID", ""), patch("auth.GOOGLE_CLIENT_ID", ""):
        r = client.post("/api/auth/google", json={"credential": "x"})
    assert r.status_code == 503 and r.json()["error_code"] == "GOOGLE_NOT_CONFIGURED"
    with patch("api.GOOGLE_CLIENT_ID", ""):
        cfg = client.get("/api/auth/google/config").json()
    assert cfg == {"enabled": False, "client_id": None}


def test_google_key_endpoint_outage_is_503_and_never_creates_an_account(client, google_key):
    token = make_token(google_key, email=f"outage-{RUN}@example.com")
    with patch("api.GOOGLE_CLIENT_ID", CLIENT_ID), patch("auth.GOOGLE_CLIENT_ID", CLIENT_ID), \
            patch("auth._google_certs_snapshot", side_effect=auth.GoogleUnavailableError("down")):
        r = client.post("/api/auth/google", json={"credential": token})
    assert r.status_code == 503 and r.json()["error_code"] == "GOOGLE_UNAVAILABLE"
    assert _count_users(f"outage-{RUN}@example.com") == 0


def test_error_responses_do_not_leak_verifier_internals(google, google_key):
    r = sign_in(google, make_token(google_key, aud="other"))
    text = r.text.lower()
    assert "audience" not in text and "traceback" not in text and CLIENT_ID not in r.text


def test_csp_allows_google_identity_services_stylesheet_and_script(client):
    csp = client.get("/api/auth/google/config").headers.get("content-security-policy", "")
    assert "https://accounts.google.com/gsi/style" in csp
    assert "script-src 'self' https://accounts.google.com" in csp
    assert "frame-src https://accounts.google.com" in csp
    assert "cross-origin-opener-policy" not in {k.lower() for k in client.get("/").headers}


# ------------------------------------------------------------------ account deletion for Google-only accounts

def test_google_only_account_can_delete_with_fresh_google_credential(google, google_key):
    email, sub = f"deleteme-{RUN}@example.com", f"sub-delete-{RUN}"
    r = sign_in(google, make_token(google_key, email=email, sub=sub))
    headers = bearer(r)
    no_cred = google.post("/api/account/delete", headers=headers, json={"confirm_email": email})
    assert no_cred.status_code == 401
    wrong = google.post("/api/account/delete", headers=headers,
                        json={"confirm_email": email, "google_credential": make_token(google_key, email=email, sub=f"other-sub-{RUN}")})
    assert wrong.status_code == 401
    assert _count_users(email) == 1
    ok = google.post("/api/account/delete", headers=headers,
                     json={"confirm_email": email, "google_credential": make_token(google_key, email=email, sub=sub)})
    assert ok.status_code == 200, ok.text
    assert _count_users(email) == 0


def test_google_only_account_gets_a_clear_change_password_response(google, google_key):
    r = sign_in(google, make_token(google_key))
    resp = google.post("/api/auth/change-password", headers=bearer(r),
                       json={"current_password": "whatever-123", "new_password": "AnotherStrong123!"})
    assert resp.status_code == 409 and resp.json()["error_code"] == "PASSWORD_NOT_SET"


# ------------------------------------------------------------------ key cache behaviour (unit)

class _FakeResponse:
    def __init__(self, body, cache="public, max-age=600"):
        self._body, self.headers = body, {"Cache-Control": cache}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def clean_cert_cache():
    saved = (auth._google_certs, auth._google_certs_expires_at, auth._google_certs_last_fetch)
    auth._google_certs, auth._google_certs_expires_at, auth._google_certs_last_fetch = {}, 0.0, 0.0
    yield
    auth._google_certs, auth._google_certs_expires_at, auth._google_certs_last_fetch = saved


def test_cert_cache_parses_jwks_and_honours_max_age(clean_cert_cache, google_key):
    import json
    body = json.dumps({"keys": [_jwk_for(google_key, "k-a")]}).encode()
    with patch("auth.urllib.request.urlopen", return_value=_FakeResponse(body)) as opened:
        first = auth._google_certs_snapshot()
        second = auth._google_certs_snapshot()
    assert "k-a" in first and first is second
    assert opened.call_count == 1  # second call served from cache


def test_forced_refresh_is_throttled_so_unknown_kids_cannot_hammer_google(clean_cert_cache, google_key):
    import json
    body = json.dumps({"keys": [_jwk_for(google_key, "k-a")]}).encode()
    with patch("auth.urllib.request.urlopen", return_value=_FakeResponse(body)) as opened:
        auth._google_certs_snapshot()
        for _ in range(5):
            auth._google_certs_snapshot(force=True)
    assert opened.call_count == 1


def test_stale_keys_are_served_during_a_short_google_outage(clean_cert_cache, google_key):
    import json
    body = json.dumps({"keys": [_jwk_for(google_key, "k-a")]}).encode()
    with patch("auth.urllib.request.urlopen", return_value=_FakeResponse(body)):
        auth._google_certs_snapshot()
    auth._google_certs_expires_at = 0.0  # expired
    with patch("auth.urllib.request.urlopen", side_effect=OSError("network down")):
        assert "k-a" in auth._google_certs_snapshot()


def test_cold_cache_and_outage_raises_google_unavailable(clean_cert_cache):
    with patch("auth.urllib.request.urlopen", side_effect=OSError("network down")):
        with pytest.raises(auth.GoogleUnavailableError):
            auth._google_certs_snapshot()


def test_invalid_key_document_is_treated_as_unavailable(clean_cert_cache):
    with patch("auth.urllib.request.urlopen", return_value=_FakeResponse(b'{"nope": 1}')):
        with pytest.raises(auth.GoogleUnavailableError):
            auth._google_certs_snapshot()
