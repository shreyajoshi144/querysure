"""Sprint 6: data-quality profiling and anomaly findings. Rules are tested on numbers; the flow runs through the real API and jobs."""
from __future__ import annotations

import sqlite3

import pytest

import config
import quality
from db import DatasetQualityReport, SessionLocal
from tests.test_s1_workspaces import _add, _team, _user, local_uploads  # noqa: F401
from tests.test_s3_ingestion import _create, _csv, _datasets, _reimport, _runs  # noqa: F401


def _col(kind="numeric", non_null=100, rows=100, **kw):
    return {"type": "INTEGER" if kind == "numeric" else "TEXT", "kind": kind, "non_null": non_null, "nulls": rows - non_null,
            "null_pct": round(100.0 * (rows - non_null) / rows, 2) if rows else 0.0, **kw}


def _prof(rows=100, **cols):
    return {"t": {"rows": rows, "columns": cols or {"a": _col(rows=rows, non_null=rows)}}}


def _rules(findings):
    return {f["rule"] for f in findings}


# ---------------------------------------------------------------- engine

def test_profile_counts_nulls_distinct_and_ranges_but_no_text_values(tmp_path):
    p = tmp_path / "c.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE t (id INTEGER, name TEXT, d TIMESTAMP, amt REAL)")
    con.execute("CREATE TABLE _qs_quarantine (x TEXT)")
    con.executemany("INSERT INTO t VALUES (?,?,?,?)", [(1, "secret-a", "2026-01-01", 1.5), (2, None, "2026-02-01", 2.5), (3, "secret-a", None, None)])
    con.commit(); con.close()
    prof = quality.profile_file(str(p))
    assert list(prof) == ["t"]                                     # _qs_* tables are never profiled
    c = prof["t"]["columns"]
    assert prof["t"]["rows"] == 3 and c["name"]["nulls"] == 1 and c["name"]["distinct"] == 1
    assert c["id"]["min"] == 1 and c["id"]["max"] == 3 and c["amt"]["mean"] == 2.0
    assert c["d"]["min"] == "2026-01-01" and "min" not in c["name"]
    assert "secret-a" not in str(prof)


def test_rules_fire_on_drop_nulls_type_and_schema_changes():
    prev = _prof(100, a=_col(rows=100, non_null=100, mean=10.0), b=_col("text", rows=100, non_null=100), gone=_col(rows=100, non_null=100))
    cur = _prof(30, a=_col(rows=30, non_null=30, mean=40.0), b=_col("numeric", rows=30, non_null=3), new=_col(rows=30, non_null=30))
    cur["t"]["columns"]["b"]["type"] = "INTEGER"
    found = quality.evaluate(cur, prev, [], None)
    assert {"row_count_drop", "null_spike", "type_changed", "numeric_shift", "column_removed", "column_added"} <= _rules(found)
    assert quality.overall(found) == "warn"
    drop = next(f for f in found if f["rule"] == "row_count_drop")
    assert drop["value"] == 30 and drop["baseline"] == 100


def test_empty_and_all_null_and_removed_table():
    prev = _prof(50)
    prev["gone"] = prev["t"]
    cur = {"t": {"rows": 0, "columns": {"a": _col(rows=0, non_null=0)}}}
    assert {"empty_table", "table_removed"} <= _rules(quality.evaluate(cur, prev, [], None))
    assert quality.overall(quality.evaluate(cur, prev, [], None)) == "error"
    allnull = _prof(10, a=_col(rows=10, non_null=0))
    assert "all_null_column" in _rules(quality.evaluate(allnull, None, [], None))


def test_first_version_has_no_comparison_findings():
    assert quality.evaluate(_prof(100), None, [], None) == []


def test_history_anomaly_needs_four_points_and_three_sigma():
    hist = [{"t": n} for n in (100, 102, 98, 101, 99)]
    assert "row_count_anomaly" in _rules(quality.evaluate(_prof(160), _prof(99), hist, None))
    assert "row_count_anomaly" not in _rules(quality.evaluate(_prof(101), _prof(99), hist, None))
    assert "row_count_anomaly" not in _rules(quality.evaluate(_prof(160), _prof(99), hist[:3], None))


def test_reject_and_duplicate_thresholds():
    class R:
        records_read, records_rejected, duplicate_rows = 100, 6, 10
    f = quality.evaluate(_prof(10), None, [], R)
    rej = next(x for x in f if x["rule"] == "high_reject_rate")
    assert rej["severity"] == "warn" and "duplicate_rows" in _rules(f)
    R.records_rejected = 25
    assert next(x for x in quality.evaluate(_prof(10), None, [], R) if x["rule"] == "high_reject_rate")["severity"] == "error"


