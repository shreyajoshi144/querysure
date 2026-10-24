"""Regression tests for the accuracy/reliability pass (multi-format evaluation, docs/03_EVALUATION.md).

Each test pins one root cause found by `python -m evaluation.multiformat` on real CSV/Excel/JSON/SQLite files: a correct
query that was rejected, wrongly flagged, or pushed toward a wrong "repair". Datasets are built here, so CI needs no
private files. The same file also checks that genuinely wrong SQL is still flagged (fixes must not just silence checks).
"""
from __future__ import annotations

import os
import re
import sqlite3

import pytest

from modules.query_planner import build_plan, check_sql_against_plan
from modules.result_verifier import semantic_validate
from modules.schema_engine import load_database
from modules.sql_intent_checker import check_intent
from modules.sql_validator import validate_sql
from modules.value_grounding import ground_question

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _db(tmp_path, name, script):
    p = tmp_path / name
    con = sqlite3.connect(p)
    con.executescript(script)
    con.commit()
    con.close()
    return str(p), load_database(str(p))


@pytest.fixture()
def countries(tmp_path):
    rows = [("Albania", "AL", "ALL", 2877797, 28748, 0), ("Austria", "AT", "EUR", 9006398, 83879, 1),
            ("Belarus", "BY", "BYN", 9449323, 207600, 0), ("Germany", "DE", "EUR", 83783942, 357114, 1),
            ("Iceland", "IS", "ISK", 341243, 103000, 0), ("Norway", "NO", "NOK", 5421241, 385207, 0),
            ("Spain", "ES", "EUR", 46754778, 505990, 1)]
    script = "CREATE TABLE countries(name TEXT, code TEXT, currency TEXT, population INTEGER, area_km2 REAL, eu_member INTEGER);" + \
        "".join(f"INSERT INTO countries VALUES('{n}','{c}','{cur}',{p},{a},{e});" for n, c, cur, p, a, e in rows)
    return _db(tmp_path, "countries.db", script)


@pytest.fixture()
def shop(tmp_path):
    """Northwind-shaped: no declared FKs, a CustomerId with orphans, ShipVia -> Shipper, several date columns."""
    script = """
    CREATE TABLE Customer(Id TEXT PRIMARY KEY, CompanyName TEXT, Country TEXT);
    CREATE TABLE Shipper(Id INTEGER PRIMARY KEY, CompanyName TEXT);
    CREATE TABLE Employee(Id INTEGER PRIMARY KEY, FirstName TEXT, BirthDate TEXT, Country TEXT);
    CREATE TABLE "Order"(Id INTEGER PRIMARY KEY, CustomerId TEXT, EmployeeId INTEGER, OrderDate TEXT, ShippedDate TEXT,
                         ShipVia INTEGER, ShipCountry TEXT, Freight REAL);
    INSERT INTO Shipper VALUES (1,'Speedy'),(2,'United'),(3,'Federal');
    INSERT INTO Employee VALUES (1,'Nancy','1980-01-02','USA'),(2,'Andrew','1975-05-06','UK');
    """
    custs = [(f"C{i:02d}", f"Cust {i}", ["Germany", "USA", "France"][i % 3]) for i in range(10)]
    script += "".join(f"INSERT INTO Customer VALUES('{a}','{b}','{c}');" for a, b, c in custs)
    for i in range(1, 41):
        cid = f"C{i % 10:02d}" if i % 10 else "ZZZ"          # 4 of 40 orders reference a missing customer (90% contained)
        script += (f"INSERT INTO \"Order\" VALUES({i},'{cid}',{1 + i % 2},'2013-{1 + i % 12:02d}-05','2013-{1 + i % 12:02d}-09',"
                   f"{1 + i % 3},'{['Germany', 'USA', 'France'][i % 3]}',{i * 1.5});")
    return _db(tmp_path, "shop.db", script)


# ------------------------------------------------------------------------------------------------ grounding
def test_short_codes_are_not_grounded_from_everyday_words(countries):
    path, schema = countries
    for q in ("Is Iceland in the EU?", "What is the median population of EU members?",
              "¿Cuántos países son miembros de la UE?", "Combien de pays sont membres de l'UE ?", "top 5 countries by population"):
        hits = ground_question(q, schema, path)
        assert not [h for h in hits if h["column"] in ("code", "currency")], (q, hits)


