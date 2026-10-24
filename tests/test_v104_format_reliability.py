"""v1.0.4 regression tests: Excel / CSV / SQLite reliability (JSON preserved).

Real user fixtures are used automatically when present in tests/fixtures/user/
(sales.xlsx, sales.csv, sales.json, northwind_small.sqlite); otherwise deterministic
stand-ins from tests/fixtures/build_*.py are used. Expected values are computed in plain
python/pandas from the source rows, never from the pipeline's own SQL."""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "fixtures"))

import build_formats as bf  # noqa: E402
import build_northwind_small as bnw  # noqa: E402
import format_benchmark as fb  # noqa: E402
from modules.business_insight_agent import generate_insights  # noqa: E402
from modules.data_reliability_agent import assess_reliability  # noqa: E402
from modules.ingestion import ingest_to_sqlite, read_csv, read_excel  # noqa: E402
from modules.query_result_engine import execute_query  # noqa: E402
from modules.schema_engine import load_database  # noqa: E402
from modules.sql_intent_checker import check_intent  # noqa: E402
from modules.sql_validator import validate_sql  # noqa: E402

USER = HERE / "fixtures" / "user"


@pytest.fixture(scope="module")
def src_rows():
    return bf.make_rows()


@pytest.fixture(scope="module")
def dbs(tmp_path_factory, src_rows):
    d = tmp_path_factory.mktemp("v104")
    files = {}
    for ext, writer in (("xlsx", bf.write_xlsx), ("csv", bf.write_csv), ("json", bf.write_json)):
        p = d / f"sales.{ext}"
        writer(str(p), src_rows)
        db = d / f"{ext}.db"
        ingest_to_sqlite(str(p), "." + ext, str(db), "sales")
        files[ext] = str(db)
    nw = d / "northwind_small.sqlite"
    bnw.build(str(nw))
    files["sqlite"] = str(nw)
    return files


def _tables(db):
    return list(load_database(db)["tables"])


# ---------------------------------------------------------------- SQLite
def test_inferred_relationships_for_undeclared_fk_database(dbs):
    s = load_database(dbs["sqlite"])
    got = {(r["from_table"], r["from_col"], r["to_table"], r["to_col"]) for r in s["relationships"]}
    for exp in [("OrderDetail", "OrderId", "Order", "Id"), ("OrderDetail", "ProductId", "Product", "Id"),
                ("Order", "CustomerId", "Customer", "Id"), ("Order", "EmployeeId", "Employee", "Id"),
                ("Product", "CategoryId", "Category", "Id"), ("Product", "SupplierId", "Supplier", "Id")]:
        assert exp in got
    assert all(r.get("inferred") for r in s["relationships"])


def test_valid_inferred_joins_accepted_and_arbitrary_joins_rejected(dbs):
    s = load_database(dbs["sqlite"])
    ok = ('SELECT p.ProductName, SUM(od.Quantity) FROM OrderDetail od JOIN Product p ON od.ProductId = p.Id '
          'JOIN "Order" o ON od.OrderId = o.Id GROUP BY p.ProductName')
    assert validate_sql(ok, s)["is_valid"]
    for bad in ["SELECT p.ProductName FROM OrderDetail od JOIN Product p ON od.Quantity = p.Id",
                'SELECT o.Id FROM "Order" o JOIN Product p ON o.Freight = p.UnitPrice',
                'SELECT o.Id FROM "Order" o JOIN Customer c ON o.EmployeeId = c.Id',
                "SELECT p.Id FROM Product p JOIN Category c ON p.UnitsInStock = c.Id"]:
        r = validate_sql(bad, s)
        assert not r["is_valid"] and any(i["type"] == "invalid_join_relationship" for i in r["issues"]), bad


def test_inference_requires_data_containment(tmp_path):
    db = tmp_path / "x.sqlite"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE Parent(Id INTEGER PRIMARY KEY, n TEXT);
    CREATE TABLE Child(Id INTEGER PRIMARY KEY, ParentId INTEGER);
    INSERT INTO Parent VALUES (1,'a'),(2,'b');
    INSERT INTO Child VALUES (1,1),(2,2),(3,99),(4,98),(5,97);""")
    con.commit(); con.close()
    assert load_database(str(db))["relationships"] == []  # orphans -> no inferred join


def test_declared_foreign_keys_untouched(tmp_path):
    db = tmp_path / "fk.sqlite"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE Parent(Id INTEGER PRIMARY KEY);
    CREATE TABLE Child(Id INTEGER PRIMARY KEY, ParentId INTEGER REFERENCES Parent(Id));
    INSERT INTO Parent VALUES (1),(2); INSERT INTO Child VALUES (1,1),(2,2);""")
    con.commit(); con.close()
    rels = load_database(str(db))["relationships"]
    assert len(rels) == 1 and not rels[0].get("inferred")


