"""
Semantic Metadata Layer
Transforms raw schema into a business knowledge base.
Maps technical column names to human meanings, domain glossary, metric definitions, and retrieval-ready semantic documents.
"""

from __future__ import annotations
from typing import Any, Dict, Optional

DOMAIN_GLOSSARY: Dict[str, Dict[str, str]] = {
    "unit_price": {"meaning": "Price paid per single item", "domain": "Finance", "usage": "Revenue calculations, margin analysis"},
    "total_price": {"meaning": "Total value of a line item (qty × unit_price)", "domain": "Finance", "usage": "Revenue aggregation"},
    "price": {"meaning": "Monetary value of a product or transaction", "domain": "Finance", "usage": "Revenue, pricing analysis"},
    "revenue": {"meaning": "Total income generated from sales", "domain": "Finance", "usage": "Business performance KPIs"},
    "amount": {"meaning": "Monetary transaction value", "domain": "Finance", "usage": "Financial summaries"},
    "discount": {"meaning": "Price reduction applied to an order", "domain": "Finance", "usage": "Promotion analysis"},
    "tax": {"meaning": "Government levy applied to a transaction", "domain": "Finance", "usage": "Compliance reporting"},
    "status": {"meaning": "Current state in the order lifecycle", "domain": "Operations", "usage": "Order tracking, funnel analysis", "values": "completed, returned, pending, cancelled, processing"},
    "order_date": {"meaning": "Date when the customer placed the order", "domain": "Operations", "usage": "Time-series analysis, seasonality"},
    "ship_date": {"meaning": "Date when the order was dispatched", "domain": "Operations", "usage": "Fulfillment efficiency"},
    "created_at": {"meaning": "Timestamp of record creation", "domain": "System", "usage": "Audit, time-series analysis"},
    "updated_at": {"meaning": "Timestamp of last modification", "domain": "System", "usage": "Data freshness checks"},
    "customer_id": {"meaning": "Unique identifier for a customer", "domain": "Customer", "usage": "Joins, customer segmentation"},
    "customer_name": {"meaning": "Full name of the customer", "domain": "Customer", "usage": "Display, reporting"},
    "email": {"meaning": "Customer email address", "domain": "Customer", "usage": "Communication, deduplication"},
    "segment": {"meaning": "Customer classification tier", "domain": "Customer", "usage": "Cohort analysis, targeted marketing"},
    "region": {"meaning": "Geographic region of the customer", "domain": "Customer", "usage": "Geographic analysis"},
    "product_id": {"meaning": "Unique identifier for a product", "domain": "Product", "usage": "Joins, product performance"},
    "product_name": {"meaning": "Commercial name of the product", "domain": "Product", "usage": "Display, reporting"},
    "category": {"meaning": "Product classification group", "domain": "Product", "usage": "Category analysis, merchandising"},
    "brand": {"meaning": "Manufacturer or brand name", "domain": "Product", "usage": "Brand performance reporting"},
    "quantity": {"meaning": "Number of units in a transaction", "domain": "Product", "usage": "Volume analysis, inventory"},
    "stock": {"meaning": "Units available in inventory", "domain": "Product", "usage": "Inventory management"},
    "store_id": {"meaning": "Unique identifier for a physical store", "domain": "Store", "usage": "Store performance, geographic analysis"},
    "store_name": {"meaning": "Name of the retail location", "domain": "Store", "usage": "Reporting, display"},
    "city": {"meaning": "City where the store or customer is located", "domain": "Location", "usage": "Geographic analysis"},
    "country": {"meaning": "Country of the store or customer", "domain": "Location", "usage": "International analysis"},
}

METRIC_DEFINITIONS: Dict[str, str] = {
    "total_revenue": "SUM(unit_price * quantity) — Total income from all sales",
    "average_order_value": "AVG(order_total) — Mean spend per transaction",
    "return_rate": "COUNT(status='returned') / COUNT(*) — Proportion of returned orders",
    "customer_ltv": "SUM(revenue) per customer — Lifetime value of a customer",
    "gross_margin": "(revenue - cost) / revenue — Profitability ratio",
    "items_per_order": "AVG(quantity) per order — Average basket size",
    "top_n_by_revenue": "ORDER BY SUM(revenue) DESC LIMIT N — Rank by total revenue",
    "month_over_month": "strftime('%Y-%m', date_col) GROUP BY — Monthly time series",
    "year_over_year": "strftime('%Y', date_col) GROUP BY — Annual time series",
}

