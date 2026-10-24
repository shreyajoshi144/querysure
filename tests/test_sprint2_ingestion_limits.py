from __future__ import annotations

import io
import zipfile

import openpyxl
import pytest

from modules import ingestion


def test_csv_row_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(ingestion, "MAX_INGEST_ROWS", 2)
    p = tmp_path / "x.csv"
    p.write_text("a,b\n1,2\n3,4\n5,6\n")
    with pytest.raises(ValueError, match="more than 2 rows"):
        ingestion.read_csv(str(p))


def test_json_column_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(ingestion, "MAX_INGEST_COLUMNS", 1)
    p = tmp_path / "x.json"
    p.write_text('[{"a":1,"b":2}]')
    with pytest.raises(ValueError, match="more than 1 columns"):
        ingestion.read_json(str(p))


def test_xlsx_sheet_and_zip_bomb_guards(monkeypatch, tmp_path):
    monkeypatch.setattr(ingestion, "MAX_INGEST_SHEETS", 1)
    p = tmp_path / "x.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "one"
    ws.append(["a", "b"])
    ws.append([1, 2])
    wb.create_sheet("two")
    wb.save(p)
    with pytest.raises(ValueError, match="more than 1 sheets"):
        ingestion.read_excel(str(p))

    bomb = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/sharedStrings.xml", b"A" * 10000)
    monkeypatch.setattr(ingestion, "MAX_XLSX_COMPRESSION_RATIO", 2.0)
    with pytest.raises(ValueError, match="compression ratio"):
        ingestion.read_excel(str(bomb))


def test_sqlite_row_cap(monkeypatch, tmp_path):
    import sqlite3
    monkeypatch.setattr(ingestion, "MAX_INGEST_ROWS", 2)
    p = tmp_path / "x.sqlite"
    c = sqlite3.connect(p)
    c.execute("create table t (a integer)")
    c.executemany("insert into t values (?)", [(1,), (2,), (3,)])
    c.commit(); c.close()
    with pytest.raises(ValueError, match="more than 2 rows"):
        ingestion.validate_sqlite(str(p))


def test_json_row_cap_rejects_before_dataframe_creation(monkeypatch, tmp_path):
    monkeypatch.setattr(ingestion, "MAX_INGEST_ROWS", 3)
    p = tmp_path / "too_many.json"
    p.write_text("[" + ",".join('{"a":%d}' % i for i in range(4)) + "]")

    def fail_dataframe(*args, **kwargs):
        raise AssertionError("DataFrame creation must not run for oversized JSON")

    monkeypatch.setattr(ingestion.pd, "read_json", fail_dataframe)
    with pytest.raises(ValueError, match="more than 3 rows"):
        ingestion.read_json(str(p))


def test_json_small_file_still_ingests_without_record_list(monkeypatch, tmp_path):
    p = tmp_path / "small.json"
    p.write_text('[{"a":1,"b":"x"},{"a":2,"b":"y"}]')
    seen = {}
    original = ingestion.pd.read_json

    def wrapped_read_json(path, *args, **kwargs):
        seen["path"] = str(path)
        assert str(path).endswith(".jsonl")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(ingestion.pd, "read_json", wrapped_read_json)
    df = ingestion.read_json(str(p))
    assert list(df.columns) == ["a", "b"]
    assert df.to_dict("records") == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    assert not __import__("os").path.exists(seen["path"])