def test_inference_is_deterministic_and_read_only(dbs):
    before = Path(dbs["sqlite"]).read_bytes()
    a = load_database(dbs["sqlite"])["relationships"]
    b = load_database(dbs["sqlite"])["relationships"]
    assert a == b and Path(dbs["sqlite"]).read_bytes() == before


# ---------------------------------------------------------------- Excel / CSV ingestion
def test_excel_dates_are_date_only_and_range_inclusive(dbs, src_rows):
    sql = "SELECT SUM(revenue) FROM sales_sales WHERE order_date BETWEEN '2023-01-01' AND '2023-01-31'"
    res = execute_query(sql, dbs["xlsx"])
    exp = sum(r["revenue"] for r in src_rows if "2023-01-01" <= r["order_date"].isoformat() <= "2023-01-31")
    assert res["rows"][0][res["columns"][0]] == pytest.approx(exp)
    # the 2023-01-31 boundary row must be inside the result
    assert any(r["order_date"].isoformat() == "2023-01-31" for r in src_rows)
    con = sqlite3.connect(dbs["xlsx"])
    assert con.execute("SELECT MAX(length(order_date)) FROM sales_sales").fetchone()[0] == 10


def test_excel_multisheet_tables_present(dbs):
    assert set(_tables(dbs["xlsx"])) == {"sales_sales", "sales_targets"}


def test_csv_text_numbers_become_numeric(dbs, src_rows):
    s = load_database(dbs["csv"])["tables"]["sales"]
    types = {c["name"]: c["type"] for c in s["columns"]}
    assert types["revenue"] == "REAL" and types["margin_pct"] == "REAL" and types["growth_pct"] == "REAL"
    res = execute_query("SELECT SUM(revenue), MAX(revenue) FROM sales", dbs["csv"])
    row = res["rows"][0]
    assert list(row.values())[0] == pytest.approx(sum(r["revenue"] for r in src_rows))
    assert list(row.values())[1] == pytest.approx(max(r["revenue"] for r in src_rows))


def test_csv_dates_iso_and_ranges(dbs, src_rows):
    res = execute_query("SELECT COUNT(*) FROM sales WHERE order_date >= '2023-02-01' AND order_date <= '2023-02-28'", dbs["csv"])
    exp = sum(1 for r in src_rows if "2023-02-01" <= r["order_date"].isoformat() <= "2023-02-28")
    assert list(res["rows"][0].values())[0] == exp


def test_csv_percent_text_scale_and_header(dbs, src_rows):
    res = execute_query("SELECT AVG(margin_pct) FROM sales", dbs["csv"])
    exp = sum(r["margin"] for r in src_rows) / len(src_rows) * 100
    assert list(res["rows"][0].values())[0] == pytest.approx(exp, abs=0.01)


def test_leading_zero_ids_and_plain_text_not_converted(tmp_path):
    p = tmp_path / "z.csv"
    p.write_text("zip,name,amount\n01234,a,\"$1,000.00\"\n00999,b,\"$2,000.00\"\n")
    df = read_csv(str(p))
    assert list(df["zip"]) == ["01234", "00999"]
    assert list(df["amount"]) == [1000.0, 2000.0]


