"""Latency measurements for the heaviest read paths, on this machine and a throw-away SQLite database:
``python -m evaluation.platform.perf``. Numbers are indicative (one laptop, SQLite, no concurrency), not a capacity guarantee."""
from __future__ import annotations

import os
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="qs-perf-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/perf.db"
os.environ.setdefault("ENVIRONMENT", "development")


def _median_ms(fn, n=5):
    xs = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t) * 1000)
    return round(sorted(xs)[len(xs) // 2], 1)


def main() -> int:
    import random
    import sqlite3
    import db
    db.Base.metadata.create_all(db.engine)
    import investigation
    import knowledge
    import overview
    import workspaces as ws_service
    from db import Dataset, KnowledgeChunk, KnowledgeDocument
    from . import env as ev
    rng = random.Random(7)
    vocab = [f"word{i}" for i in range(4000)]
    out = []
    with db.SessionLocal() as s:
        env = ev.build(s, docs={})
        doc = KnowledgeDocument(workspace_id=env.ws.id, created_by=env.owner.id, name="bulk.txt", file_format="txt", fingerprint="x" * 64, chunk_count=0)
        s.add(doc)
        s.commit()
        total = 0
        for target in (1000, 5000, 10000, 30000, 50000):
            rows = [KnowledgeChunk(document_id=doc.id, workspace_id=env.ws.id, ordinal=total + i, text=" ".join(rng.choices(vocab, k=130))) for i in range(target - total)]
            s.bulk_save_objects(rows)
            s.commit()
            total = target
            q = "word10 word200 word3000 word42"
            knowledge.clear_index_cache()
            t0 = time.perf_counter()
            knowledge.search(s, env.ws.id, q)
            cold = round((time.perf_counter() - t0) * 1000, 1)
            out.append(f"knowledge.search over {total:>6} passages (~900 chars each): first question (index build) {cold} ms, later questions median {_median_ms(lambda: knowledge.search(s, env.ws.id, q))} ms")
        member = ws_service.membership(s, env.ws.id, env.owner.id)
        out.append(f"workspace overview (50k passages, 2 datasets):  median {_median_ms(lambda: overview.build(s, env.ws, env.owner, member.role, ws_service.ROLE_RANK[member.role]))} ms")
        path = os.path.join(_TMP, "big.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE sales (order_date TEXT, region TEXT, amount REAL)")
        regions = ["N", "S", "E", "W", "C"]
        con.executemany("INSERT INTO sales VALUES (?,?,?)", ((f"20{20 + i % 5}-{1 + i % 12:02d}-{1 + i % 28:02d}", regions[i % 5], float(i % 997)) for i in range(200000)))
        con.commit()
        con.close()
        ds = Dataset(workspace_id=env.ws.id, created_by=env.owner.id, name="big", storage_ref="perf/big", status="ready")
        s.add(ds)
        s.commit()
        out.append(f"investigation.introspect, 200,000-row table:     median {_median_ms(lambda: investigation.introspect(path), 3)} ms")
        for kind in ("trend", "comparison", "breakdown"):
            params = {"kind": kind, "table": "sales", "measure": "amount", "date_column": "order_date", "dimension": "region", "agg": "sum"}
            out.append(f"investigation.run {kind:<10} on 200,000 rows:     median {_median_ms(lambda: investigation.run(s, ds, path, params), 3)} ms")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
