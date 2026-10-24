"""Deterministic golden coverage for all supported ingestion formats.

This supplements, but does not alter, the immutable 62-case benchmark.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
from pathlib import Path

import openpyxl
import pandas as pd

from modules import build_semantic_layer, execute_query, load_database, retrieve_context, validate_sql
from modules.ingestion import ingest_to_sqlite


FIXTURES = {
    "csv": {
        "filename": "sales.csv",
        "table": "sales",
        "columns": ["id", "region", "amount"],
        "rows": 3,
        "sql": "SELECT region, SUM(amount) AS total_amount FROM sales GROUP BY region ORDER BY region",
    },
    "json": {
        "filename": "sales.json",
        "table": "sales",
        "columns": ["id", "region", "amount"],
        "rows": 3,
        "sql": "SELECT region, SUM(amount) AS total_amount FROM sales GROUP BY region ORDER BY region",
    },
    "xlsx": {
        "filename": "sales.xlsx",
        "table": "sales",
        "columns": ["id", "region", "amount"],
        "rows": 3,
        "sql": "SELECT region, SUM(amount) AS total_amount FROM sales GROUP BY region ORDER BY region",
    },
    "xlsm": {
        "filename": "sales.xlsm",
        "table": "sales",
        "columns": ["id", "region", "amount"],
        "rows": 3,
        "sql": "SELECT region, SUM(amount) AS total_amount FROM sales GROUP BY region ORDER BY region",
    },
    "sqlite": {
        "filename": "sales.sqlite3",
        "table": "sales",
        "columns": ["id", "region", "amount"],
        "rows": 3,
        "sql": "SELECT region, SUM(amount) AS total_amount FROM sales GROUP BY region ORDER BY region",
    },
}


def _make_source(tmp_path: Path, kind: str) -> tuple[Path, str]:
    meta = FIXTURES[kind]
    src = tmp_path / meta["filename"]
    df = pd.DataFrame({"id": [1, 2, 3], "region": ["North", "South", "North"], "amount": [10.0, 20.0, 30.0]})
    if kind == "csv":
        df.to_csv(src, index=False)
    elif kind == "json":
        src.write_text(json.dumps(df.to_dict(orient="records")), encoding="utf-8")
    elif kind in {"xlsx", "xlsm"}:
        if kind == "xlsm":
            # openpyxl-generated workbook is structurally valid for the .xlsm ingestion path.
            with pd.ExcelWriter(src, engine="openpyxl") as writer:
                pd.DataFrame([["Sales report"], [], ["id", "region", "amount"], [1, "North", 10], [2, "South", 20], [3, "North", 30]]).to_excel(writer, index=False, header=False)
        else:
            with pd.ExcelWriter(src, engine="openpyxl") as writer:
                pd.DataFrame([["Sales report"], [], ["id", "region", "amount"], [1, "North", 10], [2, "South", 20], [3, "North", 30]]).to_excel(writer, index=False, header=False)
    else:
        conn = sqlite3.connect(src)
        df.to_sql("sales", conn, index=False)
        conn.close()
        return src, "sales"
    return src, "sales"


def test_four_format_golden_contract(tmp_path):
    for kind in ("csv", "json", "xlsx", "xlsm", "sqlite"):
        src, _ = _make_source(tmp_path, kind)
        dest = tmp_path / f"{kind}.db"
        if kind == "sqlite":
            normalized = str(src)
        else:
            ingest_to_sqlite(str(src), src.suffix, str(dest), src.stem)
            normalized = str(dest)

        schema = load_database(normalized)
        meta = FIXTURES[kind]
        assert list(schema["tables"])[0] == meta["table"]
        info = schema["tables"][meta["table"]]
        assert [c["name"] for c in info["columns"]] == meta["columns"]
        assert info["row_count"] == meta["rows"]
        assert all("Unnamed:" not in c["name"] for c in info["columns"])
        assert all(c in info["column_stats"] for c in meta["columns"])
        assert all("null_count" in info["column_stats"][c] and "distinct" in info["column_stats"][c] for c in meta["columns"])

        semantic = build_semantic_layer(schema)
        retrieval = retrieve_context("total amount by region", semantic, cache_key=f"golden-{kind}")
        assert "sales" in retrieval.get("relevant_tables", [])

        validation = validate_sql(meta["sql"], schema, semantic)
        assert validation["is_valid"] is True
        result = execute_query(meta["sql"], normalized, max_rows=100)
        assert result["success"] is True
        assert result["row_count"] == 2
        assert result["rows"][0]["region"] == "North"
        assert result["rows"][0]["total_amount"] == 40


def test_excel_header_detection_and_multiple_sheets(tmp_path):
    src = tmp_path / "multi.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Orders"
    ws.append(["Quarterly Orders"])
    ws.append([])
    ws.append(["order_id", "amount"])
    ws.append([1, 10])
    ws.append([2, 20])
    ws2 = wb.create_sheet("Customers")
    ws2.append(["Customer Export"])
    ws2.append(["customer_id", "name"])
    ws2.append([1, "A"])
    ws2.append([2, "B"])
    wb.save(src)

    dest = tmp_path / "multi.db"
    tables = ingest_to_sqlite(str(src), ".xlsx", str(dest), "multi")
    assert tables == ["multi_orders", "multi_customers"]
    schema = load_database(str(dest))
    assert schema["tables"]["multi_orders"]["row_count"] == 2
    assert schema["tables"]["multi_customers"]["row_count"] == 2
    assert all("Unnamed:" not in c["name"] for info in schema["tables"].values() for c in info["columns"])