ROLE_PRIORITY = {
    "metric": 10,
    "dimension": 9,
    "fact": 9,
    "date": 8,
    "identifier": 8,
    "attribute": 5,
    "status": 6,
    "measure": 10,
}

def _semantic_role(col_name: str, col_type: str, is_pk: bool, is_fk: bool) -> str:
    n = col_name.lower()
    t = col_type.upper()
    if is_pk or n == "id" or n.endswith("_id"):
        return "identifier"
    if any(x in n for x in ["date", "time", "created_at", "updated_at", "ship_date", "order_date"]):
        return "date"
    if any(x in n for x in ["price", "amount", "revenue", "cost", "total", "fee", "discount", "tax"]):
        return "metric"
    if n in ("status", "segment", "category", "brand", "region", "city", "country"):
        return "dimension"
    if is_fk:
        return "dimension"
    if t in ("INTEGER", "INT", "BIGINT", "REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC"):
        return "measure"
    return "attribute"

def _business_priority(col_name: str, col_type: str, role: str) -> int:
    n = col_name.lower()
    score = 1
    for key, val in [
        ("revenue", 10), ("sales", 10), ("amount", 10), ("price", 9), ("cost", 9),
        ("customer_id", 8), ("product_id", 8), ("order_id", 8), ("status", 6),
        ("created_at", 4), ("updated_at", 4), ("date", 4), ("category", 7),
        ("region", 7), ("segment", 7), ("quantity", 8),
    ]:
        if key in n:
            score = max(score, val)
    score = max(score, ROLE_PRIORITY.get(role, 3))
    if col_type.upper() in ("TEXT", "VARCHAR", "CHAR", "STRING") and role == "dimension":
        score = max(score, 6)
    return min(score, 10)


def _infer_column_meaning(col_name: str, col_type: str, is_pk: bool, is_fk: bool) -> Dict[str, Any]:
    name_lower = col_name.lower()
    if name_lower in DOMAIN_GLOSSARY:
        entry = DOMAIN_GLOSSARY[name_lower].copy()
    else:
        entry = None
        for key, val in DOMAIN_GLOSSARY.items():
            if key in name_lower or name_lower.endswith(key.split("_")[-1]):
                entry = val.copy()
                entry["inferred"] = True
                break

    role = _semantic_role(col_name, col_type, is_pk, is_fk)
    business_priority = _business_priority(col_name, col_type, role)

    if entry is None:
        domain = "General"
        meaning = col_name.replace("_", " ").title()
        if role in ("metric", "measure"):
            domain = "Finance" if any(x in name_lower for x in ["price", "amount", "revenue", "cost", "total", "fee", "discount", "tax"]) else "Numeric"
            meaning = f"Monetary value: {col_name.replace('_', ' ')}" if domain == "Finance" else f"Numeric measure: {col_name.replace('_', ' ')}"
        elif role == "date":
            domain = "Temporal"
            meaning = f"Date/time field: {col_name.replace('_', ' ')}"
        elif role == "identifier":
            domain = "Identity"
            meaning = f"Unique identifier: {col_name.replace('_id', '').replace('_', ' ').title()}"
        elif role == "dimension":
            domain = "Descriptive"
            meaning = f"Categorical descriptor: {col_name.replace('_', ' ')}"
        elif col_type.upper() in ("TEXT", "VARCHAR", "CHAR", "STRING"):
            domain = "Descriptive"
            meaning = f"Text attribute: {col_name.replace('_', ' ')}"
        elif col_type.upper() in ("INTEGER", "INT", "BIGINT"):
            domain = "Numeric"
            meaning = f"Integer value: {col_name.replace('_', ' ')}"
        elif col_type.upper() in ("REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC"):
            domain = "Numeric"
            meaning = f"Decimal value: {col_name.replace('_', ' ')}"
        elif col_type.upper() in ("BOOLEAN", "BOOL"):
            domain = "Flag"
            meaning = f"Boolean flag: {col_name.replace('_', ' ')}"

        return {
            "column": col_name,
            "type": col_type,
            "meaning": meaning,
            "domain": domain,
            "usage": "General purpose",
            "semantic_role": role,
            "business_priority": business_priority,
            "is_pk": is_pk,
            "is_fk": is_fk,
            "inferred": True,
        }

    entry["column"] = col_name
    entry["type"] = col_type
    entry["semantic_role"] = role
    entry["business_priority"] = business_priority
    entry["is_pk"] = is_pk
    entry["is_fk"] = is_fk
    return entry


