"""
Runs the AI analytics pipeline:
Connect -> Schema -> Semantic -> Retrieval -> SQL -> Validation -> Execution -> Reliability -> Insights -> Report
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid

import pandas as pd
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, ".env"))

from modules import (
    load_database,
    build_semantic_layer,
    build_vector_store,
    retrieve_context,
    generate_sql,
    validate_sql,
    execute_query,
    assess_reliability,
    generate_insights,
    generate_report,
)

app = FastAPI(title="DataSense AI", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

UPLOAD_DIR = os.path.join(ROOT, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
_root_db = os.path.join(ROOT, "retail.db")
_sample_db = os.path.join(ROOT, "sample_db", "retail.db")
SAMPLE_DB = _root_db if os.path.exists(_root_db) else _sample_db
STATIC_DIR = os.path.join(ROOT, "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

sessions: dict = {}


def err_response(msg: str, code: int = 400):
    return JSONResponse(status_code=code, content={"success": False, "error": msg})


def ok_response(data: dict):
    data["success"] = True
    return JSONResponse(content=data)


def get_session(sid: str) -> dict:
    if sid not in sessions:
        sessions[sid] = {
            "db_path": None,
            "schema": None,
            "semantic": None,
            "retrieval": None,
            "sql_result": None,
            "sql": None,
            "question": None,
            "validation": None,
            "query_result": None,
            "reliability": None,
            "insights": None,
        }
    return sessions[sid]


def _schema_summary(schema: dict) -> list:
    out = []
    for name, info in schema["tables"].items():
        out.append({
            "name": name,
            "rows": info["row_count"],
            "columns": [
                {
                    "name": c["name"],
                    "type": c["type"],
                    "pk": c["pk"],
                    "fk": any(fk["from"] == c["name"] for fk in info["foreign_keys"]),
                }
                for c in info["columns"]
            ],
            "foreign_keys": info["foreign_keys"],
        })
    return out


def _calc_health(schema: dict) -> int:
    score = 100
    for _, info in schema["tables"].items():
        has_pk = any(c["pk"] for c in info["columns"])
        if not has_pk:
            score -= 5
        if info["row_count"] == 0:
            score -= 5
    return max(0, min(100, score))


@app.get("/")
def index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse({"message": "DataSense AI API is running. Place index.html in /static/"})


@app.post("/api/session")
async def new_session():
    sid = str(uuid.uuid4())
    sessions[sid] = {
        "db_path": None,
        "schema": None,
        "semantic": None,
        "retrieval": None,
        "sql_result": None,
        "sql": None,
        "question": None,
        "validation": None,
        "query_result": None,
        "reliability": None,
        "insights": None,
    }
    return ok_response({"session_id": sid})


@app.post("/api/connect/sample")
async def connect_sample(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = body.get("session_id", "default")

    if not os.path.exists(SAMPLE_DB):
        return err_response("Sample database not found. Run: python sample_db/create_retail.py")

    sess = get_session(sid)
    sess["db_path"] = SAMPLE_DB
    schema = load_database(SAMPLE_DB)
    sess["schema"] = schema
    sess["semantic"] = build_semantic_layer(schema)
    build_vector_store(sess["semantic"])

    return ok_response({
        "db_name": os.path.basename(SAMPLE_DB),
        "table_count": schema["table_count"],
        "total_rows": schema["total_rows"],
        "relationships": len(schema["relationships"]),
        "db_size": schema["db_size"],
        "tables": _schema_summary(schema),
        "health_score": _calc_health(schema),
    })


@app.post("/api/connect/upload")
async def connect_upload(
    file: UploadFile = File(...),
    session_id: str = Form("default"),
):
    fname = file.filename or "upload.db"
    ext = os.path.splitext(fname)[1].lower()
    path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}{ext}")
    contents = await file.read()

    if ext == ".csv":
        import io
        df = pd.read_csv(io.BytesIO(contents))
        path = path.replace(".csv", ".db")
        conn = sqlite3.connect(path)
        table_name = os.path.splitext(fname)[0].replace(" ", "_").lower()[:50]
        df.to_sql(table_name, conn, if_exists="replace", index=False)
        conn.close()
    elif ext in (".db", ".sqlite", ".sqlite3"):
        with open(path, "wb") as f:
            f.write(contents)
    else:
        return err_response("Unsupported file type. Upload a .db, .sqlite, or .csv file.")

    sess = get_session(session_id)
    sess["db_path"] = path
    schema = load_database(path)
    sess["schema"] = schema
    sess["semantic"] = build_semantic_layer(schema)
    build_vector_store(sess["semantic"])

    return ok_response({
        "db_name": fname,
        "table_count": schema["table_count"],
        "total_rows": schema["total_rows"],
        "relationships": len(schema["relationships"]),
        "db_size": schema["db_size"],
        "tables": _schema_summary(schema),
        "health_score": _calc_health(schema),
    })


@app.post("/api/schema-intelligence")
async def schema_intelligence(request: Request):
    body = await request.json()
    sess = get_session(body.get("session_id", "default"))
    if not sess.get("schema"):
        return err_response("No schema loaded")
    profiles = []
    for name, info in sess["schema"].get("tables", {}).items():
        profiles.append({
            "table": name,
            "row_count": info["row_count"],
            "column_count": len(info["columns"]),
            "has_primary_key": any(c["pk"] for c in info["columns"]),
            "foreign_key_count": len(info["foreign_keys"]),
        })
    return ok_response({"table_profiles": profiles, "semantic_preview": sess.get("semantic")})


@app.post("/api/semantic-layer")
async def api_semantic_layer(request: Request):
    body = await request.json()
    sess = get_session(body.get("session_id", "default"))
    if not sess.get("schema"):
        return err_response("No schema loaded")
    sess["semantic"] = build_semantic_layer(sess["schema"])
    build_vector_store(sess["semantic"])
    return ok_response(sess["semantic"])


@app.post("/api/retrieve-tables")
async def api_retrieve_tables(request: Request):
    body = await request.json()
    sess = get_session(body.get("session_id", "default"))
    question = (body.get("question") or "").strip()
    if not sess.get("schema"):
        return err_response("No database connected")
    if not question:
        return err_response("Question is required")
    retrieval = retrieve_context(question, sess.get("semantic") or build_semantic_layer(sess["schema"]))
    sess["retrieval"] = retrieval
    return ok_response(retrieval)


@app.post("/api/generate-sql")
async def api_generate_sql(request: Request):
    body = await request.json()
    sess = get_session(body.get("session_id", "default"))
    question = (body.get("question") or "").strip()
    api_key = (body.get("api_key") or os.environ.get("GROQ_API_KEY") or "").strip()
    if not question:
        return err_response("Business question is required")
    if not api_key:
        return err_response("Groq API key is required. Set it in the UI or in .env as GROQ_API_KEY.")
    if not sess.get("schema"):
        return err_response("No database connected. Connect a database first.")
    retrieval = sess.get("retrieval") or retrieve_context(question, sess.get("semantic") or build_semantic_layer(sess["schema"]))
    sess["retrieval"] = retrieval
    result = generate_sql(question=question, schema=sess["schema"], api_key=api_key, semantic=sess.get("semantic"), retrieval=retrieval)
    if not result.get("success", True) and result.get("error"):
        return err_response(result["error"])
    sess["sql"] = result.get("sql")
    sess["sql_result"] = result
    sess["question"] = question
    tables_used = [t for t in sess["schema"].get("tables", {}) if t.upper() in (result.get("sql", "").upper())]
    return ok_response({
        "sql": result.get("sql"),
        "explanation": result.get("explanation"),
        "confidence": result.get("confidence"),
        "complexity": result.get("complexity"),
        "tables_used": tables_used,
        "retrieval_confidence": retrieval.get("confidence") if isinstance(retrieval, dict) else None,
    })


@app.post("/api/validate-sql")
async def api_validate_sql(request: Request):
    body = await request.json()
    sess = get_session(body.get("session_id", "default"))
    sql = (body.get("sql") or sess.get("sql") or "").strip()
    if not sql:
        return err_response("No SQL to validate")
    result = validate_sql(sql, schema=sess.get("schema") or {}, semantic=sess.get("semantic"))
    sess["validation"] = result
    return ok_response(result)


@app.post("/api/execute-sql")
async def api_execute_sql(request: Request):
    body = await request.json()
    sess = get_session(body.get("session_id", "default"))
    sql = (body.get("sql") or sess.get("sql") or "").strip()
    if sql:
        sess["sql"] = sql
    if not sess.get("db_path"):
        return err_response("No database connected")
    if not sess.get("sql"):
        return err_response("No SQL to execute")
    if sess.get("validation") and not sess["validation"].get("is_valid", True):
        return err_response("SQL validation failed.")
    exec_result = execute_query(sql=sess["sql"], db_path=sess["db_path"])
    if not exec_result.get("success", False):
        return err_response(exec_result.get("error", "Query execution failed"))
    sess["query_result"] = exec_result
    return ok_response(exec_result)


@app.post("/api/analyze-reliability")
async def api_analyze_reliability(request: Request):
    body = await request.json()
    sess = get_session(body.get("session_id", "default"))
    if not sess.get("validation") or not sess.get("query_result"):
        return err_response("Validation and query result are required")
    result = assess_reliability(schema=sess["schema"], validation_result=sess["validation"], query_result=sess["query_result"])
    sess["reliability"] = result
    return ok_response(result)


@app.post("/api/generate-insights")
async def api_generate_insights(request: Request):
    body = await request.json()
    sess = get_session(body.get("session_id", "default"))
    if not sess.get("query_result"):
        return err_response("No query results provided")
    result = generate_insights(
        schema=sess.get("schema") or {},
        query_result=sess["query_result"],
        reliability_result=sess.get("reliability"),
        semantic=sess.get("semantic"),
    )
    sess["insights"] = result
    return ok_response(result)


@app.post("/api/generate-report")
async def api_generate_report(request: Request):
    body = await request.json()
    sess = get_session(body.get("session_id", "default"))
    if not (sess.get("question") and sess.get("sql_result") and sess.get("validation") and sess.get("query_result") and sess.get("reliability") and sess.get("insights")):
        return err_response("Run the full pipeline before generating a report")
    report = generate_report(
        question=sess["question"],
        sql_result=sess.get("sql_result") or {"sql": sess["sql"]},
        validation_result=sess["validation"],
        query_result=sess["query_result"],
        reliability_result=sess["reliability"],
        insight_result=sess["insights"],
        semantic=sess.get("semantic"),
        output_dir=os.path.join(ROOT, "output"),
    )
    return ok_response(report)


@app.get("/api/health")
def health():
    return ok_response({
        "status": "ok",
        "modules": {
            "schema": True,
            "semantic": True,
            "retrieval": True,
            "generator": True,
            "validator": True,
            "execution": True,
            "reliability": True,
            "insight": True,
            "report": True,
        },
        "sample_db_exists": os.path.exists(SAMPLE_DB),
        "env_key_configured": bool((os.environ.get("GROQ_API_KEY") or "").strip()),
    })


if __name__ == "__main__":
    uvicorn.run("api:app", host="0.0.0.0", port=5000, reload=True)