def test_thresholds_are_configurable(monkeypatch):
    prev, cur = _prof(100), _prof(80)
    assert "row_count_drop" not in _rules(quality.evaluate(cur, prev, [], None))
    monkeypatch.setattr(config, "QUALITY_ROW_DROP_PCT", 10)
    assert "row_count_drop" in _rules(quality.evaluate(cur, prev, [], None))


# ---------------------------------------------------------------- through the API

def _reports(ds_id):
    with SessionLocal() as db:
        return db.query(DatasetQualityReport).filter(DatasetQualityReport.dataset_id == ds_id).order_by(DatasetQualityReport.created_at).all()


def _first_dataset(client, u, ws=None):
    from tests.test_s1_workspaces import _personal
    return _datasets(client, u, ws or _personal(client, u)).json()["datasets"][0]


def test_upload_creates_a_report_and_reimport_flags_the_drop(client):
    u = _user(client)
    r, _ = _create(client, u, "sales.csv", _csv(40))
    assert r.status_code in (200, 202), r.text
    ds = _first_dataset(client, u)
    assert ds["quality"]["status"] == "ok"                           # first version: nothing to compare with
    body = client._inner.get(f"/api/datasets/{ds['id']}/quality", headers=u["h"]).json()
    assert body["success"] and body["latest"]["profile"]["sales"]["rows"] == 40 and body["latest"]["findings_list"] == []
    resp, job = _reimport(client, u, ds["id"], "sales.csv", _csv(8))
    assert resp.status_code == 202 and job["status"] == "succeeded"
    body = client._inner.get(f"/api/datasets/{ds['id']}/quality", headers=u["h"]).json()
    assert body["latest"]["status"] == "warn" and "row_count_drop" in {f["rule"] for f in body["latest"]["findings_list"]}
    assert len(body["history"]) == 2 and body["latest"]["dataset_version"] == 2
    assert _first_dataset(client, u)["quality"]["status"] == "warn"


def test_identical_reimport_adds_no_report(client):
    u = _user(client)
    _create(client, u, "sales.csv", _csv(20))
    ds = _first_dataset(client, u)
    _reimport(client, u, ds["id"], "sales.csv", _csv(20))
    assert len(_reports(ds["id"])) == 1


def test_quality_endpoint_isolation_and_roles(client):
    owner, outsider, viewer = _user(client, "qo"), _user(client, "qx"), _user(client, "qv")
    ws = _team(client, owner)
    _add(client, owner, ws, viewer, "viewer")
    r, _ = _create(client, owner, "sales.csv", _csv(20), workspace_id=ws)
    assert r.status_code in (200, 202), r.text
    ds = _first_dataset(client, owner, ws)
    url = f"/api/datasets/{ds['id']}/quality"
    assert client._inner.get(url, headers=viewer["h"]).status_code == 200            # viewers may read quality
    assert client._inner.get(url, headers=outsider["h"]).status_code == 404          # non-members cannot tell it exists
    assert client._inner.get(url).status_code == 401
    assert client._inner.get("/api/datasets/nope/quality", headers=owner["h"]).status_code == 404


def test_quality_failure_never_fails_the_import(client, monkeypatch):
    def boom(path):
        raise RuntimeError("profiler exploded")
    monkeypatch.setattr(quality, "profile_file", boom)
    u = _user(client)
    r, _ = _create(client, u, "sales.csv", _csv(20))
    assert r.status_code in (200, 202), r.text
    ds = _first_dataset(client, u)
    assert ds["status"] == "ready" and ds["quality"] is None and _reports(ds["id"]) == []


def test_disabled_flag_skips_profiling(client, monkeypatch):
    monkeypatch.setattr(config, "QUALITY_ENABLED", False)
    u = _user(client)
    _create(client, u, "sales.csv", _csv(20))
    assert _first_dataset(client, u)["quality"] is None


def test_reports_are_trimmed_and_removed_with_the_dataset(client, monkeypatch):
    monkeypatch.setattr(config, "QUALITY_REPORTS_KEEP", 5)
    u = _user(client)
    _create(client, u, "sales.csv", _csv(20))
    ds = _first_dataset(client, u)
    for n in range(21, 29):
        _reimport(client, u, ds["id"], "sales.csv", _csv(n))
    assert len(_reports(ds["id"])) == 5
    assert client._inner.delete(f"/api/datasets/{ds['id']}", headers=u["h"]).status_code == 200
    assert _reports(ds["id"]) == []