def test_short_codes_written_as_stored_are_still_grounded(countries):
    path, schema = countries
    hits = ground_question("Which countries use EUR?", schema, path)
    assert any(h["column"] == "currency" and h["value"] == "EUR" for h in hits)


def test_one_word_titles_in_large_free_text_columns_need_their_stored_case(tmp_path):
    titles = ";".join(f"INSERT INTO tracks VALUES({i},'Song {i}', 1)" for i in range(300))
    path, schema = _db(tmp_path, "t.db", "CREATE TABLE tracks(TrackId INTEGER PRIMARY KEY, Name TEXT, GenreId INTEGER);"
                                          + titles + ";INSERT INTO tracks VALUES(999,'Seconds',1);")
    assert not ground_question("Average length in seconds of rock tracks", schema, path)
    assert ground_question("How long is Seconds?", schema, path)


# ------------------------------------------------------------------------------------------------ planner
def test_date_range_uses_the_column_the_question_names(shop):
    _, schema = shop
    plan = build_plan("How many orders were shipped in 2013?", schema, [])
    assert plan["date_range"] and plan["date_range"]["column"] == "ShippedDate"
    plan = build_plan("How many orders were placed in 2013?", schema, [])
    assert plan["date_range"] and plan["date_range"]["column"] == "OrderDate"


def test_unclear_date_column_is_not_guessed(shop):
    _, schema = shop
    plan = build_plan("What was the total freight in 2013?", schema, [])
    assert plan["date_range"] is None or plan["date_range"]["column"] != "BirthDate"


@pytest.mark.parametrize("q", ["Which project appears most?", "Which currency is used by the most countries?",
                               "Which country has the most customers?"])
def test_frequency_and_most_entity_questions_count(tmp_path, q):
    path, schema = _db(tmp_path, "f.db", "CREATE TABLE tasks(project_name TEXT, days_required INTEGER);"
                                          "CREATE TABLE customers(country TEXT, spend REAL);"
                                          "CREATE TABLE countries(currency TEXT, population INTEGER);")
    plan = build_plan(q, schema, [])
    assert plan["aggregation"] == "count", plan


def test_plural_which_question_does_not_force_limit_1(countries):
    _, schema = countries
    assert build_plan("Which countries had the highest population?", schema, [])["limit"] is None
    assert build_plan("Which country had the highest population?", schema, [])["limit"] == 1


def test_top_level_is_not_a_top_n_ranking(countries):
    _, schema = countries
    plan = build_plan("Which top-level country has the lowest population?", schema, [])
    assert plan["order"] and plan["order"]["direction"] == "asc"


def test_threshold_with_million_and_having(countries):
    _, schema = countries
    f = [x for x in build_plan("Which countries have a population over 50 million?", schema, [])["filters"] if x["source"] == "threshold"]
    assert f and f[0]["value"] == 50_000_000
    plan = build_plan("Which currencies have an average population below 20 million?", schema, [])
    assert not [x for x in plan["filters"] if x["source"] == "threshold"] and not plan["verifiable"]


def test_number_of_a_numeric_measure_is_a_sum_but_rows_are_counted(tmp_path):
    path, schema = _db(tmp_path, "m.db", "CREATE TABLE tasks(assigned_to TEXT, days_required INTEGER, data_value REAL);"
                                          "INSERT INTO tasks VALUES('a',3,1.0),('b',4,NULL);")
    plan = build_plan("What is the total number of days required per person?", schema, [])
    assert plan["aggregation"] == "sum" and plan["measure"]["column"] == "days_required"
    assert build_plan("How many rows have no data value?", schema, [])["aggregation"] == "count"


def test_median_and_time_buckets_are_not_independently_verified(shop):
    _, schema = shop
    assert build_plan("What is the median freight?", schema, [])["verifiable"] is False
    assert build_plan("How many orders were placed in each month of 2013?", schema, [])["verifiable"] is False


def test_same_value_in_two_columns_accepts_either(tmp_path):
    path, schema = _db(tmp_path, "g.db", "CREATE TABLE customers(CustomerId INTEGER PRIMARY KEY, Country TEXT);"
                                          "CREATE TABLE invoices(InvoiceId INTEGER PRIMARY KEY, CustomerId INTEGER, BillingCountry TEXT, Total REAL);"
                                          "INSERT INTO customers VALUES(1,'Germany');INSERT INTO invoices VALUES(1,1,'Germany',5.0);")
    q = "What is the total revenue from customers in Germany?"
    plan = build_plan(q, schema, ground_question(q, schema, path))
    sql = "SELECT SUM(i.Total) FROM invoices AS i JOIN customers AS c ON i.CustomerId = c.CustomerId WHERE c.Country = 'Germany'"
    assert not [i for i in check_sql_against_plan(sql, plan) if i["type"] == "plan_filter"]


