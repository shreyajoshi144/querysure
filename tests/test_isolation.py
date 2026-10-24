"""
Cross-user isolation regression tests for the retrieval engine.

Background: retrieval_engine used a single process-wide cache slot shared by
every caller, so user A's /api/retrieve-tables could return user B's table
names (and feed them into A's LLM prompt). These tests pin the fix at two
levels: the engine itself, and the real HTTP API with two authenticated users.
"""
from __future__ import annotations

from tests.auth_test_utils import verify_registered_email

import io
import uuid

import pandas as pd

import os
import sqlite3
import tempfile

from modules.retrieval_engine import build_vector_store, retrieve_context
from modules.schema_engine import load_database
from modules.semantic_layer import build_semantic_layer


def _semantic(table: str, column: str) -> dict:
    """Build a semantic layer the same way production does: real SQLite file
    -> load_database -> build_semantic_layer."""
    path = os.path.join(tempfile.mkdtemp(prefix="iso_"), "t.db")
    conn = sqlite3.connect(path)
    conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, "{column}" REAL)')
    conn.execute(f'INSERT INTO "{table}" VALUES (1, 1.0)')
    conn.commit()
    conn.close()
    return build_semantic_layer(load_database(path))


def _table_names(retrieval: dict) -> list:
    return [t["table"] for t in retrieval["top_tables"]]


# ---------------------------------------------------------------- engine level

def test_default_call_does_not_share_state_between_schemas():
    """The original bug: no key -> both callers hit one shared slot."""
    a = _semantic("customers_secret_alpha", "alpha_confidential_salary")
    b = _semantic("orders_beta", "amount")
    build_vector_store(a)
    build_vector_store(b)  # used to overwrite the shared slot
    names = _table_names(retrieve_context("show me the data", a))
    assert "customers_secret_alpha" in names
    assert not any("beta" in n for n in names)


def test_per_key_cache_isolated_when_other_user_connects_later():
    a = _semantic("customers_secret_alpha", "alpha_confidential_salary")
    b = _semantic("orders_beta", "amount")
    build_vector_store(a, cache_key="iso-A")
    build_vector_store(b, cache_key="iso-B")
    names = _table_names(retrieve_context("show me the data", a, cache_key="iso-A"))
    assert not any("beta" in n for n in names)


def test_wrong_cache_key_cannot_leak_another_schema():
    """Defence in depth: even a caller bug (wrong key) must not return
    another schema's data, because the index is verified against the
    semantic layer actually passed in."""
    a = _semantic("customers_secret_alpha", "alpha_confidential_salary")
    b = _semantic("orders_beta", "amount")
    build_vector_store(b, cache_key="iso-shared-key")
    names = _table_names(retrieve_context("show me the data", a, cache_key="iso-shared-key"))
    assert "customers_secret_alpha" in names
    assert not any("beta" in n for n in names)


def test_stale_cache_after_schema_change_is_rebuilt():
    """Same key, schema replaced (e.g. re-upload handled by another worker)."""
    old = _semantic("old_table", "old_col")
    new = _semantic("brand_new_table", "new_col")
    build_vector_store(old, cache_key="iso-stale")
    names = _table_names(retrieve_context("show me the data", new, cache_key="iso-stale"))
    assert "brand_new_table" in names
    assert "old_table" not in names


def test_cache_is_bounded():
    from modules.retrieval_engine import _CACHE_MAX_ENTRIES, _COLLECTION_CACHE

    sem = _semantic("t", "c")
    for i in range(_CACHE_MAX_ENTRIES + 25):
        build_vector_store(sem, cache_key=f"bound-{i}")
    assert len(_COLLECTION_CACHE) <= _CACHE_MAX_ENTRIES


# ------------------------------------------------------------------- HTTP level

def _register(client) -> dict:
    email = f"iso-{uuid.uuid4().hex[:10]}@example.com"
    pw = "TestPassword123!"
    assert client.post("/api/auth/register", json={"email": email, "password": pw}).status_code == 200
    verify_registered_email(client, email)
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _upload_csv(client, headers, filename: str, df: pd.DataFrame) -> str:
    r = client.post(
        "/api/connect/upload",
        headers=headers,
        files={"file": (filename, df.to_csv(index=False).encode(), "text/csv")},
    )
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def test_two_users_retrieve_tables_never_see_each_others_schema(client):
    user_a = _register(client)
    user_b = _register(client)

    sid_a = _upload_csv(client, user_a, "alpha_payroll.csv",
                        pd.DataFrame({"employee": ["x", "y"], "alpha_salary": [1.0, 2.0]}))
    # B connects AFTER A -- this is what used to clobber A's retrieval index.
    sid_b = _upload_csv(client, user_b, "beta_orders.csv",
                        pd.DataFrame({"order_ref": ["p", "q"], "beta_amount": [3.0, 4.0]}))

    ra = client.post("/api/retrieve-tables", headers=user_a,
                     json={"session_id": sid_a, "question": "show me the data"})
    assert ra.status_code == 200, ra.text
    blob_a = ra.text.lower()
    assert "alpha" in blob_a
    assert "beta" not in blob_a, "user A's response leaked user B's schema"

    rb = client.post("/api/retrieve-tables", headers=user_b,
                     json={"session_id": sid_b, "question": "show me the data"})
    assert rb.status_code == 200, rb.text
    blob_b = rb.text.lower()
    assert "beta" in blob_b
    assert "alpha" not in blob_b, "user B's response leaked user A's schema"