def _infer_table_business_name(table_name: str, columns: list) -> str:
    table_names = {
        "orders": "Sales Orders",
        "order_items": "Order Line Items",
        "products": "Product Catalog",
        "customers": "Customer Profiles",
        "stores": "Store Locations",
        "categories": "Product Categories",
        "employees": "Employee Records",
        "suppliers": "Supplier Directory",
        "inventory": "Inventory Levels",
        "transactions": "Financial Transactions",
        "reviews": "Customer Reviews",
        "returns": "Return Records",
    }
    return table_names.get(table_name.lower(), table_name.replace("_", " ").title())

def _identify_table_metrics(table_name: str, columns: list) -> list:
    col_names = [c["column"].lower() for c in columns]
    metrics = []
    if any(
        any(k in col for k in ["price", "amount", "revenue", "sales", "cost", "total"])
        for col in col_names
    ):
        metrics.append("revenue_sum")
    if any(
        any(k in col for k in ["quantity", "qty", "units", "count"])
        for col in col_names
    ):
        metrics.append("volume_count")
    if any(
        any(k in col for k in ["date", "time", "created_at", "updated_at"])
        for col in col_names
    ):
        metrics.append("time_series")
    if any(
        "customer" in col
        for col in col_names
    ):
        metrics.append("customer_analysis")
    if any(
        any(k in col for k in ["category", "segment", "region", "store", "brand"])
        for col in col_names
    ):
        metrics.append("group_by_dimension")
    if any(
        "status" in col
        for col in col_names
    ):
        metrics.append("status_funnel")
    return metrics



def _table_type(table_name: str, row_count: int, columns: list) -> str:
    cols = {c["column"].lower() for c in columns}
    if row_count >= 100 and any(x in cols for x in ["date", "created_at", "order_date", "updated_at"]) and any(x in cols for x in ["price", "amount", "revenue", "quantity", "total_price", "cost"]):
        return "fact_table"
    if any(x in table_name.lower() for x in ["order_items", "transactions", "sales", "facts"]):
        return "fact_table"
    if any(x in table_name.lower() for x in ["customers", "products", "categories", "stores", "dim"]):
        return "dimension_table"
    return "fact_table" if row_count > 1000 else "dimension_table"


def _table_summary(table_name: str, business_name: str, table_type: str, columns: list) -> str:
    roles = sorted({c.get("semantic_role", "attribute") for c in columns})
    key_cols = [c["column"] for c in columns if c.get("is_pk") or c.get("is_fk")]
    measures = [c["column"] for c in columns if c.get("semantic_role") in {"metric", "measure"}]
    return (
        f"Table {table_name} ({business_name}) is a {table_type} with semantic roles {', '.join(roles)}. "
        f"Key columns include {', '.join(key_cols[:5]) or 'none identified'}. "
        f"Business measures include {', '.join(measures[:5]) or 'none identified'}."
    )