def test_same_column_name_in_several_tables_is_ambiguous_for_grouping(shop):
    _, schema = shop
    plan = build_plan("Which 3 countries received the most orders?", schema, [])
    assert not any(g["column"] == "Country" for g in plan["group_by"])


# ------------------------------------------------------------------------------------------------ checks accept valid SQL
def test_conditional_count_counts(countries):
    path, schema = countries
    sql = "SELECT SUM(CASE WHEN c.eu_member = 1 THEN 1 ELSE 0 END) AS n FROM countries AS c"
    assert not semantic_validate("How many countries are EU members?", sql, schema, path)["issues"]


def test_conditional_aggregation_on_one_column_with_two_values(tmp_path):
    path, schema = _db(tmp_path, "c.db", "CREATE TABLE fin(industry TEXT, variable TEXT, value REAL);"
                                          "INSERT INTO fin VALUES('Mining','Operating profit',5),('Mining','Sales',20);")
    q = "What is the Operating profit as a percentage of Sales for Mining?"
    sql = ("SELECT SUM(CASE WHEN f.variable = 'Operating profit' THEN f.value END) * 100.0 / "
           "SUM(CASE WHEN f.variable = 'Sales' THEN f.value END) FROM fin AS f WHERE f.industry = 'Mining'")
    issues = [i for i in semantic_validate(q, sql, schema, path)["issues"] if i["type"] == "plan_exact_value"]
    assert not issues


def test_spanish_total_question_answered_with_count_is_not_flagged(countries):
    path, schema = countries
    assert not semantic_validate("¿Cuántos países hay en total?", "SELECT COUNT(*) FROM countries AS c", schema, path)["issues"]


# ------------------------------------------------------------------------------------------------ validator
def test_select_star_is_accepted_with_a_warning(countries):
    _, schema = countries
    r = validate_sql("SELECT * FROM countries WHERE name = 'Spain'", schema)
    assert r["is_valid"] and any("SELECT *" in w for w in r["warnings"])


def test_named_foreign_key_with_orphans_is_a_valid_join_and_warns(shop):
    _, schema = shop
    r = validate_sql('SELECT c.CompanyName, COUNT(*) FROM "Order" AS o JOIN Customer AS c ON o.CustomerId = c.Id GROUP BY c.CompanyName', schema)
    assert r["is_valid"], r["issues"]
    assert any("inner join leaves them out" in w for w in r["warnings"])


def test_column_named_after_the_target_table_is_a_valid_join(shop):
    _, schema = shop
    r = validate_sql('SELECT s.CompanyName, COUNT(*) FROM "Order" AS o JOIN Shipper AS s ON o.ShipVia = s.Id GROUP BY s.CompanyName', schema)
    assert r["is_valid"], r["issues"]


def test_unrelated_joins_are_still_rejected(shop):
    _, schema = shop
    assert not validate_sql('SELECT o.Id FROM "Order" AS o JOIN Customer AS c ON o.EmployeeId = c.Id', schema)["is_valid"]
    assert not validate_sql('SELECT o.Id FROM "Order" AS o JOIN Shipper AS s ON o.EmployeeId = s.Id', schema)["is_valid"]


def test_joining_two_aggregates_on_their_only_group_key_is_valid(countries):
    """Per-group results combined in one query (profit / sales per industry) join one-to-one without a declared key."""
    _, schema = countries
    ok = """WITH a AS (SELECT currency, SUM(population) AS pop FROM countries GROUP BY currency),
                 b AS (SELECT currency AS cur, COUNT(*) AS n FROM countries GROUP BY cur)
            SELECT a.currency, a.pop / b.n FROM a JOIN b ON a.currency = b.cur"""
    assert validate_sql(ok, schema)["is_valid"]
    for bad in ["SELECT x.name FROM countries AS x JOIN countries AS y ON x.currency = y.currency",
                """WITH a AS (SELECT currency, name, SUM(population) AS pop FROM countries GROUP BY currency, name),
                        b AS (SELECT currency, COUNT(*) AS n FROM countries GROUP BY currency)
                   SELECT a.name FROM a JOIN b ON a.currency = b.currency""",
                """WITH a AS (SELECT currency, population FROM countries),
                        b AS (SELECT currency, COUNT(*) AS n FROM countries GROUP BY currency)
                   SELECT a.currency FROM a JOIN b ON a.currency = b.currency"""]:
        assert not validate_sql(bad, schema)["is_valid"], bad


