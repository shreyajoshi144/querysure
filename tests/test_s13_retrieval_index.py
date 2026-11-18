"""Sprint 13: the cached inverted index returns exactly what scoring every passage returns, and never serves a stale or foreign index."""
from __future__ import annotations

import random

import knowledge
from db import KnowledgeChunk, KnowledgeDocument, SessionLocal
from modules.retrieval_engine import TextIndex, _build_tfidf, _cosine, _STOP, _tokenize
from tests.test_s1_workspaces import _team, _user, local_uploads  # noqa: F401
from tests.test_s7_knowledge import POLICY, _put


def _brute(query, texts, top_k, min_score):
    """The pre-Sprint-13 algorithm: cosine against every text."""
    from collections import Counter
    vecs, idf = _build_tfidf(texts)
    terms = [t for t in _tokenize(query) if t not in _STOP and t in idf]
    if not terms:
        return []
    qv = {t: (c / len(terms)) * idf[t] for t, c in Counter(terms).items()}
    scored = [(i, _cosine(qv, v)) for i, v in enumerate(vecs) if set(qv) & set(v)]
    scored = [(i, s) for i, s in scored if s >= min_score and s > 0]
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:top_k]


def test_inverted_index_scores_match_scoring_every_text():
    rng = random.Random(3)
    vocab = [f"term{i}" for i in range(300)] + ["refund", "invoice", "policy", "approval", "retention"]
    texts = [" ".join(rng.choices(vocab, k=rng.randint(5, 60))) for _ in range(400)]
    index = TextIndex(texts)
    for q in ("refund policy approval", "term7 term9 invoice", "retention", "term1 term2 term3 term4", "nothing matches xyzzy"):
        got, want = index.rank(q, 5, 0.05), _brute(q, texts, 5, 0.05)
        assert [i for i, _ in got] == [i for i, _ in want], q
        assert all(abs(a[1] - b[1]) < 1e-9 for a, b in zip(got, want)), q


def test_workspace_index_follows_uploads_and_deletes_and_never_crosses_workspaces(client):
    owner = _user(client, "idx")
    a, b = _team(client, owner), _team(client, owner)
    assert _put(client, owner, a, "policy.md", POLICY).status_code == 201
    with SessionLocal() as s:
        first = knowledge.search(s, a, "refund approval")
        assert first and knowledge.search(s, a, "refund approval") == first                       # cached, same answer
        assert knowledge.search(s, b, "refund approval") == []                                    # other workspace: no index, no leak
    extra = _put(client, owner, a, "garden.md", b"# Garden\n\nCompost and mulch improve soil drainage for tomato beds. " * 3)
    assert extra.status_code == 201
    with SessionLocal() as s:
        assert any(p["document_name"] == "garden.md" for p in knowledge.search(s, a, "compost mulch tomato"))   # new document visible at once
        doc = s.query(KnowledgeDocument).filter_by(workspace_id=a, name="garden.md").one()
        knowledge.delete_documents(s, [doc.id])
        s.commit()
        assert not any(p["document_name"] == "garden.md" for p in knowledge.search(s, a, "compost mulch tomato"))  # deleted document gone at once
        assert s.query(KnowledgeChunk).filter_by(workspace_id=b).count() == 0
        narrowed = knowledge.search(s, a, "refund approval", document_ids=[first[0]["document_id"]])
        assert narrowed and all(p["document_id"] == first[0]["document_id"] for p in narrowed)


def test_index_cache_is_bounded(client, monkeypatch):
    import config
    monkeypatch.setattr(config, "KNOWLEDGE_INDEX_CACHE_WORKSPACES", 1)
    knowledge.clear_index_cache()
    owner = _user(client, "lru")
    a, b = _team(client, owner), _team(client, owner)
    for ws in (a, b):
        assert _put(client, owner, ws, "policy.md", POLICY).status_code == 201
    with SessionLocal() as s:
        knowledge.search(s, a, "refund")
        knowledge.search(s, b, "refund")
    assert list(knowledge._INDEXES) == [b]