def build_semantic_layer(schema: dict) -> dict:
    semantic = {
        "tables": {},
        "domain_glossary": DOMAIN_GLOSSARY,
        "metric_definitions": METRIC_DEFINITIONS,
        "table_business_names": {},
        "relationships_narrative": [],
        "embedding_documents": [],
    }

    for table_name, info in schema["tables"].items():
        fk_cols = {fk["from"] for fk in info.get("foreign_keys", [])}
        pk_cols = {c["name"] for c in info["columns"] if c["pk"]}
        enriched_columns = []
        domains_in_table = set()
        for col in info["columns"]:
            is_fk = col["name"] in fk_cols
            enriched = _infer_column_meaning(col["name"], col["type"], is_pk=col["pk"], is_fk=is_fk)
            enriched_columns.append(enriched)
            domains_in_table.add(enriched["domain"])

        business_name = _infer_table_business_name(table_name, enriched_columns)
        table_type = _table_type(table_name, info["row_count"], enriched_columns)
        table_summary = _table_summary(table_name, business_name, table_type, enriched_columns)
        semantic["table_business_names"][table_name] = business_name
        available_metrics = _identify_table_metrics(table_name, enriched_columns)
        semantic["tables"][table_name] = {
            "business_name": business_name,
            "table_type": table_type,
            "table_summary": table_summary,
            "columns": enriched_columns,
            "domains": sorted(domains_in_table),
            "row_count": info["row_count"],
            "available_metrics": available_metrics,
            "primary_keys": list(pk_cols),
            "foreign_keys": info.get("foreign_keys", []),
            "business_priority": max([c.get("business_priority", 1) for c in enriched_columns] or [1]),
        }

        for col in enriched_columns:
            semantic["embedding_documents"].append(
                {
                    "id": f"{table_name}.{col['column']}",
                    "type": "column",
                    "table": table_name,
                    "text": (
                        f"Table: {table_name}\n"
                        f"Business Name: {business_name}\n"
                        f"Table Type: {table_type}\n"
                        f"Table Summary: {table_summary}\n"
                        f"Column: {col['column']}\n"
                        f"Meaning: {col['meaning']}\n"
                        f"Domain: {col['domain']}\n"
                        f"Semantic Role: {col['semantic_role']}\n"
                        f"Usage: {col.get('usage', 'General purpose')}\n"
                        f"Business Priority: {col['business_priority']}\n"
                    ),
                    "metadata": {
                        "table": table_name,
                        "column": col["column"],
                        "role": col["semantic_role"],
                        "priority": col["business_priority"],
                        "table_type": table_type,
                    },
                }
            )

        semantic["embedding_documents"].append(
            {
                "id": f"table::{table_name}",
                "type": "table",
                "table": table_name,
                "text": (
                    f"Table: {table_name}\n"
                    f"Business Name: {business_name}\n"
                    f"Table Type: {table_type}\n"
                    f"Table Summary: {table_summary}\n"
                    f"Row Count: {info['row_count']}\n"
                    f"Domains: {', '.join(sorted(domains_in_table))}\n"
                    f"Available Metrics: {', '.join(available_metrics) or 'general'}\n"
                ),
                "metadata": {
                    "table": table_name,
                    "table_type": table_type,
                    "priority": max([c.get("business_priority", 1) for c in enriched_columns] or [1]),
                },
            }
        )

    for rel in schema.get("relationships", []):
        ft = rel["from_table"]
        tt = rel["to_table"]
        fb = semantic["table_business_names"].get(ft, ft)
        tb = semantic["table_business_names"].get(tt, tt)
        semantic["relationships_narrative"].append(f"Each {fb} record links to a {tb} via {rel['from_col']} -> {rel['to_col']}")

    return semantic

def semantic_to_prompt_text(semantic: dict, relevant_tables: Optional[list] = None) -> str:
    lines = ["SEMANTIC DATABASE KNOWLEDGE:\n"]
    tables_to_include = relevant_tables or list(semantic["tables"].keys())
    for table_name in tables_to_include:
        if table_name not in semantic["tables"]:
            continue
        info = semantic["tables"][table_name]
        lines.append(f'Table: {table_name} -> "{info["business_name"]}" ({info["row_count"]:,} rows)')
        lines.append(f"  Table type: {info['table_type']}")
        lines.append(f"  Summary: {info['table_summary']}")
        lines.append(f"  Available metrics: {', '.join(info['available_metrics']) or 'general'}")
        for col in info["columns"]:
            pk_tag = " [PK]" if col["is_pk"] else ""
            fk_tag = " [FK]" if col["is_fk"] else ""
            lines.append(f"  - {col['column']} ({col['type']}){pk_tag}{fk_tag} [{col['semantic_role']}, priority={col['business_priority']}] -> {col['meaning']}")
        if info["foreign_keys"]:
            for fk in info["foreign_keys"]:
                lines.append(f"  JOIN: {fk['from']} -> {fk['to_table']}.{fk['to_col']}")
        lines.append("")
    if semantic.get("relationships_narrative"):
        lines.append("BUSINESS RELATIONSHIPS:")
        for r in semantic["relationships_narrative"]:
            lines.append(f"  • {r}")
        lines.append("")
    lines.append("KEY METRIC DEFINITIONS:")
    for metric, definition in semantic["metric_definitions"].items():
        lines.append(f"  • {metric}: {definition}")
    return "\n".join(lines)