def test_column_neither_grouped_nor_aggregated_is_flagged(shop):
    """SQLite accepts it and returns an arbitrary row's value per group (seen live: each project's 'finish date')."""
    _, schema = shop
    flagged = check_intent("When was each customer's last order?",
                           'SELECT o.CustomerId, o.OrderDate FROM "Order" AS o GROUP BY o.CustomerId', schema)
    assert any(i["type"] == "intent_ungrouped" for i in flagged)
    for ok in ['SELECT o.CustomerId, MAX(o.OrderDate) FROM "Order" AS o GROUP BY o.CustomerId',
               'SELECT o.CustomerId AS c, COUNT(*) FROM "Order" AS o GROUP BY c',
               'SELECT o.CustomerId, COUNT(*) FROM "Order" AS o GROUP BY 1',
               'SELECT c.CompanyName, COUNT(*) FROM "Order" AS o JOIN Customer AS c ON o.CustomerId = c.Id GROUP BY c.Id']:
        assert not any(i["type"] == "intent_ungrouped" for i in check_intent("Orders per customer", ok, schema)), ok


@pytest.mark.parametrize("q, foreign", [
    ("¿Cuántos países son miembros de la UE?", True),
    ("Quels 3 pays ont reçu le plus de commandes ?", True),
    ("Wie viele Bestellungen wurden nach dem geforderten Datum versandt?", True),
    ("प्रत्येक प्रोजेक्ट के लिए औसत आवश्यक दिन", True),
    ("How many EU members are there?", False),
    ("most common project?", False),
    ("Which countries use the euro and have a population over 5 million?", False),
])
def test_retrieval_score_does_not_mark_non_english_questions_low_confidence(q, foreign):
    """The TF-IDF score compares question words with English schema names, so it is meaningless for other languages."""
    from modules.sql_generator import _non_english_question
    assert _non_english_question(q) is foreign


def test_read_only_guarantees_are_unchanged(countries):
    _, schema = countries
    for bad in ("DELETE FROM countries", "SELECT 1; DROP TABLE countries", "ATTACH DATABASE 'x' AS x",
                "PRAGMA table_info(countries)", "SELECT name FROM sqlite_master"):
        assert not validate_sql(bad, schema)["is_valid"], bad


# ------------------------------------------------------------------------------------------------ wrong SQL is still flagged
@pytest.mark.parametrize("q,sql", [
    ("What are the 5 most populous countries?", "SELECT c.name FROM countries AS c ORDER BY c.population DESC LIMIT 3"),
    ("Which countries have a population over 50 million?", "SELECT c.name FROM countries AS c WHERE c.population > 50"),
    ("Which country has the lowest population?", "SELECT c.name FROM countries AS c ORDER BY c.population DESC LIMIT 1"),
    ("What is the capital of Germany?", "SELECT c.name FROM countries AS c WHERE c.name = 'germany'"),
])
def test_wrong_sql_is_still_flagged(countries, q, sql):
    path, schema = countries
    assert semantic_validate(q, sql, schema, path)["issues"], (q, sql)


def test_amount_ranking_answered_by_counting_rows_is_flagged(tmp_path):
    path, schema = _db(tmp_path, "r.db", "CREATE TABLE genres(GenreId INTEGER PRIMARY KEY, Name TEXT);"
                                          "CREATE TABLE sales(GenreId INTEGER, UnitPrice REAL, Quantity INTEGER);")
    sql = "SELECT g.Name, COUNT(*) AS n FROM sales AS s JOIN genres AS g ON s.GenreId = g.GenreId GROUP BY g.Name ORDER BY n DESC LIMIT 5"
    assert any("only counts rows" in i["message"] for i in check_intent("What are the top 5 genres by revenue?", sql, schema))


# ------------------------------------------------------------------------------------------------ UI: dataset switch + history
def _app():
    with open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8") as fh:
        return fh.read()


