"""v1.0.5 benchmark for CSV (hard fixture) and SQLite (Northwind-style, no declared FKs).

Measured WITHOUT an LLM. Per case:
  exec     reference SQL validates, executes, and equals the independently computed (pandas / plain python) result
  accept   semantic validation raises no issue for the reference SQL under EVERY phrasing (no false alarms)
  detect   a defective variant is caught (semantic issue, validator rejection or failed independent verification)
           under EVERY phrasing
  correct  exec and accept and detect
CSV only, extra metrics on the deterministic planner path (question -> plan -> reference SQL -> result):
  covered  phrasings for which the planner can build an unambiguous, independently checkable plan
  planner_exact  of the covered ones, the planner's own SQL result equals the pandas expectation exactly
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import build_hard_csv as hc  # noqa: E402
from modules.query_planner import build_plan, reference_sql  # noqa: E402
from modules.query_result_engine import execute_query  # noqa: E402
from modules.result_verifier import semantic_validate, verify_result  # noqa: E402
from modules.schema_engine import load_database  # noqa: E402
from modules.sql_validator import validate_sql  # noqa: E402
from modules.value_grounding import ground_question  # noqa: E402
from format_benchmark import rows_equal  # noqa: E402


def csv_cases(csv_path: str) -> List[Dict[str, Any]]:
    df = pd.read_csv(csv_path)
    df = df.rename(columns={"Order": "order", "Group": "group", "Plan Name": "plan_name", "Customer": "customer", "Amount": "amount",
                            "Units": "units", "Status": "status", "Start Date": "start_date"})
    T = "subs"
    d = df
    m_mar = d[(d.start_date >= "2023-03-01") & (d.start_date <= "2023-03-31")]
    q2 = d[(d.start_date >= "2023-04-01") & (d.start_date <= "2023-06-30")]
    g_units = d.groupby("group")["units"].sum()
    return [
        dict(id="count_active", q=["How many Active subscriptions are there?", "number of subscriptions with status Active", "count the Active ones"],
             held=["Count subscriptions whose status is Active", "How many rows have Active status?"],
             sql=f"SELECT COUNT(*) FROM {T} WHERE status = 'Active'", bad=f"SELECT COUNT(*) FROM {T} WHERE status = 'active'",
             exp=[(int((d.status == "Active").sum()),)]),
        dict(id="sum_amount_pending_nulls", q=["What is the total amount for Pending subscriptions?", "sum of amount where status is Pending"],
             held=["Total amount of Pending ones", "Add up the amount for Pending subscriptions"],
             sql=f"SELECT SUM(amount) FROM {T} WHERE status = 'Pending'", bad=f"SELECT SUM(amount) FROM {T} WHERE status = 'Cancelled'",
             exp=[(float(d.loc[d.status == "Pending", "amount"].sum()),)]),
        dict(id="avg_amount_by_status", q=["What is the average amount by status?", "average amount for each status"],
             held=["Mean amount per status", "Per status, what's the average amount?"],
             sql=f"SELECT status, AVG(amount) FROM {T} GROUP BY status ORDER BY status", bad=f"SELECT status, SUM(amount) FROM {T} GROUP BY status ORDER BY status",
             exp=[(k, float(v)) for k, v in d.groupby("status")["amount"].mean().sort_index().items()]),
        dict(id="units_by_group_reserved", q=["Total units by group", "sum of units for each group"],
             held=["Units per group, totalled", "What are the total units in each group?"],
             sql=f'SELECT "group", SUM(units) FROM {T} GROUP BY "group" ORDER BY "group"', bad=f'SELECT "group", AVG(units) FROM {T} GROUP BY "group" ORDER BY "group"',
             exp=[(k, int(v)) for k, v in g_units.sort_index().items()]),
        dict(id="top_group_units", q=["Which group has the highest total units?", "Which group has the most units in total?"],
             held=["Name the group with the largest total units", "which group is number one by total units"],
             sql=f'SELECT "group", SUM(units) AS u FROM {T} GROUP BY "group" ORDER BY u DESC, "group" LIMIT 1',
             bad=f'SELECT "group", SUM(units) AS u FROM {T} GROUP BY "group" ORDER BY u ASC LIMIT 1',
             exp=[(g_units.sort_values(ascending=False, kind="stable").index[0], int(g_units.max()))]),
        dict(id="lowest2_groups", q=["Which 2 groups have the lowest total units?", "bottom 2 groups by total units"],
             held=["Show the two groups with the fewest total units", "the 2 smallest groups by total units"],
             sql=f'SELECT "group", SUM(units) AS u FROM {T} GROUP BY "group" ORDER BY u ASC, "group" LIMIT 2',
             bad=f'SELECT "group", SUM(units) AS u FROM {T} GROUP BY "group" ORDER BY u DESC LIMIT 2',
             exp=[(k, int(v)) for k, v in g_units.sort_values(kind="stable").head(2).items()]),
        dict(id="long_value_a", q=[f"What is the total units for {hc.LONG_A}?", f"sum of units where plan_name is {hc.LONG_A}"],
             held=[f"Total units of {hc.LONG_A}", f"How many units in total for {hc.LONG_A}?"],
             sql=f"SELECT SUM(units) FROM {T} WHERE plan_name = '{hc.LONG_A}'", bad=f"SELECT SUM(units) FROM {T} WHERE plan_name = '{hc.LONG_A[:40]}'",
             exp=[(int(d.loc[d.plan_name == hc.LONG_A, "units"].sum()),)]),
        dict(id="long_value_b_similar", q=[f"What is the total units for {hc.LONG_B}?", f"sum of units for {hc.LONG_B}"],
             held=[f"Total units of {hc.LONG_B}", f"Units total, {hc.LONG_B}"],
             sql=f"SELECT SUM(units) FROM {T} WHERE plan_name = '{hc.LONG_B}'", bad=f"SELECT SUM(units) FROM {T} WHERE plan_name = '{hc.LONG_A}'",
             exp=[(int(d.loc[d.plan_name == hc.LONG_B, "units"].sum()),)]),
        dict(id="apostrophe_value", q=["What is the total units for O'Brien & Sons Ltd?", "sum of units for customer O'Brien & Sons Ltd"],
             held=["Total units for O'Brien & Sons Ltd", "Units, summed, for O'Brien & Sons Ltd"],
             sql=f"SELECT SUM(units) FROM {T} WHERE customer = 'O''Brien & Sons Ltd'", bad=f"SELECT SUM(units) FROM {T}",
             exp=[(int(d.loc[d.customer == "O'Brien & Sons Ltd", "units"].sum()),)]),
        dict(id="whitespace_value", q=["How many rows for Smith  and  Co?", "count of Smith  and  Co orders"],
             held=["Number of rows for Smith  and  Co", "Count the Smith  and  Co records"],
             sql=f"SELECT COUNT(*) FROM {T} WHERE customer = 'Smith  and  Co'", bad=f"SELECT COUNT(*) FROM {T} WHERE customer = 'smith and co'",
             exp=[(int((d.customer == "Smith  and  Co").sum()),)]),
        dict(id="date_range_inclusive", q=["What is the total units between 2023-03-01 and 2023-03-31?", "sum of units from 2023-03-01 to 2023-03-31"],
             held=["Total units for dates 2023-03-01 through 2023-03-31", "Add the units dated 2023-03-01 to 2023-03-31"],
             sql=f"SELECT SUM(units) FROM {T} WHERE start_date BETWEEN '2023-03-01' AND '2023-03-31'",
             bad=f"SELECT SUM(units) FROM {T} WHERE start_date >= '2023-03-01' AND start_date < '2023-03-31'",
             exp=[(int(m_mar.units.sum()),)]),
        dict(id="count_in_month", q=["How many subscriptions started in March 2023?", "number of rows in March 2023"],
             held=["Count rows with a start date in March 2023", "How many started during March 2023?"],
             sql=f"SELECT COUNT(*) FROM {T} WHERE start_date >= '2023-03-01' AND start_date <= '2023-03-31'", bad=f"SELECT COUNT(*) FROM {T}",
             exp=[(len(m_mar),)]),
        dict(id="filter_plus_quarter", q=["What is the total amount for Active subscriptions in Q2 2023?", "sum of amount where status is Active in Q2 2023"],
             held=["Total amount of Active ones in Q2 2023", "Active subscriptions' total amount, Q2 2023"],
             sql=f"SELECT SUM(amount) FROM {T} WHERE status = 'Active' AND start_date >= '2023-04-01' AND start_date <= '2023-06-30'",
             bad=f"SELECT SUM(amount) FROM {T} WHERE start_date >= '2023-04-01' AND start_date <= '2023-06-30'",
             exp=[(float(q2.loc[q2.status == "Active", "amount"].sum()),)]),
        dict(id="max_units", q=["What is the maximum units?", "maximum units across all rows"],
             held=["Largest units value overall (maximum)", "Give the maximum of units"],
             sql=f"SELECT MAX(units) FROM {T}", bad=f"SELECT MIN(units) FROM {T}", exp=[(int(d.units.max()),)]),
        dict(id="avg_units_threshold", q=["What is the average units for rows with units above 10?", "mean units where units exceeds 10"],
             held=["Average of units where units is greater than 10", "avg units for those over 10 units"],
             sql=f"SELECT AVG(units) FROM {T} WHERE units > 10", bad=f"SELECT AVG(units) FROM {T}",
             exp=[(float(d.loc[d.units > 10, "units"].mean()),)]),
        dict(id="two_filters", q=["How many Active subscriptions are in group g1?", "count rows where status is Active and group is g1"],
             held=["Number of Active rows in group g1", "Count Active subscriptions in g1"],
             sql=f"SELECT COUNT(*) FROM {T} WHERE status = 'Active' AND \"group\" = 'g1'", bad=f"SELECT COUNT(*) FROM {T} WHERE status = 'Active'",
             exp=[(int(((d.status == "Active") & (d.group == "g1")).sum()),)]),
        dict(id="count_by_status", q=["How many subscriptions per status?", "count of rows for each status"],
             held=["Number of rows by status", "Count subscriptions by status"],
             sql=f"SELECT status, COUNT(*) FROM {T} GROUP BY status ORDER BY status", bad=f"SELECT status, SUM(units) FROM {T} GROUP BY status ORDER BY status",
             exp=[(k, int(v)) for k, v in d.groupby("status").size().sort_index().items()]),
        dict(id="empty_nonexistent_value", q=["What is the total amount for Gold subscriptions?", "sum of amount where status is Gold"],
             held=["Total amount of Gold ones", "Add up the amount for Gold subscriptions"],
             sql=f"SELECT SUM(amount) FROM {T} WHERE status = 'Gold'", bad=None, expect_status="suspicious_empty",
             exp=[(None,)]),
    ]


def _caught(q: str, bad_sql: str, schema: dict, db: str) -> bool:
    v = validate_sql(bad_sql, schema)
    if not v.get("is_valid"):
        return True
    sem = semantic_validate(q, bad_sql, schema, db)
    if sem["issues"]:
        return True
    res = execute_query(bad_sql, db, parameterize=True)
    ver = verify_result(q, bad_sql, schema, res, db, plan=sem["plan"])
    return ver["status"] in ("mismatch", "suspicious_empty")


def run_csv(csv_path: str, db: str, heldout: bool) -> Dict[str, Any]:
    schema = load_database(db)
    out, planner_cov, planner_ok, total_phr = [], 0, 0, 0
    verified_cov = 0
    for c in csv_cases(csv_path):
        qs = c["held"] if heldout else c["q"]
        v = validate_sql(c["sql"], schema)
        res = execute_query(c["sql"], db, parameterize=True) if v["is_valid"] else {"rows": [], "columns": []}
        got = [tuple(r[col] for col in res.get("columns", [])) for r in res.get("rows", [])]
        exec_ok = v["is_valid"] and (rows_equal(got, c["exp"]) or (c["exp"] == [(None,)] and got == [(None,)]))
        accept_ok, detect_ok = True, True
        for q in qs:
            sem = semantic_validate(q, c["sql"], schema, db)
            if c.get("expect_status"):
                ver = verify_result(q, c["sql"], schema, res, db, plan=sem["plan"])
                detect_ok &= ver["status"] == c["expect_status"]      # the system must NOT present this as a good answer
            else:
                accept_ok &= not sem["issues"]
                detect_ok &= _caught(q, c["bad"], schema, db)
                ver = verify_result(q, c["sql"], schema, res, db, plan=sem["plan"])
                verified_cov += ver["status"] == "verified_match"
            # planner path: question -> plan -> its own reference SQL -> compare to pandas
            total_phr += 1
            plan = build_plan(q, schema, ground_question(q, schema, db))
            ref = reference_sql(plan)
            if ref and not c.get("expect_status"):
                planner_cov += 1
                rr = execute_query(ref[0], db, params=ref[1])
                rgot = [tuple(r[col] for col in rr["columns"]) for r in rr["rows"]]
                planner_ok += bool(rr.get("success")) and rows_equal(rgot, c["exp"])
        out.append(dict(id=c["id"], exec=bool(exec_ok), accept=bool(accept_ok), detect=bool(detect_ok),
                        correct=bool(exec_ok and accept_ok and detect_ok), got=got[:3], exp=c["exp"][:3]))
    n = len(out)
    pct = lambda k: round(100.0 * sum(r[k] for r in out) / n, 2)  # noqa: E731
    return dict(total=n, correct=sum(r["correct"] for r in out), accuracy=pct("correct"), exec_pct=pct("exec"), accept_pct=pct("accept"),
                detect_pct=pct("detect"), phrasings=total_phr, planner_covered=planner_cov, planner_exact=planner_ok,
                independently_verified=verified_cov, cases=out)


# ------------------------------------------------------------------------------------------------ SQLite
def sqlite_cases(db_path: str) -> List[Dict[str, Any]]:
    """CTE / subquery / alias cases over the declared-FK-free Northwind-small stand-in; expectations by plain python."""
    import sqlite3
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    orders = {r["Id"]: dict(r) for r in con.execute('SELECT * FROM "Order"')}
    prods = {r["Id"]: dict(r) for r in con.execute("SELECT * FROM Product")}
    custs = {r["Id"]: dict(r) for r in con.execute("SELECT * FROM Customer")}
    dets = [dict(r) for r in con.execute("SELECT * FROM OrderDetail")]
    con.close()
    qty = defaultdict(int)
    for d in dets:
        qty[prods[d["ProductId"]]["ProductName"]] += d["Quantity"]
    top3 = sorted(qty.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    cnt = defaultdict(int)
    for o in orders.values():
        cnt[custs[o["CustomerId"]]["Country"]] += 1
    avg_f = sum(o["Freight"] for o in orders.values()) / len(orders)
    n_above = sum(1 for o in orders.values() if o["Freight"] > avg_f)
    n_orders_cust = defaultdict(int)
    for o in orders.values():
        n_orders_cust[o["CustomerId"]] += 1
    busy = sorted(c for c, n in n_orders_cust.items() if n >= 3)
    return [
        dict(id="cte_top3_products_qty", sql='WITH q AS (SELECT ProductId, SUM(Quantity) AS u FROM OrderDetail GROUP BY ProductId) '
             'SELECT p.ProductName, q.u FROM q JOIN Product p ON p.Id = q.ProductId ORDER BY q.u DESC, p.ProductName LIMIT 3',
             bad='WITH q AS (SELECT ProductId, SUM(Quantity) AS u FROM OrderDetail GROUP BY ProductId) '
                 'SELECT p.ProductName, q.u FROM q JOIN Product p ON p.Id = q.u ORDER BY q.u DESC LIMIT 3',
             exp=[(k, v) for k, v in top3]),
        dict(id="derived_orders_by_country", sql='SELECT c.Country, t.n FROM (SELECT CustomerId, COUNT(*) AS n FROM "Order" GROUP BY CustomerId) t '
             'JOIN Customer c ON c.Id = t.CustomerId ORDER BY c.Country, t.n',
             bad='SELECT c.Country, t.n FROM (SELECT CustomerId, COUNT(*) AS n FROM "Order" GROUP BY CustomerId) t '
                 'JOIN Customer c ON c.Id = t.n ORDER BY c.Country',
             exp=sorted((custs[cid]["Country"], n) for cid, n in n_orders_cust.items())),
        dict(id="subquery_above_avg_freight", sql='SELECT COUNT(*) FROM "Order" WHERE Freight > (SELECT AVG(Freight) FROM "Order")',
             bad='SELECT COUNT(*) FROM "Order" o JOIN Customer c ON c.Id = o.EmployeeId WHERE o.Freight > (SELECT AVG(Freight) FROM "Order")',
             exp=[(n_above,)]),
        dict(id="alias_join_count_by_country", sql='SELECT cu.Country, COUNT(*) AS n FROM "Order" o JOIN Customer cu ON cu.Id = o.CustomerId '
             'GROUP BY cu.Country ORDER BY cu.Country', bad='SELECT cu.Country, COUNT(*) AS n FROM "Order" o, Customer cu GROUP BY cu.Country ORDER BY cu.Country',
             exp=sorted(cnt.items())),
        dict(id="in_subquery_busy_customers", sql='SELECT Id FROM Customer WHERE Id IN (SELECT CustomerId FROM "Order" GROUP BY CustomerId HAVING COUNT(*) >= 3) ORDER BY Id',
             bad='SELECT c.Id FROM Customer c JOIN Product p ON p.Id = c.Id ORDER BY c.Id',
             exp=[(c,) for c in busy]),
        dict(id="cte_chain_total_qty", sql='WITH a AS (SELECT ProductId, Quantity FROM OrderDetail), b AS (SELECT ProductId, SUM(Quantity) AS s FROM a GROUP BY ProductId) SELECT SUM(s) FROM b',
             bad='WITH a AS (SELECT ProductId, Quantity FROM OrderDetail), b AS (SELECT ProductId, SUM(Quantity) AS s FROM a GROUP BY ProductId) SELECT SUM(s) FROM b JOIN Customer c ON c.Id = b.s',
             exp=[(sum(d["Quantity"] for d in dets),)]),
        dict(id="reserved_table_quoted", sql='SELECT COUNT(*) FROM "Order"', bad='SELECT COUNT(*) FROM "Order" o JOIN Product p ON p.Id = o.Id',
             exp=[(len(orders),)]),
    ]


def run_sqlite(db: str) -> Dict[str, Any]:
    """exec = valid + executes + equals python expectation; detect = defective variant is rejected by validator or fails to pass."""
    schema = load_database(db)
    out = []
    for c in sqlite_cases(db):
        v = validate_sql(c["sql"], schema)
        res = execute_query(c["sql"], db) if v["is_valid"] else {"rows": [], "columns": []}
        got = [tuple(r[col] for col in res.get("columns", [])) for r in res.get("rows", [])]
        exec_ok = v["is_valid"] and rows_equal(got, c["exp"])
        bv = validate_sql(c["bad"], schema)
        detect_ok = not bv["is_valid"]
        out.append(dict(id=c["id"], exec=bool(exec_ok), detect=bool(detect_ok), correct=bool(exec_ok and detect_ok),
                        issues=[i["message"] for i in v.get("issues", [])], got=got[:3], exp=c["exp"][:3]))
    n = len(out)
    return dict(total=n, correct=sum(r["correct"] for r in out), accuracy=round(100.0 * sum(r["correct"] for r in out) / n, 2),
                exec_pct=round(100.0 * sum(r["exec"] for r in out) / n, 2), detect_pct=round(100.0 * sum(r["detect"] for r in out) / n, 2), cases=out)
