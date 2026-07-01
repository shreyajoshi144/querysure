# QuerySure - Trust First AI Analytics for Natural Language SQL

QuerySure is a natural-language-to-SQL analytics pipeline that validates generated SQL before execution, scores result reliability, and turns trusted query output into business insights. It is built for retail analytics, but the architecture is general enough to adapt to other relational datasets.

## Why QuerySure

Most "ask your database" tools treat the LLM as the final authority. QuerySure treats the LLM as one stage in a larger analytics pipeline:

- It builds schema context and a semantic layer before generation.
- It retrieves only the most relevant tables, columns, metrics, and join hints with TF-IDF.
- It validates generated SQL against the real schema and blocks risky patterns.
- It executes only read-only queries with row limits.
- It scores returned data for completeness, freshness, integrity, drift, and uniqueness before generating insight text.

That makes the system more useful for analytics workflows where a wrong join or hallucinated column can produce a very confident, very wrong answer.

<img width="1470" height="956" alt="Screenshot 2026-07-01 at 11 19 06 PM" src="https://github.com/user-attachments/assets/cc77b89b-d0e3-4189-8d36-7a1dccdf25c7" />

## What it does

Ask a plain-English business question and QuerySure returns validated SQL, a safety and reliability report, and a business insight summary — all traceable back to the schema context used to generate the answer.

<img width="1635" height="962" alt="querysure" src="https://github.com/user-attachments/assets/bd1df429-3dc1-4d8a-80c7-078b494f8b21" />

**Example**

- **Question:** What is the return rate by store region?
- **Output:** validated SQL → risk score → reliability score → business insight summary

## Demo Visuals 

<img width="1470" height="956" alt="Screenshot 2026-07-01 at 11 25 40 PM" src="https://github.com/user-attachments/assets/1f26674d-c8cc-4c75-b031-6710794e16f3" />

<img width="1470" height="956" alt="Screenshot 2026-07-01 at 11 20 43 PM" src="https://github.com/user-attachments/assets/f45c620d-621f-4391-bdbe-7bb282575178" />

<img width="1470" height="956" alt="Screenshot 2026-07-01 at 11 21 38 PM" src="https://github.com/user-attachments/assets/4c900197-5c9c-40f5-bff5-6f90ebb8788e" />

 <img width="1470" height="956" alt="Screenshot 2026-07-01 at 11 22 00 PM" src="https://github.com/user-attachments/assets/7e0d6383-c9d6-49a4-bd76-8aeed6cc2ee8" />

## Core modules

| Module | Responsibility |
|---|---|
| `api.py` | FastAPI server and API orchestration |
| `schema_engine.py` | Loads SQLite metadata, foreign keys, sample rows, and column statistics |
| `semantic_layer.py` | Maps technical columns to business meanings and metric definitions |
| `retrieval_engine.py` | TF-IDF retrieval for relevant schema and semantic context |
| `sql_generator.py` | Uses the LLM to generate SQLite SQL from retrieved context |
| `sql_validator.py` | Checks schema consistency, joins, dangerous keywords, `SELECT *`, and aggregation issues |
| `query_result_engine.py` | Executes read-only SQL and builds result profiling metadata |
| `data_reliability_agent.py` | Scores completeness, freshness, integrity, drift, and uniqueness |
| `business_insight_agent.py` | Generates KPI-aware findings, anomalies, and recommendations |
| `report_generator.py` | Produces JSON, HTML, CSV, and PDF artifacts |

## Quick start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure your API key

```bash
cp .env.example .env
# add your Groq API key
```

### 3. Run the app

```bash
python api.py
```

Then open `http://localhost:5000`.

A sample retail database is included, so the project can be tested immediately without extra setup.

## Example questions

- Which product categories generated the highest revenue?
- Show me the top 5 customers by total spend.
- What is the return rate by store region?
- Which stores had the most orders in 2023?

## Validation flow

QuerySure is designed to reject bad SQL early.

1. The system retrieves only relevant tables, columns, and metric definitions.
2. The generator produces SQL from that constrained context.
3. The validator checks for unknown tables and columns, invalid joins, `SELECT *`, dangerous SQL keywords, and aggregation issues.
4. The execution layer allows only `SELECT` and `WITH` queries and applies row limits.
5. The reliability layer scores the returned data before the insight layer writes a narrative.

That separation matters because valid SQL is not the same as trustworthy output.

## Failure cases

A query can be rejected before execution if it refers to a table or column that does not exist in the schema, uses an invalid join relationship, or includes a suspicious SQL pattern. In those cases, the system returns a validation failure instead of silently producing a misleading answer.

Common rejection reasons include:

- Invalid table or column reference
- Invalid join relationship
- Dangerous SQL pattern
- `SELECT *` usage
- Aggregation issues without a proper `GROUP BY`
  
## Project structure

```text
querysure/
├── api.py
├── retail.db
├── requirements.txt
├── .env.example
├── modules/
│   ├── schema_engine.py
│   ├── semantic_layer.py
│   ├── retrieval_engine.py
│   ├── sql_generator.py
│   ├── sql_validator.py
│   ├── query_result_engine.py
│   ├── data_reliability_agent.py
│   ├── business_insight_agent.py
│   └── report_generator.py
├── static/
│   └── index.html
└── output/
```

## Tech stack

- Python
- FastAPI
- SQLite
- Pandas
- Groq API
- TF-IDF retrieval
- CSS custom properties

## Known limitations

- SQL validation is regex-based, not a full AST parser.
- The app is session-scoped and in-memory.
- There is no multi-user authentication layer.
- There is no fallback provider if the LLM API is unavailable.
- Freshness and drift scoring use heuristics rather than learned baselines.