def test_history_has_no_reopen_or_rerun_and_confirms_deletes():
    app = _app()
    assert "histRerun" not in app and "histAsk" not in app and "Reopen question" not in app and "Rerun on current dataset" not in app
    assert "Result not saved" in app
    detail = app[app.index("async function openHistoryEntry"):app.index("(function wireHistory()")]
    assert "histDeleteConfirm" in detail and "histDeleteCancel" in detail
    # the DELETE request only happens in the confirm handler
    assert detail.count("method: 'DELETE'") == 1
    assert detail.index("method: 'DELETE'") > detail.index("$('histDeleteConfirm').onclick")


def test_switching_dataset_clears_the_previous_answer_and_locks_ask():
    app = _app()
    upload = app[app.index("async function uploadFileInner"):app.index("function throwApiError")]
    fail = upload.index("Upload failed'); showGlobalError")
    activate = upload.index("onDatasetActivated()")
    assert activate > fail, "a failed upload must return before anything is activated"
    assert re.search(r"async function uploadFile\(file\) \{[^}]*setPipelineBusy\(true\)", app)
    assert re.search(r"async function connectSample\(\) \{[^}]*setPipelineBusy\(true\)", app)
    reset = app[app.index("function resetAnswerState"):app.index("function onDatasetActivated")]
    assert "clearSheet()" in reset and "$('question')" in reset
    assert "if (epoch !== datasetEpoch) return;" in app
    sheet = app[app.index("function clearSheet"):app.index("function resetAnswerState")]
    assert "$('sheetQuestion').textContent = ''" in sheet, "the hidden sheet must not keep the old question"
    poll = app[app.index("async function pollJob"):app.index("async function connectSample")]
    assert "res.error" in poll, "a failed upload job must show the worker's reason, not only 'try again'"


# ------------------------------------------------------------------------------------------------ found by the live run
def _wide_schema_semantic(tmp_path):
    """More than SMALL_SCHEMA_COLUMNS columns, so retrieval must rank and connect tables instead of showing all."""
    pad = ", ".join(f"Extra{i} TEXT" for i in range(40))
    script = f"""
    CREATE TABLE Category(Id INTEGER PRIMARY KEY, CategoryName TEXT, {pad});
    CREATE TABLE Product(Id INTEGER PRIMARY KEY, ProductName TEXT, CategoryId INTEGER, {pad});
    CREATE TABLE "Order"(Id INTEGER PRIMARY KEY, OrderDate TEXT, ShipCountry TEXT, {pad});
    CREATE TABLE OrderDetail(Id INTEGER PRIMARY KEY, OrderId INTEGER, ProductId INTEGER, Quantity INTEGER, {pad});
    CREATE TABLE Employee(Id INTEGER PRIMARY KEY, LastName TEXT, {pad});
    INSERT INTO Category(Id, CategoryName) VALUES (1,'Beverages');
    INSERT INTO Product(Id, ProductName, CategoryId) VALUES (1,'Chai',1);
    INSERT INTO "Order"(Id, OrderDate, ShipCountry) VALUES (1,'2013-01-01','Germany');
    INSERT INTO OrderDetail(Id, OrderId, ProductId, Quantity) VALUES (1,1,1,5);
    INSERT INTO Employee(Id, LastName) VALUES (1,'Davolio');
    """
    from modules.semantic_layer import build_semantic_layer
    _, schema = _db(tmp_path, "wide.db", script)
    return build_semantic_layer(schema)


def test_retrieval_matches_plural_questions_to_camelcase_tables(tmp_path):
    from modules.retrieval_engine import retrieve_context
    sem = _wide_schema_semantic(tmp_path)
    # top_k_tables=2: the ranking itself (not "every table fits") must put the named table first
    assert "Order" in retrieve_context("Which 3 countries received the most orders?", sem, top_k_tables=2)["relevant_tables"][:2]
    assert "Product" in retrieve_context("Number of products in every category", sem, top_k_tables=2)["relevant_tables"][:2]


def test_retrieval_adds_the_tables_that_connect_the_selected_ones(tmp_path):
    from modules.retrieval_engine import retrieve_context
    sem = _wide_schema_semantic(tmp_path)
    r = retrieve_context("Total quantity per category name", sem, top_k_tables=2)
    ranked = [t["table"] for t in r["top_tables"] if not t.get("included_because")]
    assert set(ranked) == {"Category", "OrderDetail"}, ranked
    bridge = [t for t in r["top_tables"] if t["table"] == "Product"]
    assert bridge and "connects" in bridge[0]["included_because"]   # Category <- Product <- OrderDetail


