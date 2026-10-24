"""Multi-format, multilingual evaluation of the deterministic pipeline.

Each case pairs a natural-language question (in several languages and phrasings) with candidate SQL written the way a
capable model would write it, and an expected result computed INDEPENDENTLY from the raw source file (pandas / json /
openpyxl / sqlite3 table reads, never QuerySure's SQL or ingestion). The runner drives QuerySure's real upload path:
ingestion -> schema profile -> semantic layer -> retrieval -> generate_sql (planning, grounding, validation, semantic
checks, verification, repair loop) -> the /api/execute-sql re-validation and sandbox execution.

What it measures: given correct SQL, does QuerySure accept it, run it, return exactly the independently computed answer,
and avoid false alarms (bogus plan/intent warnings, false verification mismatches, needless repair requests)?

What it does NOT measure: whether an LLM writes that SQL from the question. That needs `python -m evaluation live`
with a provider key.
"""