def test_ambiguous_slash_dates_left_untouched(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("d,v\n01/02/2023,1\n03/04/2023,2\n")
    assert list(read_csv(str(p))["d"]) == ["01/02/2023", "03/04/2023"]


def test_json_ingestion_unchanged(dbs, src_rows):
    con = sqlite3.connect(dbs["json"])
    assert con.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == len(src_rows)
    assert con.execute("SELECT SUM(revenue) FROM sales").fetchone()[0] == pytest.approx(sum(r["revenue"] for r in src_rows))
    cols = [r[1] for r in con.execute("PRAGMA table_info(sales)")]
    assert "margin" in cols and "margin_pct" not in cols


# ---------------------------------------------------------------- profile
def test_percentage_scale_profiled_per_format(dbs):
    x = load_database(dbs["xlsx"])["tables"]["sales_sales"]["column_profile"]
    c = load_database(dbs["csv"])["tables"]["sales"]["column_profile"]
    assert x["margin"]["scale"] == "fraction_0_1"
    assert c["margin_pct"]["scale"] == "percent_0_100"
    assert c["growth_pct"]["scale"] == "percent_0_100"
    assert x["order_date"]["kind"] == "date_text" and not x["order_date"]["has_time"]
    assert set(x["region"]["values"]) == {"North", "South", "East", "West"}


def test_prompt_contains_profile_and_rules(dbs):
    from modules.sql_generator import _schema_subset_text
    t = _schema_subset_text(load_database(dbs["xlsx"]))
    assert "SCALE=fraction_0_1" in t and "values=[" in t and "dates 2023-01-01" in t


def test_relationships_listed_before_tables_in_prompt(dbs):
    from modules.sql_generator import _schema_subset_text
    t = _schema_subset_text(load_database(dbs["sqlite"]))
    assert t.index("RELEVANT RELATIONSHIPS") < t.index("Table: ")


# ---------------------------------------------------------------- intent checker
@pytest.mark.parametrize("q,sql,expect", [
    ("total revenue in 2023", "SELECT SUM(revenue) FROM sales", True),
    ("total revenue in 2023", "SELECT SUM(revenue) FROM sales WHERE order_date >= '2023-01-01'", False),
    ("how many orders have margin above 30%", "SELECT COUNT(*) FROM sales_sales WHERE margin > 30", True),
    ("how many orders have margin above 30%", "SELECT COUNT(*) FROM sales_sales WHERE margin > 0.30", False),
    ("top 3 regions by revenue", "SELECT region, SUM(revenue) FROM sales GROUP BY region", True),
    ("average revenue by region", "SELECT region, SUM(revenue) FROM sales GROUP BY region", True),
])
def test_intent_checker(dbs, q, sql, expect):
    schema = load_database(dbs["xlsx"])
    sql = sql.replace("FROM sales ", "FROM sales_sales ").replace("FROM sales", "FROM sales_sales") if "sales_sales" not in sql else sql
    assert bool(check_intent(q, sql, schema)) is expect


def test_intent_repair_never_replaces_valid_sql_with_invalid(monkeypatch, dbs):
    import modules.sql_generator as g
    schema = load_database(dbs["xlsx"])
    good = "SELECT SUM(revenue) FROM sales_sales"
    monkeypatch.setattr(g, "_run_structured", lambda *a, **k: (type("P", (), dict(sql=good, explanation="e", confidence="High", complexity="Simple"))(), "{}", "groq:x", []))
    monkeypatch.setattr(g, "_repair_sql_with_llm", lambda *a, **k: ("SELECT nope FROM nowhere", "groq:x", []))
    out = g.generate_sql("total revenue in 2023", schema, "k")
    assert out["sql"] == good and out["validation"]["is_valid"]
    assert out["validation"]["intent_warnings"]


# ---------------------------------------------------------------- reliability / insight
def test_historical_dataset_not_flagged_stale():
    qr = {"columns": ["order_date", "revenue"], "rows": [{"order_date": "2019-03-01", "revenue": 1.0}, {"order_date": "2019-03-02", "revenue": 2.0}],
          "row_count": 2, "result_profile": {"row_count": 2, "null_counts": {}, "distinct_counts": {"order_date": 2, "revenue": 2}}}
    r = assess_reliability({"tables": {"t": {"columns": [{"name": "order_date", "type": "TEXT"}]}}}, {"is_valid": True}, qr)
    assert r["freshness"]["status"] == "PASS" and r["freshness"]["score"] == 100


def test_ranked_result_not_penalised_for_drift_or_uniqueness():
    rows = [{"region": n, "revenue": v} for n, v in [("a", 1000.0), ("b", 10.0), ("c", 9.0), ("d", 8.0)]]
    qr = {"columns": ["region", "revenue"], "rows": rows, "row_count": 4,
          "result_profile": {"row_count": 4, "null_counts": {}, "distinct_counts": {"region": 4, "revenue": 4}}}
    r = assess_reliability({"tables": {}}, {"is_valid": True}, qr)
    assert r["drift"]["status"] == "PASS" and r["uniqueness"]["status"] == "PASS"
    for key in ("completeness", "freshness", "integrity", "drift", "uniqueness"):
        assert key in r


def test_no_fake_trend_or_share_for_rankings_and_rates():
    rows = [{"region": "a", "avg_margin": 0.4}, {"region": "b", "avg_margin": 0.2}, {"region": "c", "avg_margin": 0.1}]
    qr = {"columns": ["region", "avg_margin"], "rows": rows, "row_count": 3, "result_profile": {"row_count": 3, "distinct_counts": {"region": 3}}}
    ins = generate_insights({}, qr, None, None)
    text = " ".join(ins["key_findings"] + ins["anomalies"])
    assert "contributes" not in text and "first to last" not in text


def test_time_series_trend_still_reported():
    rows = [{"month": f"2023-0{i}", "revenue": float(100 * i)} for i in range(1, 5)]
    qr = {"columns": ["month", "revenue"], "rows": rows, "row_count": 4, "result_profile": {"row_count": 4, "distinct_counts": {"month": 4}}}
    ins = generate_insights({}, qr, None, None)
    assert any("increased" in f for f in ins["key_findings"])


# ---------------------------------------------------------------- benchmark
def _bench(dbs, src_rows):
    return {
        "excel": (dbs["xlsx"], fb.tabular_cases(src_rows, "sales_sales", "margin", True)),
        "csv": (dbs["csv"], fb.tabular_cases(src_rows, "sales", "margin_pct", False)),
        "json": (dbs["json"], fb.tabular_cases(src_rows, "sales", "margin", True)),
        "sqlite": (dbs["sqlite"], fb.northwind_cases(dbs["sqlite"])),
    }


@pytest.mark.parametrize("fmt", ["excel", "csv", "json", "sqlite"])
@pytest.mark.parametrize("heldout", [False, True])
def test_per_format_benchmark_at_least_99pct(dbs, src_rows, fmt, heldout, tmp_path):
    db, cases = _bench(dbs, src_rows)[fmt]
    if heldout:
        cases = fb.with_holdout(cases)
    rep = fb.run_benchmark(db, cases)
    (tmp_path / f"{fmt}.json").write_text(json.dumps(rep, default=str))
    failures = [c for c in rep["cases"] if not c["correct"]]
    assert rep["accuracy"] >= 99.0, failures


# ---------------------------------------------------------------- optional real user fixtures
@pytest.mark.skipif(not (USER / "northwind_small.sqlite").exists(), reason="real northwind_small.sqlite not supplied")
def test_real_northwind_inferred_relationships_are_sound():
    p = str(USER / "northwind_small.sqlite")
    s = load_database(p)
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    for r in s["relationships"]:
        bad = con.execute(f'SELECT COUNT(*) FROM "{r["from_table"]}" WHERE "{r["from_col"]}" IS NOT NULL AND "{r["from_col"]}" NOT IN (SELECT "{r["to_col"]}" FROM "{r["to_table"]}")').fetchone()[0]
        share = bad / max(1, s["tables"][r["from_table"]]["row_count"])
        if bad == 0 or r.get("inferred") is False:
            continue
        # Since v1.0.8 a <table>Id -> <table>.<primary key> name match may carry orphans (Northwind: 29 of 830 orders
        # reference a missing customer). Every other inferred join must still be essentially orphan-free, and the
        # orphan count must be recorded so the validator can warn that an inner join drops those rows.
        named_fk = r["from_col"].lower() in (r["to_table"].lower() + "id", r["to_table"].lower() + "_id")
        assert (named_fk and share <= 0.2 and r.get("unmatched_rows") == bad) or share <= 0.01, (r, bad)


@pytest.mark.parametrize("name", ["sales.xlsx", "sales.csv", "sales.json"])
def test_real_user_files_ingest_and_profile(name, tmp_path):
    p = USER / name
    if not p.exists():
        pytest.skip(f"{name} not supplied")
    db = tmp_path / "u.db"
    tabs = ingest_to_sqlite(str(p), p.suffix, str(db), "user")
    s = load_database(str(db))
    assert tabs and all(s["tables"][t]["row_count"] > 0 and "column_profile" in s["tables"][t] for t in tabs)