def test_small_schema_shows_every_non_empty_table(tmp_path):
    from modules.retrieval_engine import retrieve_context
    from modules.semantic_layer import build_semantic_layer
    script = "".join(f"CREATE TABLE t{i}(id INTEGER PRIMARY KEY, label TEXT); INSERT INTO t{i} VALUES (1,'x');" for i in range(8))
    script += "CREATE TABLE empty_one(id INTEGER PRIMARY KEY);"
    _, schema = _db(tmp_path, "small.db", script)
    tables = retrieve_context("label of t3", build_semantic_layer(schema))["relevant_tables"]
    assert {f"t{i}" for i in range(8)} <= set(tables)               # all 8 non-empty tables, more than top_k (5)


def test_double_quoted_values_become_string_literals_only_when_they_are_not_names():
    from modules.sqlite_compile_check import requote_string_literals
    s = {"tables": {"fin": {"columns": [{"name": "status"}, {"name": "region"}, {"name": "v"}]}}}
    fixed = requote_string_literals('SELECT SUM(f.v) FROM fin AS f WHERE f.status = "Unadjusted" AND f.region IN ("North")', s)
    assert fixed == "SELECT SUM(f.v) FROM fin AS f WHERE f.status = 'Unadjusted' AND f.region IN ('North')"
    assert requote_string_literals('SELECT "v" FROM fin WHERE "status" = "region"', s) is None   # real names untouched
    assert requote_string_literals('SELECT "nope" FROM fin', s) is None                         # not compared: still invalid


def test_generate_sql_repairs_double_quoted_values_without_an_llm_round(countries, monkeypatch):
    import json as _json
    import modules.sql_generator as sg
    from modules.semantic_layer import build_semantic_layer
    path, schema = countries
    calls = []

    def fake(system, user, key):
        calls.append(1)
        return _json.dumps({"sql": 'SELECT COUNT(*) FROM countries AS c WHERE c.currency = "EUR"', "explanation": "",
                            "confidence": "High", "complexity": "Simple"}), "groq:stub", []
    monkeypatch.setattr(sg, "_run_provider_chain", fake)
    out = sg.generate_sql(question="How many countries use EUR?", schema=schema, api_key="k",
                          semantic=build_semantic_layer(schema), retrieval=None, db_path=path)
    assert out["validation"]["is_valid"] and "'EUR'" in out["sql"] and len(calls) == 1
    assert "double-quoted values turned into string literals" in out["deterministic_fixes"]


def _stores_orders(tmp_path):
    """North has the most stores (3) but South has the most orders (4), so counting the wrong table changes the answer."""
    stores = [(1, "North"), (2, "North"), (3, "North"), (4, "South")]
    orders = [(10, 1), (11, 2), (12, 4), (13, 4), (14, 4), (15, 4)]
    script = "CREATE TABLE stores(store_id INTEGER PRIMARY KEY, region TEXT);" + \
        "CREATE TABLE orders(order_id INTEGER PRIMARY KEY, store_id INTEGER REFERENCES stores(store_id));" + \
        "".join(f"INSERT INTO stores VALUES({i},'{r}');" for i, r in stores) + \
        "".join(f"INSERT INTO orders VALUES({o},{s});" for o, s in orders)
    return _db(tmp_path, "stores.db", script)


def _generate(monkeypatch, path, schema, question, sql):
    import json as _json
    import modules.sql_generator as sg
    from modules.semantic_layer import build_semantic_layer
    monkeypatch.setattr(sg, "_run_provider_chain", lambda s, u, k: (_json.dumps(
        {"sql": sql, "explanation": "", "confidence": "High", "complexity": "Simple"}), "groq:stub", []))
    return sg.generate_sql(question=question, schema=schema, api_key="k", semantic=build_semantic_layer(schema),
                           retrieval=None, db_path=path)


def test_most_orders_per_region_is_not_verified_against_a_count_of_stores(tmp_path, monkeypatch):
    """Seen live on the sample data: the correct join was reported as a Mismatch because the reference counted stores."""
    path, schema = _stores_orders(tmp_path)
    ok = ("SELECT s.region, COUNT(o.order_id) AS n FROM orders AS o JOIN stores AS s ON o.store_id = s.store_id "
          "GROUP BY s.region ORDER BY n DESC, s.region LIMIT 1")
    out = _generate(monkeypatch, path, schema, "Which region placed the most orders?", ok)
    assert out["validation"]["is_valid"]
    assert (out.get("verification") or {}).get("status") != "mismatch", out.get("verification")


