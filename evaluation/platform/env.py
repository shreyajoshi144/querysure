"""A throw-away workspace for the offline suites: real users, workspaces, documents (indexed by the real ``knowledge.index_document``),
datasets and a connection, in whatever database ``db.SessionLocal`` points at. Names are unique per build, so it is safe inside the test
database; the standalone CLI points ``DATABASE_URL`` at a temp SQLite file first (see ``__main__``)."""
from __future__ import annotations

import os
import tempfile
import uuid
from types import SimpleNamespace

import knowledge
import workspaces as ws_service
from db import Connection, Dataset, User

from .corpus import DOCS


def _user(db, tag: str) -> User:
    u = User(email=f"eval-{tag}-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", email_verified=True)
    db.add(u)
    db.commit()
    return u


def build(db, *, docs: dict | None = None) -> SimpleNamespace:
    """Owner + analyst of a team workspace holding ``docs`` (default: the retrieval corpus) and two datasets (one connection-backed);
    plus a second workspace with its own document, for isolation checks."""
    tmp = tempfile.mkdtemp(prefix="qs-eval-")
    owner, analyst, other_owner = _user(db, "owner"), _user(db, "analyst"), _user(db, "other")
    ws = ws_service.create_team(db, owner, f"Eval {uuid.uuid4().hex[:6]}")
    ws_other = ws_service.create_team(db, other_owner, f"Other {uuid.uuid4().hex[:6]}")
    owner_member = ws_service.membership(db, ws.id, owner.id)
    ws_service.add_member(db, ws, owner_member, analyst.email, "analyst")

    def index(workspace, user, name, text):
        path = os.path.join(tmp, f"{uuid.uuid4().hex}-{name}")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return knowledge.index_document(db, workspace_id=workspace.id, user_id=user.id, name=name, ext=".md", path=path, size=len(text.encode())).id

    doc_ids = {name: index(ws, owner, name, text) for name, text in (docs if docs is not None else DOCS).items()}
    other_doc = index(ws_other, other_owner, "secret_other.md", "Confidential merger plans of the other workspace. Codename aurora budget nine million.")
    conn = Connection(workspace_id=ws.id, created_by=owner.id, name="warehouse", connector="postgres", config={}, status="active")
    db.add(conn)
    db.flush()
    ds_ids, ds_other = {}, None
    for name, kw in (("Orders", {"connection_id": conn.id, "source_table": "orders"}), ("Customers", {})):
        ds = Dataset(workspace_id=ws.id, created_by=owner.id, name=name, storage_ref=f"eval/{uuid.uuid4()}", status="ready", **kw)
        db.add(ds)
        db.flush()
        ds_ids[name] = ds.id
    dso = Dataset(workspace_id=ws_other.id, created_by=other_owner.id, name="OtherData", storage_ref=f"eval/{uuid.uuid4()}", status="ready")
    db.add(dso)
    db.commit()
    ds_other = dso.id
    return SimpleNamespace(db=db, tmp=tmp, owner=owner, analyst=analyst, other_owner=other_owner, ws=ws, ws_other=ws_other,
                           docs=doc_ids, other_doc=other_doc, datasets=ds_ids, other_dataset=ds_other)
