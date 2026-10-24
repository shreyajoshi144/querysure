"""Deliberately WRONG candidate SQL. Each one executes, but does not answer its question.

The pipeline "detects" a case when it rejects the SQL, reports a plan/intent/value issue, finds a verification mismatch
or suspicious empty result, or asks for a repair. A fix that only silences warnings shows up here as lost detection.
`detectable` marks mistakes the deterministic layers can see in principle; the rest (e.g. a forgotten discount, picking
the wrong one of two plausible hierarchies) need a model or a human and are reported separately, not hidden.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .cases import CSV_T, J_T, X_T


@dataclass
class Negative:
    id: str
    dataset: str
    question: str
    sql: str
    mistake: str
    detectable: bool = True


NEGATIVES: List[Negative] = [
    Negative("neg-x1", "xlsx_projects", "How many tasks start in February 2024?",
             f"SELECT COUNT(*) FROM {X_T} AS p WHERE p.end_date >= '2024-02-01' AND p.end_date < '2024-03-01'", "wrong date column"),
    Negative("neg-x2", "xlsx_projects", "How many tasks start in February 2024?",
             f"SELECT COUNT(*) FROM {X_T} AS p WHERE p.start_date >= '2024-02-01' AND p.start_date < '2024-02-28'", "range drops 28-29 Feb"),
    Negative("neg-x3", "xlsx_projects", "Show all tasks for the Marketing project",
             f"SELECT p.task_name FROM {X_T} AS p WHERE p.project_name = 'marketing'", "value spelled differently from the data"),
    Negative("neg-x4", "xlsx_projects", "Which project appears most?",
             f"SELECT p.project_name, COUNT(*) AS n FROM {X_T} AS p GROUP BY p.project_name ORDER BY n ASC LIMIT 1", "ascending instead of descending"),
    Negative("neg-x5", "xlsx_projects", "What is the average progress per project, as a percentage?",
             f"SELECT p.project_name, SUM(p.progress) FROM {X_T} AS p GROUP BY p.project_name", "SUM instead of AVG"),
    Negative("neg-x6", "xlsx_projects", "How many tasks per project are completed?",
             f"SELECT p.project_name, COUNT(*) FROM {X_T} AS p GROUP BY p.project_name", "counts all tasks, completion filter dropped", detectable=False),
    Negative("neg-j1", "json_countries", "What are the 5 most populous countries?",
             f"SELECT c.name, c.population FROM {J_T} AS c ORDER BY c.population DESC LIMIT 3", "LIMIT 3 instead of 5"),
    Negative("neg-j2", "json_countries", "Which countries have a population over 50 million?",
             f"SELECT c.name FROM {J_T} AS c WHERE c.population > 50", "threshold missing the million multiplier"),
    Negative("neg-j3", "json_countries", "What is the capital of Germany?",
             f"SELECT c.capital FROM {J_T} AS c WHERE c.name = 'germany'", "value spelled differently from the data"),
    Negative("neg-j4", "json_countries", "Which currency is used by the most countries?",
             f"SELECT c.currency, COUNT(*) AS n FROM {J_T} AS c GROUP BY c.currency ORDER BY n ASC LIMIT 1", "ascending instead of descending"),
    Negative("neg-j5", "json_countries", "What is the average area of countries outside the EU?",
             f"SELECT SUM(c.area_km2) FROM {J_T} AS c WHERE c.eu_member = 0", "SUM instead of AVG"),
    Negative("neg-j6", "json_countries", "How many countries are EU members?",
             f"SELECT COUNT(*) FROM {J_T} AS c", "EU condition dropped", detectable=False),
    Negative("neg-c1", "csv_financial", "Which 5 top-level industries had the highest unadjusted sales in the March 2026 quarter?",
             f"""SELECT b.series_title_2, SUM(b.data_value) AS s FROM {CSV_T} AS b WHERE b.series_title_1 = 'Sales (operating income)'
                 AND b.series_title_4 = 'Unadjusted' AND b.period = 2026.03 GROUP BY b.series_title_2 ORDER BY s DESC LIMIT 3""", "LIMIT 3 instead of 5"),
    Negative("neg-c2", "csv_financial", "Which 5 top-level industries had the highest unadjusted sales in the March 2026 quarter?",
             f"""SELECT b.series_title_2, SUM(b.data_value) AS s FROM {CSV_T} AS b WHERE b.series_title_1 = 'Sales (operating income)'
                 AND b.period = 2026.03 GROUP BY b.series_title_2 ORDER BY s DESC LIMIT 5""", "'unadjusted' filter dropped (triple counts adjusted series)"),
    Negative("neg-c3", "csv_financial", "Which top-level industry had the lowest unadjusted operating profit in the March 2026 quarter?",
             f"""SELECT b.series_title_2, SUM(b.data_value) AS s FROM {CSV_T} AS b WHERE b.series_title_1 = 'Operating profit'
                 AND b.series_title_4 = 'Unadjusted' AND b.period = 2026.03 GROUP BY b.series_title_2 ORDER BY s DESC LIMIT 1""", "highest instead of lowest"),
    Negative("neg-c4", "csv_financial", "Which 5 top-level industries had the highest unadjusted sales in the March 2026 quarter?",
             f"""SELECT b.series_title_2, SUM(b.data_value) AS s FROM {CSV_T} AS b WHERE b.series_title_1 = 'Sales (operating income)'
                 AND b.series_title_4 = 'Unadjusted' AND b.period = 2026.03 GROUP BY b.series_title_2 ORDER BY s DESC LIMIT 5""",
             "Level 1 and Level 2 hierarchies mixed", detectable=False),
    Negative("neg-n1", "sqlite_northwind", "How many orders were shipped in 2013?",
             """SELECT COUNT(*) FROM "Order" AS o WHERE o.OrderDate >= '2013-01-01' AND o.OrderDate < '2014-01-01'""", "order date used for 'shipped'"),
    Negative("neg-n2", "sqlite_northwind", "What are the top 3 products by quantity sold?",
             """SELECT p.ProductName, SUM(od.Quantity) AS q FROM OrderDetail AS od JOIN Product AS p ON od.ProductId = p.Id
                GROUP BY p.ProductName ORDER BY q DESC LIMIT 5""", "LIMIT 5 instead of 3"),
    Negative("neg-n3", "sqlite_northwind", "What was the total revenue in 2013?",
             """SELECT SUM(od.UnitPrice * od.Quantity * (1 - od.Discount)) FROM OrderDetail AS od""", "year filter dropped"),
    Negative("neg-n4", "sqlite_northwind", "How many products are discontinued?",
             "SELECT COUNT(*) FROM Product AS p WHERE p.Discontinued = 0", "inverted condition", detectable=False),
    Negative("neg-h1", "db_chinook", "What are the top 5 genres by revenue?",
             """SELECT g.Name, COUNT(*) AS n FROM invoice_items AS ii JOIN tracks AS t ON ii.TrackId = t.TrackId
                JOIN genres AS g ON t.GenreId = g.GenreId GROUP BY g.Name ORDER BY n DESC LIMIT 5""", "COUNT of sales instead of revenue SUM"),
    Negative("neg-h2", "db_chinook", "What was the monthly revenue in 2012?",
             """SELECT strftime('%m', i.InvoiceDate) AS m, SUM(i.Total) FROM invoices AS i GROUP BY m ORDER BY m""", "year filter dropped"),
    Negative("neg-h3", "db_chinook", "What is the total revenue from customers in Germany?",
             """SELECT SUM(i.Total) FROM invoices AS i JOIN customers AS c ON i.CustomerId = c.CustomerId WHERE c.Country = 'germany'""",
             "value spelled differently from the data"),
    Negative("neg-h4", "db_chinook", "Which country has the most customers?",
             "SELECT c.Country, COUNT(*) AS n FROM customers AS c GROUP BY c.Country ORDER BY n ASC LIMIT 1", "ascending instead of descending"),
]
