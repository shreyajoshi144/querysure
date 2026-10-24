"""Per-format deterministic benchmark (no LLM).

For every case we have: several question phrasings (some deliberately unseen / worded
differently), a REFERENCE SQL, a DEFECTIVE SQL that makes the classic mistake, and an
expected result computed by plain python from the source rows (never from SQL).
Measured per format:
  exec   - reference SQL passes validate_sql, executes read-only, and equals the independent expectation
  accept - intent checker raises no issue for the reference SQL under every phrasing (no false alarms)
  flag   - intent checker flags the defective SQL under every phrasing (catches the mistake)
A case counts as correct only if all three hold. This measures the deterministic layers around
the LLM; it is NOT an end-to-end NL->SQL accuracy figure."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any, Callable, Dict, List

from modules.query_result_engine import execute_query
from modules.schema_engine import load_database
from modules.sql_intent_checker import check_intent
from modules.sql_validator import validate_sql


def _close(a, b, tol=1e-6):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tol * max(1.0, abs(b))
    return a == b


def rows_equal(got: List[tuple], exp: List[tuple]) -> bool:
    return len(got) == len(exp) and all(len(g) == len(e) and all(_close(x, y, 1e-6) for x, y in zip(g, e)) for g, e in zip(got, exp))


def tabular_cases(rows: List[dict], T: str, margin_col: str, margin_is_fraction: bool) -> List[Dict[str, Any]]:
    """Cases for the sales-shaped CSV / Excel / JSON stand-ins. `rows` is the source truth."""
    M = margin_col
    in_jan = [r for r in rows if "2023-01-01" <= r["order_date"].isoformat() <= "2023-01-31"]
    by_region = defaultdict(float)
    for r in rows:
        by_region[r["region"]] += r["revenue"]
    top_region = sorted(by_region.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    top5 = sorted(rows, key=lambda r: (-r["revenue"], r["order_id"]))[:5]
    cat_rev = defaultdict(list)
    for r in rows:
        cat_rev[r["category"]].append(r["revenue"])
    cat_units = defaultdict(int)
    for r in rows:
        cat_units[r["category"]] += r["units"]
    least_cat = sorted(cat_units.items(), key=lambda kv: (kv[1], kv[0]))[0]
    east = sum(1 for r in rows if r["region"] == "East")
    thr = 0.30
    hi_margin = sum(1 for r in rows if r["margin"] > thr)
    q1_month = defaultdict(float)
    for r in rows:
        if "2023-01-01" <= r["order_date"].isoformat() <= "2023-03-31":
            q1_month[r["order_date"].isoformat()[:7]] += r["revenue"]
    reg_margin = defaultdict(list)
    for r in rows:
        reg_margin[r["region"]].append(r["margin"])
    hi_growth = [r["growth_pct"] for r in rows if r["growth_pct"] > 20]
    thr_lit = "0.30" if margin_is_fraction else "30"
    bad_thr = "30" if margin_is_fraction else "0.30"
    pct_expr = f"AVG({M}) * 100" if margin_is_fraction else f"AVG({M})"
    return [
        dict(id="range_inclusive_sum",
             q=["What was total revenue between 2023-01-01 and 2023-01-31?",
                "Sum the revenue for orders dated from 2023-01-01 through 2023-01-31",
                "total sales amount in the period 2023-01-01 to 2023-01-31 please"],
             sql=f"SELECT SUM(revenue) FROM {T} WHERE order_date BETWEEN '2023-01-01' AND '2023-01-31'",
             bad=f"SELECT SUM(revenue) FROM {T}",
             exp=[(sum(r["revenue"] for r in in_jan),)]),
        dict(id="rank_top_region",
             q=["Which region had the highest total revenue?",
                "Rank regions by revenue and tell me the best one",
                "what is the biggest region in terms of total revenue"],
             sql=f"SELECT region, SUM(revenue) AS total_revenue FROM {T} GROUP BY region ORDER BY total_revenue DESC, region LIMIT 1",
             bad=f"SELECT region, SUM(revenue) AS total_revenue FROM {T} GROUP BY region",
             exp=[top_region]),
        dict(id="top5_orders",
             q=["Show the top 5 orders by revenue",
                "top 5 biggest orders (by revenue)",
                "List the 5 largest orders ranked by revenue, top 5 only"],
             sql=f"SELECT order_id, revenue FROM {T} ORDER BY revenue DESC, order_id LIMIT 5",
             bad=f"SELECT order_id, revenue FROM {T} ORDER BY revenue DESC",
             exp=[(r["order_id"], r["revenue"]) for r in top5]),
        dict(id="avg_by_category",
             q=["What is the average revenue by category?",
                "average order revenue for each category",
                "mean revenue per category"],
             sql=f"SELECT category, AVG(revenue) FROM {T} GROUP BY category ORDER BY category",
             bad=f"SELECT category, SUM(revenue) FROM {T} GROUP BY category ORDER BY category",
             exp=[(c, sum(v) / len(v)) for c, v in sorted(cat_rev.items())]),
        dict(id="count_filter_east",
             q=["How many orders came from the East region?",
                "number of orders in East",
                "count the East orders"],
             sql=f"SELECT COUNT(*) FROM {T} WHERE region = 'East'",
             bad=f"SELECT COUNT(*) FROM {T}",
             exp=[(east,)]),
        dict(id="pct_threshold",
             q=["How many orders have a margin above 30%?",
                "count orders whose margin exceeds 30 percent",
                "number of orders with margin greater than 30%"],
             sql=f"SELECT COUNT(*) FROM {T} WHERE {M} > {thr_lit}",
             bad=f"SELECT COUNT(*) FROM {T} WHERE {M} > {bad_thr}",
             exp=[(hi_margin,)]),
        dict(id="avg_margin_pct_by_region",
             q=["What is the average margin percentage by region?",
                "average margin % for each region",
                "mean margin percent per region"],
             sql=f"SELECT region, {pct_expr} FROM {T} GROUP BY region ORDER BY region",
             bad=f"SELECT region, SUM({M}) FROM {T} GROUP BY region ORDER BY region",
             exp=[(g, sum(v) / len(v) * 100) for g, v in sorted(reg_margin.items())]),
        dict(id="q1_monthly",
             q=["Show total revenue by month for Q1 2023",
                "monthly revenue totals for January to March 2023",
                "revenue per month in the first quarter of 2023"],
             sql=f"SELECT substr(order_date,1,7) AS month, SUM(revenue) FROM {T} WHERE order_date >= '2023-01-01' AND order_date <= '2023-03-31' GROUP BY month ORDER BY month",
             bad=f"SELECT substr(order_date,1,7) AS month, SUM(revenue) FROM {T} GROUP BY month ORDER BY month",
             exp=sorted(q1_month.items())),
        dict(id="lowest_category_units",
             q=["Which category sold the fewest units?",
                "category with the lowest total units",
                "which category has the least units sold"],
             sql=f"SELECT category, SUM(units) AS u FROM {T} GROUP BY category ORDER BY u ASC, category LIMIT 1",
             bad=f"SELECT category, SUM(units) AS u FROM {T} GROUP BY category ORDER BY u DESC LIMIT 1",
             exp=[least_cat]),
        dict(id="avg_high_growth",
             q=["What is the average growth for orders with growth above 20%?",
                "mean growth where growth exceeds 20%",
                "average growth_pct of orders growing more than 20%"],
             sql=f"SELECT AVG(growth_pct) FROM {T} WHERE growth_pct > 20",
             bad=f"SELECT AVG(growth_pct) FROM {T} WHERE growth_pct > 0.2",
             exp=[(sum(hi_growth) / len(hi_growth),)]),
    ]


def northwind_cases(db_path: str) -> List[Dict[str, Any]]:
    """Cases over the declared-FK-free Northwind-small stand-in; expectations via python dict joins."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    orders = {r["Id"]: dict(r) for r in con.execute('SELECT * FROM "Order"')}
    prods = {r["Id"]: dict(r) for r in con.execute("SELECT * FROM Product")}
    cats = {r["Id"]: dict(r) for r in con.execute("SELECT * FROM Category")}
    custs = {r["Id"]: dict(r) for r in con.execute("SELECT * FROM Customer")}
    emps = {r["Id"]: dict(r) for r in con.execute("SELECT * FROM Employee")}
    dets = [dict(r) for r in con.execute("SELECT * FROM OrderDetail")]
    con.close()

    rev_by_prod = defaultdict(float)
    for d in dets:
        rev_by_prod[prods[d["ProductId"]]["ProductName"]] += d["UnitPrice"] * d["Quantity"] * (1 - d["Discount"])
    top5 = sorted(rev_by_prod.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    orders_by_country = defaultdict(int)
    for o in orders.values():
        orders_by_country[custs[o["CustomerId"]]["Country"]] += 1
    freight = defaultdict(list)
    for o in orders.values():
        e = emps[o["EmployeeId"]]
        freight[e["LastName"]].append(o["Freight"])
    units_by_cat = defaultdict(int)
    for d in dets:
        units_by_cat[cats[prods[d["ProductId"]]["CategoryId"]]["CategoryName"]] += d["Quantity"]
    mar = sum(d["UnitPrice"] * d["Quantity"] for d in dets if "2023-03-01" <= orders[d["OrderId"]]["OrderDate"] <= "2023-03-31")
    germany_rev = defaultdict(float)
    for d in dets:
        o = orders[d["OrderId"]]
        if custs[o["CustomerId"]]["Country"] == "Germany":
            germany_rev[prods[d["ProductId"]]["ProductName"]] += d["UnitPrice"] * d["Quantity"]
    top3_g = sorted(germany_rev.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    return [
        dict(id="nw_top5_products_revenue",
             q=["Top 5 products by revenue after discount",
                "Which five products generated the most net revenue?",
                "rank products by total net sales, show top 5"],
             sql='SELECT p.ProductName, SUM(od.UnitPrice * od.Quantity * (1 - od.Discount)) AS rev FROM OrderDetail od '
                 'JOIN Product p ON od.ProductId = p.Id GROUP BY p.ProductName ORDER BY rev DESC, p.ProductName LIMIT 5',
             bad='SELECT p.ProductName, SUM(od.UnitPrice * od.Quantity * (1 - od.Discount)) AS rev FROM OrderDetail od '
                 'JOIN Product p ON od.ProductId = p.Id GROUP BY p.ProductName',
             exp=top5),
        dict(id="nw_orders_by_country",
             q=["How many orders per customer country?",
                "number of orders for each country of the customer",
                "count orders by customer country"],
             sql='SELECT c.Country, COUNT(*) FROM "Order" o JOIN Customer c ON o.CustomerId = c.Id GROUP BY c.Country ORDER BY c.Country',
             bad='SELECT c.Country, SUM(o.Id) FROM "Order" o JOIN Customer c ON o.CustomerId = c.Id GROUP BY c.Country ORDER BY c.Country',
             exp=sorted(orders_by_country.items())),
        dict(id="nw_avg_freight_employee",
             q=["Average freight per employee",
                "what is the mean freight cost for each employee",
                "average shipping freight by employee last name"],
             sql='SELECT e.LastName, AVG(o.Freight) FROM "Order" o JOIN Employee e ON o.EmployeeId = e.Id GROUP BY e.LastName ORDER BY e.LastName',
             bad='SELECT e.LastName, SUM(o.Freight) FROM "Order" o JOIN Employee e ON o.EmployeeId = e.Id GROUP BY e.LastName ORDER BY e.LastName',
             exp=[(k, sum(v) / len(v)) for k, v in sorted(freight.items())]),
        dict(id="nw_units_by_category_3join",
             q=["Total units sold by category",
                "sum of quantity for each product category",
                "how many units per category"],
             sql='SELECT c.CategoryName, SUM(od.Quantity) FROM OrderDetail od JOIN Product p ON od.ProductId = p.Id '
                 'JOIN Category c ON p.CategoryId = c.Id GROUP BY c.CategoryName ORDER BY c.CategoryName',
             bad='SELECT c.CategoryName, MAX(od.Quantity) FROM OrderDetail od JOIN Product p ON od.ProductId = p.Id '
                 'JOIN Category c ON p.CategoryId = c.Id ORDER BY c.CategoryName',
             exp=sorted(units_by_cat.items())),
        dict(id="nw_march_revenue",
             q=["Total revenue for orders between 2023-03-01 and 2023-03-31",
                "sum of sales in the period 2023-03-01 to 2023-03-31",
                "total gross revenue from 2023-03-01 through 2023-03-31"],
             sql="SELECT SUM(od.UnitPrice * od.Quantity) FROM OrderDetail od JOIN \"Order\" o ON od.OrderId = o.Id "
                 "WHERE o.OrderDate BETWEEN '2023-03-01' AND '2023-03-31'",
             bad='SELECT SUM(od.UnitPrice * od.Quantity) FROM OrderDetail od JOIN "Order" o ON od.OrderId = o.Id',
             exp=[(mar,)]),
        dict(id="nw_germany_top3",
             q=["Top 3 products by revenue for Germany customers",
                "which 3 products sell the most to Germany? rank them by total revenue",
                "top 3 best-selling products (by revenue) among Germany customers"],
             sql='SELECT p.ProductName, SUM(od.UnitPrice * od.Quantity) AS rev FROM OrderDetail od '
                 'JOIN Product p ON od.ProductId = p.Id JOIN "Order" o ON od.OrderId = o.Id '
                 "JOIN Customer c ON o.CustomerId = c.Id WHERE c.Country = 'Germany' "
                 'GROUP BY p.ProductName ORDER BY rev DESC, p.ProductName LIMIT 3',
             bad='SELECT p.ProductName, SUM(od.UnitPrice * od.Quantity) AS rev FROM OrderDetail od '
                 'JOIN Product p ON od.ProductId = p.Id JOIN "Order" o ON od.OrderId = o.Id '
                 'JOIN Customer c ON o.CustomerId = c.Id GROUP BY p.ProductName ORDER BY rev DESC, p.ProductName LIMIT 3',
             exp=top3_g),
    ]


def run_benchmark(db_path: str, cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    schema = load_database(db_path)
    results = []
    for c in cases:
        v = validate_sql(c["sql"], schema)
        res = execute_query(c["sql"], db_path) if v.get("is_valid") else {"status": "skipped", "rows": [], "columns": []}
        got = [tuple(r[col] for col in res.get("columns", [])) for r in res.get("rows", [])]
        exec_ok = bool(v.get("is_valid")) and res.get("status") in ("success", "ok", "completed") or (
            bool(v.get("is_valid")) and bool(got))
        exec_ok = exec_ok and rows_equal(got, c["exp"])
        accept_ok = all(not check_intent(q, c["sql"], schema) for q in c["q"])
        flag_ok = all(bool(check_intent(q, c["bad"], schema)) for q in c["q"])
        results.append(dict(id=c["id"], exec=exec_ok, accept=accept_ok, flag=flag_ok,
                            correct=exec_ok and accept_ok and flag_ok,
                            detail=dict(valid=v.get("is_valid"), issues=[i["message"] for i in v.get("issues", [])],
                                        got=got[:6], exp=c["exp"][:6])))
    n = len(results)
    return dict(total=n, correct=sum(r["correct"] for r in results),
                accuracy=round(100.0 * sum(r["correct"] for r in results) / max(n, 1), 2),
                exec_pct=round(100.0 * sum(r["exec"] for r in results) / max(n, 1), 2),
                accept_pct=round(100.0 * sum(r["accept"] for r in results) / max(n, 1), 2),
                flag_pct=round(100.0 * sum(r["flag"] for r in results) / max(n, 1), 2),
                cases=results)


# Phrasings written AFTER the checker was tuned and never used to tune it (held-out).
HOLDOUT = {
    "range_inclusive_sum": ["Give me the revenue sum for 2023-01-01 up to and including 2023-01-31",
                            "How much revenue did we book from 2023-01-01 to 2023-01-31?"],
    "rank_top_region": ["Name the region with the largest revenue total", "Which region is number one by total revenue?"],
    "top5_orders": ["Which are our 5 highest revenue orders? Top 5", "show top 5 orders ranked by revenue"],
    "avg_by_category": ["Per category, what's the average revenue?", "Compute avg revenue by category"],
    "count_filter_east": ["How many East orders are there?", "what's the count of orders where region is East"],
    "pct_threshold": ["Orders with margin over 30% - how many?", "How many orders exceed a 30% margin?"],
    "avg_margin_pct_by_region": ["Show average margin percent per region", "For each region give the mean margin %"],
    "q1_monthly": ["Break revenue down by month for January through March 2023", "Q1 2023 revenue per month"],
    "lowest_category_units": ["Which category moved the smallest number of units in total?", "least units sold: which category"],
    "avg_high_growth": ["Average growth among orders growing above 20%", "mean growth for orders with more than 20% growth"],
    "nw_top5_products_revenue": ["Top 5 products ranked by discounted revenue", "5 best products by total net revenue"],
    "nw_orders_by_country": ["Break down order counts by customer country", "How many orders did each country place?"],
    "nw_avg_freight_employee": ["Mean freight per employee", "For each employee, average the freight on their orders"],
    "nw_units_by_category_3join": ["Sum the units sold for each category", "Total quantity by category"],
    "nw_march_revenue": ["Revenue total from 2023-03-01 to 2023-03-31 inclusive", "What did orders dated 2023-03-01 through 2023-03-31 bring in in total revenue?"],
    "nw_germany_top3": ["Germany customers: top 3 products ranked by revenue", "What are the 3 best products by revenue for Germany?"],
}


def with_holdout(cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for c in cases:
        out.append({**c, "id": c["id"] + "#heldout", "q": HOLDOUT[c["id"]]})
    return out