def test_most_stores_per_region_still_catches_a_query_that_counts_orders(tmp_path, monkeypatch):
    path, schema = _stores_orders(tmp_path)
    wrong = ("SELECT s.region, COUNT(o.order_id) AS n FROM orders AS o JOIN stores AS s ON o.store_id = s.store_id "
             "GROUP BY s.region ORDER BY n DESC, s.region LIMIT 1")
    out = _generate(monkeypatch, path, schema, "Which region has the most stores?", wrong)
    assert (out.get("verification") or {}).get("status") == "mismatch", out.get("verification")
    right = "SELECT region, COUNT(*) AS n FROM stores GROUP BY region ORDER BY n DESC, region LIMIT 1"
    assert _generate(monkeypatch, path, schema, "Which region has the most stores?", right)["verification"]["status"] == "verified_match"


def _insights(path, sql, with_db=True):
    from modules.business_insight_agent import generate_insights
    from modules.query_result_engine import execute_query
    qr = execute_query(sql, path)
    assert qr["success"], qr
    return qr, generate_insights({}, qr, None, None, db_path=path if with_db else None)


_PER_REGION = ("SELECT s.region, COUNT(o.order_id) AS orders FROM orders AS o JOIN stores AS s ON o.store_id = s.store_id "
               "GROUP BY s.region ORDER BY orders DESC, s.region")


def test_share_of_total_for_a_top_1_result_uses_every_group(tmp_path):
    """Seen live: 'North contributes 100.00% of total order_count' for a LIMIT 1 answer (really 1,503 of 3,000)."""
    path, _ = _stores_orders(tmp_path)
    qr, ins = _insights(path, _PER_REGION + " LIMIT 1")
    assert [(r["region"], r["orders"]) for r in qr["rows"]] == [("South", 4)]          # the ranking itself
    text = " ".join(ins["key_findings"])
    assert "South contributes 66.67% of total orders." in text, text                     # 4 of 6 orders, not 100%
    assert "100.00%" not in text and "top two contributors" not in text                  # one row is not "top two"


def test_share_of_total_without_a_limit_uses_the_returned_rows(tmp_path):
    path, _ = _stores_orders(tmp_path)
    qr, ins = _insights(path, _PER_REGION)
    assert [(r["region"], r["orders"]) for r in qr["rows"]] == [("South", 4), ("North", 2)]
    assert "South contributes 66.67% of total orders." in " ".join(ins["key_findings"])
    assert "(66.7% of the total)" in ins["headline"]


def test_limited_result_states_no_share_when_the_full_total_is_unavailable(tmp_path):
    path, _ = _stores_orders(tmp_path)
    _, ins = _insights(path, _PER_REGION + " LIMIT 1", with_db=False)
    assert not any("contributes" in f or "% of total" in f for f in ins["key_findings"]), ins["key_findings"]
    _, ins = _insights(path, _PER_REGION + " LIMIT 2", with_db=False)
    assert "of the total" not in ins["headline"]


def test_only_the_primary_sort_key_decides_the_ranking_direction():
    s = {"tables": {}}
    ok = "SELECT p, COUNT(*) AS n FROM t GROUP BY p ORDER BY n DESC, p ASC LIMIT 1"
    assert not check_intent("Which project appears most?", ok, s)
    assert check_intent("Which project appears most?", ok.replace("n DESC, p ASC", "n ASC"), s)


def test_row_filter_after_where_is_not_a_having_threshold(tmp_path):
    path, schema = _db(tmp_path, "u.db", "CREATE TABLE subs(units INTEGER, plan TEXT);INSERT INTO subs VALUES(5,'a'),(20,'b');")
    plan = build_plan("mean units where units exceeds 10", schema, [])
    assert [f for f in plan["filters"] if f["source"] == "threshold"]


def test_words_inside_a_stored_value_are_not_question_wording(tmp_path):
    path, schema = _db(tmp_path, "a.db", "CREATE TABLE subs(plan_name TEXT, amount REAL);"
                                          "INSERT INTO subs VALUES('Premium Tier (Annual)', 10), ('Basic', 5);")
    q = "What is the total amount for Premium Tier (Annual)?"
    assert build_plan(q, schema, ground_question(q, schema, path))["verifiable"] is True
