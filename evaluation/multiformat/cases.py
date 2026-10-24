"""Evaluation cases. Expected results are computed from the RAW source files (see sources.py), never from SQL.

Every case has an English question, optional variants (other languages, informal wording, typos, incomplete sentences),
the candidate SQL a capable model would write, and an `expected` function. Variants reuse the base case's SQL and
expected result: the meaning is the same, only the wording changes, which exercises the question-dependent layers
(grounding, planning, intent checks, verification) without changing the right answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List

import pandas as pd

from .sources import Sources

Rows = List[tuple]


@dataclass
class Case:
    id: str
    dataset: str
    category: str
    question: str
    sql: str
    expected: Callable[[Sources], Rows]
    ordered: bool = False
    variants: Dict[str, str] = field(default_factory=dict)


def _t(df: pd.DataFrame, cols: List[str]) -> Rows:
    out = []
    for rec in df[cols].itertuples(index=False):
        out.append(tuple(None if (isinstance(v, float) and pd.isna(v)) else (v.item() if hasattr(v, "item") else v) for v in rec))
    return out


def _one(v) -> Rows:
    return [(v.item() if hasattr(v, "item") else v,)]


def _median(s: pd.Series) -> float:
    return float(s.dropna().median())


# --------------------------------------------------------------------------------------------- CSV: NZ business data
CSV_T = "business_financial_data_march_2026_quarter"


def _l1(src: Sources) -> pd.DataFrame:
    d = src.csv()
    return d[(d["Group"] == "Industry by financial variable (NZSIOC Level 1)") & (d["Series_title_4"] == "Unadjusted")]


def _csv_top5_sales(src):
    d = _l1(src)
    s = d[(d.Series_title_1 == "Sales (operating income)") & (d.Period == "2026.03")].groupby("Series_title_2").Data_value.sum()
    s = s.reset_index().sort_values(["Data_value", "Series_title_2"], ascending=[False, True]).head(5)
    return _t(s, ["Series_title_2", "Data_value"])


def _csv_margin(src):
    d = _l1(src)
    d = d[d.Period == "2026.03"]
    p = d.pivot_table(index="Series_title_2", columns="Series_title_1", values="Data_value", aggfunc="sum")
    p = p[p["Sales (operating income)"].notna() & (p["Sales (operating income)"] != 0)]
    p["m"] = p["Operating profit"] * 100.0 / p["Sales (operating income)"]
    p = p.reset_index().sort_values(["m", "Series_title_2"], ascending=[False, True]).head(5)
    return _t(p, ["Series_title_2", "m"])


def _csv_mining_by_year(src):
    d = _l1(src)
    d = d[(d.Series_title_2 == "Mining") & (d.Series_title_1 == "Sales (operating income)")].copy()
    d["y"] = d.Period.str[:4].astype(int)
    g = d.groupby("y").Data_value.sum().reset_index().sort_values("y")
    return _t(g, ["y", "Data_value"])


def _csv_salary_share(src):
    d = _l1(src)
    d = d[(d.Series_title_2 == "Education and Training") & (d.Period == "2026.03")]
    sal = d[d.Series_title_1 == "Salaries and wages"].Data_value.sum()
    sales = d[d.Series_title_1 == "Sales (operating income)"].Data_value.sum()
    return _one(sal * 100.0 / sales)


def _csv_median_wholesale(src):
    d = _l1(src)
    return _one(_median(d[(d.Series_title_2 == "Wholesale Trade") & (d.Series_title_1 == "Sales (operating income)")].Data_value))


def _csv_qoq(src):
    d = _l1(src)
    d = d[(d.Series_title_2 == "Construction") & (d.Series_title_1 == "Sales (operating income)")]
    a = d[d.Period == "2026.03"].Data_value.sum()
    b = d[d.Period == "2025.12"].Data_value.sum()
    return _one(a - b)


def _csv_lowest_profit(src):
    d = _l1(src)
    d = d[(d.Series_title_1 == "Operating profit") & (d.Period == "2026.03")].groupby("Series_title_2").Data_value.sum()
    d = d.reset_index().sort_values(["Data_value", "Series_title_2"]).head(1)
    return _t(d, ["Series_title_2", "Data_value"])


def _csv_status(src):
    d = src.csv()
    g = d.groupby("STATUS").size().reset_index(name="n").sort_values("STATUS")
    return _t(g, ["STATUS", "n"])


def _csv_avg_construction(src):
    d = _l1(src)
    return _one(float(d[(d.Series_title_2 == "Construction") & (d.Series_title_1 == "Sales (operating income)")].Data_value.mean()))


CSV_CASES = [
    Case("csv-01", "csv_financial", "ranking",
         "Which 5 top-level industries had the highest unadjusted sales in the March 2026 quarter?",
         f"""SELECT b.series_title_2 AS industry, SUM(b.data_value) AS sales FROM {CSV_T} AS b
             WHERE b.series_title_1 = 'Sales (operating income)' AND b.series_title_4 = 'Unadjusted'
               AND b."group" = 'Industry by financial variable (NZSIOC Level 1)' AND b.period = 2026.03
             GROUP BY b.series_title_2 ORDER BY sales DESC, industry LIMIT 5""", _csv_top5_sales, ordered=True,
         variants={"es": "¿Qué 5 industrias de nivel superior tuvieron las mayores ventas sin ajustar en el trimestre de marzo de 2026?",
                   "fr": "Quelles sont les 5 industries de premier niveau avec les ventes non ajustées les plus élevées au trimestre de mars 2026 ?",
                   "de": "Welche 5 Branchen der obersten Ebene hatten im Märzquartal 2026 den höchsten nicht bereinigten Umsatz?",
                   "hi": "मार्च 2026 तिमाही में किन 5 शीर्ष-स्तरीय उद्योगों की असमायोजित बिक्री सबसे अधिक थी?",
                   "informal": "top 5 industries by sales march 2026 qtr (unadjusted, level 1)",
                   "typo": "which 5 top level industires had the higest unadjusted sales in march 2026 quater"}),
    Case("csv-02", "csv_financial", "count", "How many distinct industries are in the data?",
         f"SELECT COUNT(DISTINCT b.series_title_2) AS industries FROM {CSV_T} AS b",
         lambda s: _one(s.csv().Series_title_2.nunique()),
         variants={"es": "¿Cuántas industrias distintas hay en los datos?", "de": "Wie viele verschiedene Branchen gibt es in den Daten?",
                   "incomplete": "number of industries"}),
    Case("csv-03", "csv_financial", "filter_count", "How many records are suppressed?",
         f"SELECT COUNT(*) AS suppressed_rows FROM {CSV_T} AS b WHERE b.suppressed = 'Y'",
         lambda s: _one(int((s.csv().Suppressed == "Y").sum())),
         variants={"fr": "Combien d'enregistrements sont supprimés ?", "informal": "how many rows suppressed?"}),
    Case("csv-04", "csv_financial", "nulls", "How many rows have no data value?",
         f"SELECT COUNT(*) AS missing FROM {CSV_T} AS b WHERE b.data_value IS NULL",
         lambda s: _one(int(s.csv().Data_value.isna().sum())),
         variants={"de": "Wie viele Zeilen haben keinen Datenwert?", "typo": "how many rows have no data valeu"}),
    Case("csv-05", "csv_financial", "extreme", "What is the latest period in the data?",
         f"SELECT MAX(b.period) AS latest_period FROM {CSV_T} AS b",
         lambda s: _one(float(max(s.csv().Period)))),
    Case("csv-06", "csv_financial", "percentage",
         "Which 5 top-level industries had the highest operating profit margin (operating profit as a percentage of unadjusted sales) in the March 2026 quarter?",
         f"""SELECT b.series_title_2 AS industry,
                    SUM(CASE WHEN b.series_title_1 = 'Operating profit' THEN b.data_value END) * 100.0
                    / SUM(CASE WHEN b.series_title_1 = 'Sales (operating income)' THEN b.data_value END) AS margin_pct
             FROM {CSV_T} AS b
             WHERE b.series_title_4 = 'Unadjusted' AND b."group" = 'Industry by financial variable (NZSIOC Level 1)' AND b.period = 2026.03
             GROUP BY b.series_title_2
             HAVING SUM(CASE WHEN b.series_title_1 = 'Sales (operating income)' THEN b.data_value END) <> 0
             ORDER BY margin_pct DESC, industry LIMIT 5""", _csv_margin, ordered=True,
         variants={"es": "¿Qué 5 industrias de nivel superior tuvieron el mayor margen de beneficio operativo (beneficio operativo como porcentaje de las ventas sin ajustar) en el trimestre de marzo de 2026?",
                   "informal": "best 5 industries by op profit margin % march 2026, unadjusted"}),
    Case("csv-07", "csv_financial", "time_series", "Show total unadjusted Mining sales for each year",
         f"""SELECT CAST(b.period AS INTEGER) AS year, SUM(b.data_value) AS sales FROM {CSV_T} AS b
             WHERE b.series_title_2 = 'Mining' AND b.series_title_1 = 'Sales (operating income)' AND b.series_title_4 = 'Unadjusted'
               AND b."group" = 'Industry by financial variable (NZSIOC Level 1)'
             GROUP BY year ORDER BY year""", _csv_mining_by_year, ordered=True,
         variants={"fr": "Afficher le total des ventes non ajustées du secteur Mining pour chaque année",
                   "de": "Zeige den gesamten nicht bereinigten Mining-Umsatz für jedes Jahr"}),
    Case("csv-08", "csv_financial", "average", "What were the average quarterly unadjusted sales for Construction?",
         f"""SELECT AVG(b.data_value) AS avg_sales FROM {CSV_T} AS b WHERE b.series_title_2 = 'Construction'
             AND b.series_title_1 = 'Sales (operating income)' AND b.series_title_4 = 'Unadjusted'
             AND b."group" = 'Industry by financial variable (NZSIOC Level 1)'""", _csv_avg_construction),
    Case("csv-09", "csv_financial", "group_count", "How many rows are there for each status code?",
         f"SELECT b.status, COUNT(*) AS n FROM {CSV_T} AS b GROUP BY b.status ORDER BY b.status", _csv_status, ordered=True,
         variants={"es": "¿Cuántas filas hay para cada código de estado?", "hi": "प्रत्येक status कोड के लिए कितनी पंक्तियाँ हैं?"}),
    Case("csv-10", "csv_financial", "percentage",
         "What percentage of unadjusted sales did salaries and wages represent for Education and Training in the March 2026 quarter?",
         f"""SELECT SUM(CASE WHEN b.series_title_1 = 'Salaries and wages' THEN b.data_value END) * 100.0
                  / SUM(CASE WHEN b.series_title_1 = 'Sales (operating income)' THEN b.data_value END) AS salary_pct
             FROM {CSV_T} AS b WHERE b.series_title_2 = 'Education and Training' AND b.series_title_4 = 'Unadjusted'
               AND b."group" = 'Industry by financial variable (NZSIOC Level 1)' AND b.period = 2026.03""", _csv_salary_share,
         variants={"de": "Wie viel Prozent des nicht bereinigten Umsatzes machten Löhne und Gehälter bei Education and Training im Märzquartal 2026 aus?"}),
    Case("csv-11", "csv_financial", "median", "What is the median quarterly unadjusted sales value for Wholesale Trade?",
         f"""WITH v AS (SELECT b.data_value AS val FROM {CSV_T} AS b WHERE b.series_title_2 = 'Wholesale Trade'
                 AND b.series_title_1 = 'Sales (operating income)' AND b.series_title_4 = 'Unadjusted'
                 AND b."group" = 'Industry by financial variable (NZSIOC Level 1)' AND b.data_value IS NOT NULL),
             r AS (SELECT v.val, ROW_NUMBER() OVER (ORDER BY v.val) AS rn, COUNT(*) OVER () AS cnt FROM v)
             SELECT AVG(r.val) AS median_sales FROM r WHERE r.rn IN ((r.cnt + 1) / 2, (r.cnt + 2) / 2)""", _csv_median_wholesale,
         variants={"es": "¿Cuál es la mediana de las ventas trimestrales sin ajustar de Wholesale Trade?",
                   "fr": "Quelle est la médiane des ventes trimestrielles non ajustées de Wholesale Trade ?"}),
    Case("csv-12", "csv_financial", "comparison",
         "How much did unadjusted Construction sales change from the December 2025 quarter to the March 2026 quarter?",
         f"""SELECT SUM(CASE WHEN b.period = 2026.03 THEN b.data_value ELSE 0 END)
                  - SUM(CASE WHEN b.period = 2025.12 THEN b.data_value ELSE 0 END) AS change
             FROM {CSV_T} AS b WHERE b.series_title_2 = 'Construction' AND b.series_title_1 = 'Sales (operating income)'
               AND b.series_title_4 = 'Unadjusted' AND b."group" = 'Industry by financial variable (NZSIOC Level 1)'""", _csv_qoq),
    Case("csv-13", "csv_financial", "extreme",
         "Which top-level industry had the lowest unadjusted operating profit in the March 2026 quarter?",
         f"""SELECT b.series_title_2 AS industry, SUM(b.data_value) AS operating_profit FROM {CSV_T} AS b
             WHERE b.series_title_1 = 'Operating profit' AND b.series_title_4 = 'Unadjusted'
               AND b."group" = 'Industry by financial variable (NZSIOC Level 1)' AND b.period = 2026.03
             GROUP BY b.series_title_2 ORDER BY operating_profit ASC, industry LIMIT 1""", _csv_lowest_profit, ordered=True,
         variants={"fr": "Quelle industrie de premier niveau a eu le bénéfice d'exploitation non ajusté le plus bas au trimestre de mars 2026 ?"}),
    Case("csv-14", "csv_financial", "count", "How many different series are there?",
         f"SELECT COUNT(DISTINCT b.series_reference) AS series FROM {CSV_T} AS b",
         lambda s: _one(s.csv().Series_reference.nunique())),
]

# --------------------------------------------------------------------------------------------- XLSX: project schedule
X_T = "project_management_sample_data"


def _x(src):
    return src.xlsx()


def _x_most(src):
    g = _x(src).groupby("Project Name").size().reset_index(name="n").sort_values(["n", "Project Name"], ascending=[False, True]).head(1)
    return _t(g, ["Project Name", "n"])


def _x_completed_per_project(src):
    d = _x(src)
    g = d.assign(c=(d.Progress >= 1).astype(int)).groupby("Project Name").c.sum().reset_index()
    return _t(g, ["Project Name", "c"])


def _x_avg_progress(src):
    g = _x(src).groupby("Project Name").Progress.mean().mul(100).reset_index().sort_values(["Progress", "Project Name"], ascending=[False, True])
    return _t(g, ["Project Name", "Progress"])


def _x_end_before_start(src):
    d = _x(src)
    d = d[d["End Date"] < d["Start Date"]].sort_values(["Project Name", "Task Name"])
    return [(r["Project Name"], r["Task Name"]) for _, r in d.iterrows()]


def _x_mismatch(src):
    d = _x(src)
    exp_end = d["Start Date"] + pd.to_timedelta(d["Days Required"], unit="D")
    d = d[d["End Date"] != exp_end].sort_values(["Project Name", "Task Name"])
    return [(r["Project Name"], r["Task Name"]) for _, r in d.iterrows()]


def _x_days_per_person(src):
    g = _x(src).groupby("Assigned to")["Days Required"].sum().reset_index()
    return _t(g, ["Assigned to", "Days Required"])


def _x_feb(src):
    d = _x(src)
    return _one(int(((d["Start Date"] >= "2024-02-01") & (d["Start Date"] < "2024-03-01")).sum()))


def _x_marketing(src):
    d = _x(src)
    d = d[d["Project Name"] == "Marketing"].copy()
    d["s"] = d["Start Date"].dt.strftime("%Y-%m-%d")
    d["e"] = d["End Date"].dt.strftime("%Y-%m-%d")
    return _t(d, ["Project Name", "Task Name", "Assigned to", "s", "Days Required", "e", "Progress"])


def _x_low_progress(src):
    g = _x(src).groupby("Project Name").Progress.mean()
    return [(k,) for k in sorted(g[g < 0.2].index)]


def _x_project_end(src):
    g = _x(src).groupby("Project Name")["End Date"].max().dt.strftime("%Y-%m-%d").reset_index()
    return _t(g, ["Project Name", "End Date"])


def _x_tasks_per_person(src):
    g = _x(src).groupby("Assigned to").size().reset_index(name="n")
    return _t(g, ["Assigned to", "n"])


def _x_monthly_starts(src):
    d = _x(src)
    g = d.groupby(d["Start Date"].dt.strftime("%Y-%m")).size().reset_index(name="n").sort_values("Start Date")
    return _t(g, ["Start Date", "n"])


XLSX_CASES = [
    Case("xlsx-01", "xlsx_projects", "frequency", "Which project appears most?",
         f"SELECT p.project_name, COUNT(*) AS task_count FROM {X_T} AS p GROUP BY p.project_name ORDER BY task_count DESC, p.project_name LIMIT 1",
         _x_most, ordered=True,
         variants={"es": "¿Qué proyecto aparece más veces?", "fr": "Quel projet apparaît le plus souvent ?",
                   "de": "Welches Projekt kommt am häufigsten vor?", "hi": "कौन सा प्रोजेक्ट सबसे ज़्यादा बार आता है?",
                   "informal": "most common project?", "typo": "which projcet apears most", "incomplete": "project with the most tasks"}),
    Case("xlsx-02", "xlsx_projects", "count", "How many tasks are there in total?",
         f"SELECT COUNT(*) AS tasks FROM {X_T} AS p", lambda s: _one(len(_x(s))),
         variants={"es": "¿Cuántas tareas hay en total?", "de": "Wie viele Aufgaben gibt es insgesamt?"}),
    Case("xlsx-03", "xlsx_projects", "filter_count", "How many tasks are fully completed?",
         f"SELECT COUNT(*) AS completed FROM {X_T} AS p WHERE p.progress >= 1",
         lambda s: _one(int((_x(s).Progress >= 1).sum())),
         variants={"fr": "Combien de tâches sont entièrement terminées ?", "informal": "how many tasks done?"}),
    Case("xlsx-04", "xlsx_projects", "group_count", "How many tasks per project are completed?",
         f"SELECT p.project_name, SUM(CASE WHEN p.progress >= 1 THEN 1 ELSE 0 END) AS completed FROM {X_T} AS p GROUP BY p.project_name",
         _x_completed_per_project,
         variants={"es": "¿Cuántas tareas por proyecto están completadas?"}),
    Case("xlsx-05", "xlsx_projects", "average", "What is the average progress per project, as a percentage?",
         f"SELECT p.project_name, AVG(p.progress) * 100 AS avg_progress_pct FROM {X_T} AS p GROUP BY p.project_name ORDER BY avg_progress_pct DESC, p.project_name",
         _x_avg_progress, ordered=True,
         variants={"de": "Wie hoch ist der durchschnittliche Fortschritt pro Projekt in Prozent?",
                   "hi": "प्रत्येक प्रोजेक्ट की औसत प्रगति प्रतिशत में क्या है?"}),
    Case("xlsx-06", "xlsx_projects", "anomaly", "Which tasks end before they start?",
         f"SELECT p.project_name, p.task_name FROM {X_T} AS p WHERE p.end_date < p.start_date ORDER BY p.project_name, p.task_name",
         _x_end_before_start, ordered=True,
         variants={"es": "¿Qué tareas terminan antes de empezar?", "informal": "any tasks with end date before start date?"}),
    Case("xlsx-07", "xlsx_projects", "anomaly",
         "Which tasks have an end date that doesn't match the start date plus the days required?",
         f"""SELECT p.project_name, p.task_name FROM {X_T} AS p
             WHERE p.end_date <> date(p.start_date, '+' || p.days_required || ' days') ORDER BY p.project_name, p.task_name""",
         _x_mismatch, ordered=True,
         variants={"fr": "Quelles tâches ont une date de fin qui ne correspond pas à la date de début plus les jours requis ?",
                   "informal": "find schedule anomalies (end date != start + days required)"}),
    Case("xlsx-08", "xlsx_projects", "group_sum", "What is the total number of days required per person?",
         f"SELECT p.assigned_to, SUM(p.days_required) AS total_days FROM {X_T} AS p GROUP BY p.assigned_to",
         _x_days_per_person),
    Case("xlsx-09", "xlsx_projects", "date_filter", "How many tasks start in February 2024?",
         f"SELECT COUNT(*) AS n FROM {X_T} AS p WHERE p.start_date >= '2024-02-01' AND p.start_date < '2024-03-01'", _x_feb,
         variants={"es": "¿Cuántas tareas empiezan en febrero de 2024?", "de": "Wie viele Aufgaben beginnen im Februar 2024?"}),
    Case("xlsx-10", "xlsx_projects", "listing", "Show all tasks for the Marketing project",
         f"SELECT * FROM {X_T} WHERE project_name = 'Marketing'", _x_marketing,
         variants={"fr": "Afficher toutes les tâches du projet Marketing"}),
    Case("xlsx-11", "xlsx_projects", "median", "What is the median number of days required for a task?",
         f"""WITH r AS (SELECT p.days_required AS d, ROW_NUMBER() OVER (ORDER BY p.days_required) AS rn, COUNT(*) OVER () AS cnt FROM {X_T} AS p)
             SELECT AVG(r.d) AS median_days FROM r WHERE r.rn IN ((r.cnt + 1) / 2, (r.cnt + 2) / 2)""",
         lambda s: _one(_median(_x(s)["Days Required"]))),
    Case("xlsx-12", "xlsx_projects", "having", "Which projects have an average progress below 20%?",
         f"SELECT p.project_name FROM {X_T} AS p GROUP BY p.project_name HAVING AVG(p.progress) < 0.2 ORDER BY p.project_name",
         _x_low_progress, ordered=True),
    Case("xlsx-13", "xlsx_projects", "group_max", "When does each project finish?",
         f"SELECT p.project_name, MAX(p.end_date) AS finish_date FROM {X_T} AS p GROUP BY p.project_name", _x_project_end),
    Case("xlsx-14", "xlsx_projects", "group_count", "How many tasks is each person assigned?",
         f"SELECT p.assigned_to, COUNT(*) AS tasks FROM {X_T} AS p GROUP BY p.assigned_to", _x_tasks_per_person,
         variants={"hi": "प्रत्येक व्यक्ति को कितने कार्य सौंपे गए हैं?"}),
    Case("xlsx-15", "xlsx_projects", "count", "How many tasks have not started yet (0% progress)?",
         f"SELECT COUNT(*) AS not_started FROM {X_T} AS p WHERE p.progress = 0",
         lambda s: _one(int((_x(s).Progress == 0).sum()))),
    Case("xlsx-16", "xlsx_projects", "time_series", "How many tasks start in each month?",
         f"SELECT strftime('%Y-%m', p.start_date) AS month, COUNT(*) AS n FROM {X_T} AS p GROUP BY month ORDER BY month",
         _x_monthly_starts, ordered=True,
         variants={"es": "¿Cuántas tareas empiezan cada mes?"}),
]

# --------------------------------------------------------------------------------------------- JSON: European countries
J_T = "european_countries"


def _j(src):
    return src.json_countries()


def _j_top_pop(src):
    d = _j(src).sort_values(["population", "name"], ascending=[False, True]).head(5)
    return _t(d, ["name", "population"])


def _j_density(src):
    d = _j(src).assign(dens=lambda x: x.population / x.area_km2).sort_values(["dens", "name"], ascending=[False, True]).head(3)
    return _t(d, ["name", "dens"])


def _j_by_currency(src):
    g = _j(src).groupby("currency").size().reset_index(name="n")
    return _t(g, ["currency", "n"])


def _j_top_currency(src):
    g = _j(src).groupby("currency").size().reset_index(name="n").sort_values(["n", "currency"], ascending=[False, True]).head(1)
    return _t(g, ["currency", "n"])


JSON_CASES = [
    Case("json-01", "json_countries", "boolean_count", "How many countries are EU members?",
         f"SELECT COUNT(*) AS eu_countries FROM {J_T} AS c WHERE c.eu_member = 1",
         lambda s: _one(int(_j(s).eu_member.sum())),
         variants={"es": "¿Cuántos países son miembros de la UE?", "fr": "Combien de pays sont membres de l'UE ?",
                   "de": "Wie viele Länder sind EU-Mitglieder?", "hi": "कितने देश यूरोपीय संघ के सदस्य हैं?",
                   "informal": "how many are in the EU?", "typo": "how many countires are eu memebers", "incomplete": "EU members count"}),
    Case("json-02", "json_countries", "boolean_count", "How many countries are not in the EU?",
         f"SELECT COUNT(*) AS non_eu FROM {J_T} AS c WHERE c.eu_member = 0",
         lambda s: _one(int((~_j(s).eu_member).sum())),
         variants={"es": "¿Cuántos países no están en la UE?", "de": "Wie viele Länder sind nicht in der EU?"}),
    Case("json-03", "json_countries", "boolean_filter", "How many EU countries use the euro?",
         f"SELECT COUNT(*) AS n FROM {J_T} AS c WHERE c.eu_member = 1 AND c.currency = 'EUR'",
         lambda s: _one(int((_j(s).eu_member & (_j(s).currency == "EUR")).sum())),
         variants={"fr": "Combien de pays de l'UE utilisent l'euro ?"}),
    Case("json-04", "json_countries", "boolean_filter", "Which non-EU countries use the euro?",
         f"SELECT c.name FROM {J_T} AS c WHERE c.eu_member = 0 AND c.currency = 'EUR' ORDER BY c.name",
         lambda s: [(n,) for n in sorted(_j(s)[(~_j(s).eu_member) & (_j(s).currency == "EUR")].name)], ordered=True,
         variants={"es": "¿Qué países fuera de la UE usan el euro?", "de": "Welche Nicht-EU-Länder verwenden den Euro?"}),
    Case("json-05", "json_countries", "median", "What is the median population of EU member countries?",
         f"""WITH r AS (SELECT c.population, ROW_NUMBER() OVER (ORDER BY c.population) AS rn, COUNT(*) OVER () AS cnt
                       FROM {J_T} AS c WHERE c.eu_member = 1)
             SELECT AVG(r.population) AS median_population FROM r WHERE r.rn IN ((r.cnt + 1) / 2, (r.cnt + 2) / 2)""",
         lambda s: _one(_median(_j(s)[_j(s).eu_member].population)),
         variants={"es": "¿Cuál es la mediana de población de los países miembros de la UE?",
                   "fr": "Quelle est la population médiane des pays membres de l'UE ?",
                   "hi": "यूरोपीय संघ के सदस्य देशों की माध्य जनसंख्या क्या है?"}),
    Case("json-06", "json_countries", "ranking", "What are the 5 most populous countries?",
         f"SELECT c.name, c.population FROM {J_T} AS c ORDER BY c.population DESC, c.name LIMIT 5", _j_top_pop, ordered=True,
         variants={"de": "Welches sind die 5 bevölkerungsreichsten Länder?", "informal": "top 5 countries by population"}),
    Case("json-07", "json_countries", "derived_ranking", "Which 3 countries have the highest population density?",
         f"SELECT c.name, c.population * 1.0 / c.area_km2 AS density FROM {J_T} AS c ORDER BY density DESC, c.name LIMIT 3",
         _j_density, ordered=True),
    Case("json-08", "json_countries", "sum", "What is the total population of EU member countries?",
         f"SELECT SUM(c.population) AS total_population FROM {J_T} AS c WHERE c.eu_member = 1",
         lambda s: _one(int(_j(s)[_j(s).eu_member].population.sum())),
         variants={"es": "¿Cuál es la población total de los países miembros de la UE?"}),
    Case("json-09", "json_countries", "average", "What is the average area of countries outside the EU?",
         f"SELECT AVG(c.area_km2) AS avg_area FROM {J_T} AS c WHERE c.eu_member = 0",
         lambda s: _one(float(_j(s)[~_j(s).eu_member].area_km2.mean()))),
    Case("json-10", "json_countries", "frequency", "Which currency is used by the most countries?",
         f"SELECT c.currency, COUNT(*) AS countries FROM {J_T} AS c GROUP BY c.currency ORDER BY countries DESC, c.currency LIMIT 1",
         _j_top_currency, ordered=True,
         variants={"fr": "Quelle monnaie est utilisée par le plus grand nombre de pays ?", "informal": "most common currency?"}),
    Case("json-11", "json_countries", "lookup", "What is the capital of Germany?",
         f"SELECT c.capital FROM {J_T} AS c WHERE c.name = 'Germany'",
         lambda s: [(v,) for v in _j(s)[_j(s).name == "Germany"].capital],
         variants={"es": "¿Cuál es la capital de Germany?", "de": "Was ist die Hauptstadt von Germany?"}),
    Case("json-12", "json_countries", "threshold", "Which countries have a population over 50 million?",
         f"SELECT c.name FROM {J_T} AS c WHERE c.population > 50000000 ORDER BY c.name",
         lambda s: [(n,) for n in sorted(_j(s)[_j(s).population > 50_000_000].name)], ordered=True),
    Case("json-13", "json_countries", "extreme", "Which country is the smallest by area?",
         f"SELECT c.name, c.area_km2 FROM {J_T} AS c ORDER BY c.area_km2 ASC, c.name LIMIT 1",
         lambda s: _t(_j(s).sort_values(["area_km2", "name"]).head(1), ["name", "area_km2"]), ordered=True),
    Case("json-14", "json_countries", "group_count", "How many countries use each currency?",
         f"SELECT c.currency, COUNT(*) AS n FROM {J_T} AS c GROUP BY c.currency", _j_by_currency),
    Case("json-15", "json_countries", "percentage", "What percentage of the countries are EU members?",
         f"SELECT SUM(CASE WHEN c.eu_member = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS eu_pct FROM {J_T} AS c",
         lambda s: _one(float(_j(s).eu_member.mean() * 100.0)),
         variants={"de": "Wie viel Prozent der Länder sind EU-Mitglieder?"}),
    Case("json-16", "json_countries", "lookup", "Is Iceland in the EU?",
         f"SELECT c.name, c.eu_member FROM {J_T} AS c WHERE c.name = 'Iceland'",
         lambda s: [(r.name, int(r.eu_member)) for r in _j(s)[_j(s).name == "Iceland"].itertuples()],
         variants={"es": "¿Está Iceland en la UE?", "informal": "is iceland an eu member?"}),
]

# --------------------------------------------------------------------------------------------- SQLite: Northwind
NW = "sqlite_northwind"


def _nw(src, t):
    return src.table(NW, t)


def _nw_rev():
    def f(src):
        od, p, c = _nw(src, "OrderDetail"), _nw(src, "Product"), _nw(src, "Category")
        m = od.merge(p, left_on="ProductId", right_on="Id", suffixes=("", "_p")).merge(c, left_on="CategoryId", right_on="Id", suffixes=("", "_c"))
        m["rev"] = m.UnitPrice * m.Quantity * (1 - m.Discount)
        g = m.groupby("CategoryName").rev.sum().reset_index().sort_values(["rev", "CategoryName"], ascending=[False, True])
        return _t(g, ["CategoryName", "rev"])
    return f


def _nw_top_customers(src):
    o, c = _nw(src, "Order"), _nw(src, "Customer")
    m = o.merge(c, left_on="CustomerId", right_on="Id", suffixes=("", "_c"))
    g = m.groupby("CompanyName").size().reset_index(name="n").sort_values(["n", "CompanyName"], ascending=[False, True]).head(5)
    return _t(g, ["CompanyName", "n"])


def _nw_by_shipper(src):
    o, s = _nw(src, "Order"), _nw(src, "Shipper")
    m = o.merge(s, left_on="ShipVia", right_on="Id", suffixes=("", "_s"))
    g = m.groupby("CompanyName").size().reset_index(name="n")
    return _t(g, ["CompanyName", "n"])


def _nw_emp_most(src):
    o, e = _nw(src, "Order"), _nw(src, "Employee")
    m = o.merge(e, left_on="EmployeeId", right_on="Id", suffixes=("", "_e"))
    m["name"] = m.FirstName + " " + m.LastName
    g = m.groupby("name").size().reset_index(name="n").sort_values(["n", "name"], ascending=[False, True]).head(1)
    return _t(g, ["name", "n"])


def _nw_monthly_2013(src):
    o = _nw(src, "Order")
    o = o[o.OrderDate.str[:4] == "2013"]
    g = o.groupby(o.OrderDate.str[5:7]).size().reset_index(name="n").sort_values("OrderDate")
    return _t(g, ["OrderDate", "n"])


def _nw_top_qty(src):
    od, p = _nw(src, "OrderDetail"), _nw(src, "Product")
    m = od.merge(p, left_on="ProductId", right_on="Id", suffixes=("", "_p"))
    g = m.groupby("ProductName").Quantity.sum().reset_index().sort_values(["Quantity", "ProductName"], ascending=[False, True]).head(3)
    return _t(g, ["ProductName", "Quantity"])


def _nw_freight_country(src):
    o = _nw(src, "Order")
    g = o.groupby("ShipCountry").Freight.mean().reset_index().sort_values(["Freight", "ShipCountry"], ascending=[False, True]).head(5)
    return _t(g, ["ShipCountry", "Freight"])


def _nw_rev_2013(src):
    od, o = _nw(src, "OrderDetail"), _nw(src, "Order")
    m = od.merge(o, left_on="OrderId", right_on="Id", suffixes=("", "_o"))
    m = m[m.OrderDate.str[:4] == "2013"]
    return _one(float((m.UnitPrice * m.Quantity * (1 - m.Discount)).sum()))


def _nw_supplier_most(src):
    p, s = _nw(src, "Product"), _nw(src, "Supplier")
    m = p.merge(s, left_on="SupplierId", right_on="Id", suffixes=("", "_s"))
    g = m.groupby("CompanyName").size().reset_index(name="n").sort_values(["n", "CompanyName"], ascending=[False, True]).head(1)
    return _t(g, ["CompanyName", "n"])


def _nw_avg_price_cat(src):
    p, c = _nw(src, "Product"), _nw(src, "Category")
    m = p.merge(c, left_on="CategoryId", right_on="Id", suffixes=("", "_c"))
    g = m.groupby("CategoryName").UnitPrice.mean().reset_index()
    return _t(g, ["CategoryName", "UnitPrice"])


NW_CASES = [
    Case("nw-01", NW, "join_ranking", "Who are the top 5 customers by number of orders?",
         """SELECT c.CompanyName, COUNT(o.Id) AS orders FROM "Order" AS o JOIN Customer AS c ON o.CustomerId = c.Id
            GROUP BY c.CompanyName ORDER BY orders DESC, c.CompanyName LIMIT 5""", _nw_top_customers, ordered=True,
         variants={"es": "¿Quiénes son los 5 principales clientes por número de pedidos?",
                   "fr": "Quels sont les 5 meilleurs clients par nombre de commandes ?",
                   "de": "Wer sind die Top 5 Kunden nach Anzahl der Bestellungen?",
                   "hi": "ऑर्डर की संख्या के अनुसार शीर्ष 5 ग्राहक कौन हैं?",
                   "typo": "top 5 custmers by number of oders"}),
    Case("nw-02", NW, "join_sum", "What is the revenue by category (after discounts)?",
         """SELECT cat.CategoryName, SUM(od.UnitPrice * od.Quantity * (1 - od.Discount)) AS revenue FROM OrderDetail AS od
            JOIN Product AS p ON od.ProductId = p.Id JOIN Category AS cat ON p.CategoryId = cat.Id
            GROUP BY cat.CategoryName ORDER BY revenue DESC, cat.CategoryName""", _nw_rev(), ordered=True,
         variants={"es": "¿Cuáles son los ingresos por categoría (después de descuentos)?", "informal": "revenue per category net of discount"}),
    Case("nw-03", NW, "date_filter", "How many orders were shipped in 2013?",
         """SELECT COUNT(*) AS n FROM "Order" AS o WHERE o.ShippedDate >= '2013-01-01' AND o.ShippedDate < '2014-01-01'""",
         lambda s: _one(int((_nw(s, "Order").ShippedDate.fillna("").str[:4] == "2013").sum())),
         variants={"de": "Wie viele Bestellungen wurden 2013 versandt?", "fr": "Combien de commandes ont été expédiées en 2013 ?"}),
    Case("nw-04", NW, "join_count", "How many orders did each shipper handle?",
         """SELECT s.CompanyName, COUNT(*) AS orders FROM "Order" AS o JOIN Shipper AS s ON o.ShipVia = s.Id GROUP BY s.CompanyName""",
         _nw_by_shipper, variants={"es": "¿Cuántos pedidos gestionó cada transportista?"}),
    Case("nw-05", NW, "average_ranking", "Which 5 ship countries have the highest average freight?",
         """SELECT o.ShipCountry, AVG(o.Freight) AS avg_freight FROM "Order" AS o GROUP BY o.ShipCountry ORDER BY avg_freight DESC, o.ShipCountry LIMIT 5""",
         _nw_freight_country, ordered=True),
    Case("nw-06", NW, "filter_count", "How many products are discontinued?",
         "SELECT COUNT(*) AS discontinued FROM Product AS p WHERE p.Discontinued = 1",
         lambda s: _one(int((_nw(s, "Product").Discontinued == 1).sum())),
         variants={"fr": "Combien de produits sont arrêtés ?", "informal": "discontinued products count"}),
    Case("nw-07", NW, "join_extreme", "Which employee handled the most orders?",
         """SELECT e.FirstName || ' ' || e.LastName AS employee, COUNT(*) AS orders FROM "Order" AS o JOIN Employee AS e ON o.EmployeeId = e.Id
            GROUP BY e.Id ORDER BY orders DESC, employee LIMIT 1""", _nw_emp_most, ordered=True,
         variants={"es": "¿Qué empleado gestionó más pedidos?", "de": "Welcher Mitarbeiter hat die meisten Bestellungen bearbeitet?"}),
    Case("nw-08", NW, "time_series", "How many orders were placed in each month of 2013?",
         """SELECT strftime('%m', o.OrderDate) AS month, COUNT(*) AS orders FROM "Order" AS o
            WHERE o.OrderDate >= '2013-01-01' AND o.OrderDate < '2014-01-01' GROUP BY month ORDER BY month""", _nw_monthly_2013, ordered=True,
         variants={"es": "¿Cuántos pedidos se hicieron en cada mes de 2013?", "hi": "2013 के प्रत्येक महीने में कितने ऑर्डर दिए गए?"}),
    Case("nw-09", NW, "join_ranking", "What are the top 3 products by quantity sold?",
         """SELECT p.ProductName, SUM(od.Quantity) AS qty FROM OrderDetail AS od JOIN Product AS p ON od.ProductId = p.Id
            GROUP BY p.ProductName ORDER BY qty DESC, p.ProductName LIMIT 3""", _nw_top_qty, ordered=True),
    Case("nw-10", NW, "column_compare", "How many products have fewer units in stock than their reorder level?",
         "SELECT COUNT(*) AS n FROM Product AS p WHERE p.UnitsInStock < p.ReorderLevel",
         lambda s: _one(int((_nw(s, "Product").UnitsInStock < _nw(s, "Product").ReorderLevel).sum()))),
    Case("nw-11", NW, "median", "What is the median freight cost of an order?",
         """WITH r AS (SELECT o.Freight AS f, ROW_NUMBER() OVER (ORDER BY o.Freight) AS rn, COUNT(*) OVER () AS cnt FROM "Order" AS o)
            SELECT AVG(r.f) AS median_freight FROM r WHERE r.rn IN ((r.cnt + 1) / 2, (r.cnt + 2) / 2)""",
         lambda s: _one(_median(_nw(s, "Order").Freight)),
         variants={"de": "Wie hoch sind die mittleren Frachtkosten (Median) einer Bestellung?"}),
    Case("nw-12", NW, "join_avg", "What is the average unit price per category?",
         """SELECT c.CategoryName, AVG(p.UnitPrice) AS avg_price FROM Product AS p JOIN Category AS c ON p.CategoryId = c.Id GROUP BY c.CategoryName""",
         _nw_avg_price_cat),
    Case("nw-13", NW, "join_date_sum", "What was the total revenue in 2013?",
         """SELECT SUM(od.UnitPrice * od.Quantity * (1 - od.Discount)) AS revenue FROM OrderDetail AS od JOIN "Order" AS o ON od.OrderId = o.Id
            WHERE o.OrderDate >= '2013-01-01' AND o.OrderDate < '2014-01-01'""", _nw_rev_2013,
         variants={"es": "¿Cuáles fueron los ingresos totales en 2013?", "fr": "Quel a été le chiffre d'affaires total en 2013 ?"}),
    Case("nw-14", NW, "nulls", "How many orders have not been shipped?",
         """SELECT COUNT(*) AS unshipped FROM "Order" AS o WHERE o.ShippedDate IS NULL""",
         lambda s: _one(int(_nw(s, "Order").ShippedDate.isna().sum()))),
    Case("nw-15", NW, "join_extreme", "Which supplier provides the most products?",
         """SELECT s.CompanyName, COUNT(*) AS products FROM Product AS p JOIN Supplier AS s ON p.SupplierId = s.Id
            GROUP BY s.CompanyName ORDER BY products DESC, s.CompanyName LIMIT 1""", _nw_supplier_most, ordered=True),
]

# --------------------------------------------------------------------------------------------- SQLite (.db): Chinook
CH = "db_chinook"


def _ch(src, t):
    return src.table(CH, t)


def _ch_genre_rev(src):
    ii, t, g = _ch(src, "invoice_items"), _ch(src, "tracks"), _ch(src, "genres")
    m = ii.merge(t, on="TrackId", suffixes=("", "_t")).merge(g, on="GenreId", suffixes=("", "_g"))
    m["rev"] = m.UnitPrice * m.Quantity
    r = m.groupby("Name_g").rev.sum().reset_index().sort_values(["rev", "Name_g"], ascending=[False, True]).head(5)
    return _t(r, ["Name_g", "rev"])


def _ch_country_most(src):
    c = _ch(src, "customers")
    g = c.groupby("Country").size().reset_index(name="n").sort_values(["n", "Country"], ascending=[False, True]).head(1)
    return _t(g, ["Country", "n"])


def _ch_monthly_2012(src):
    i = _ch(src, "invoices")
    i = i[i.InvoiceDate.str[:4] == "2012"]
    g = i.groupby(i.InvoiceDate.str[5:7]).Total.sum().reset_index().sort_values("InvoiceDate")
    return _t(g, ["InvoiceDate", "Total"])


def _ch_median_len(src):
    t, g = _ch(src, "tracks"), _ch(src, "genres")
    m = t.merge(g, on="GenreId", suffixes=("", "_g"))
    r = m.groupby("Name_g").Milliseconds.median().div(60000.0).reset_index().sort_values("Name_g")
    return _t(r, ["Name_g", "Milliseconds"])


def _ch_rev_year(src):
    i = _ch(src, "invoices")
    g = i.groupby(i.InvoiceDate.str[:4]).Total.sum().reset_index().sort_values("InvoiceDate")
    return _t(g, ["InvoiceDate", "Total"])


def _ch_rep(src):
    c, e = _ch(src, "customers"), _ch(src, "employees")
    m = c.merge(e, left_on="SupportRepId", right_on="EmployeeId", suffixes=("", "_e"))
    m["rep"] = m.FirstName_e + " " + m.LastName_e
    g = m.groupby("rep").size().reset_index(name="n")
    return _t(g, ["rep", "n"])


def _ch_media(src):
    t, mt = _ch(src, "tracks"), _ch(src, "media_types")
    m = t.merge(mt, on="MediaTypeId", suffixes=("", "_m"))
    g = m.groupby("Name_m").size().reset_index(name="n")
    return _t(g, ["Name_m", "n"])


def _ch_top_customers(src):
    i, c = _ch(src, "invoices"), _ch(src, "customers")
    m = i.merge(c, on="CustomerId")
    m["name"] = m.FirstName + " " + m.LastName
    g = m.groupby(["CustomerId", "name"]).Total.sum().reset_index().sort_values(["Total", "name"], ascending=[False, True]).head(3)
    return _t(g, ["name", "Total"])


def _ch_never_bought(src):
    t, ii = _ch(src, "tracks"), _ch(src, "invoice_items")
    return _one(int((~t.TrackId.isin(ii.TrackId)).sum()))


def _ch_germany(src):
    i = _ch(src, "invoices")
    c = _ch(src, "customers")
    m = i.merge(c, on="CustomerId")
    return _one(float(m[m.Country == "Germany"].Total.sum()))


def _ch_playlist_most(src):
    pt, p = _ch(src, "playlist_track"), _ch(src, "playlists")
    m = pt.merge(p, on="PlaylistId")
    g = m.groupby(["PlaylistId", "Name"]).size().reset_index(name="n").sort_values(["n", "Name"], ascending=[False, True]).head(1)
    return _t(g, ["Name", "n"])


def _ch_avg_invoice_country(src):
    i = _ch(src, "invoices")
    g = i.groupby("BillingCountry").Total.mean().reset_index().sort_values(["Total", "BillingCountry"], ascending=[False, True]).head(5)
    return _t(g, ["BillingCountry", "Total"])


CH_CASES = [
    Case("ch-01", CH, "join_ranking", "What are the top 5 genres by revenue?",
         """SELECT g.Name AS genre, SUM(ii.UnitPrice * ii.Quantity) AS revenue FROM invoice_items AS ii
            JOIN tracks AS t ON ii.TrackId = t.TrackId JOIN genres AS g ON t.GenreId = g.GenreId
            GROUP BY g.Name ORDER BY revenue DESC, genre LIMIT 5""", _ch_genre_rev, ordered=True,
         variants={"es": "¿Cuáles son los 5 géneros con más ingresos?", "fr": "Quels sont les 5 genres qui génèrent le plus de revenus ?",
                   "de": "Was sind die Top 5 Genres nach Umsatz?", "hi": "राजस्व के अनुसार शीर्ष 5 शैलियाँ कौन सी हैं?",
                   "typo": "top 5 genres by revenu"}),
    Case("ch-02", CH, "frequency", "Which country has the most customers?",
         "SELECT c.Country, COUNT(*) AS customers FROM customers AS c GROUP BY c.Country ORDER BY customers DESC, c.Country LIMIT 1",
         _ch_country_most, ordered=True,
         variants={"es": "¿Qué país tiene más clientes?", "fr": "Quel pays compte le plus de clients ?", "informal": "country w/ most customers"}),
    Case("ch-03", CH, "time_series", "What was the monthly revenue in 2012?",
         """SELECT strftime('%m', i.InvoiceDate) AS month, SUM(i.Total) AS revenue FROM invoices AS i
            WHERE i.InvoiceDate >= '2012-01-01' AND i.InvoiceDate < '2013-01-01' GROUP BY month ORDER BY month""",
         _ch_monthly_2012, ordered=True,
         variants={"de": "Wie hoch war der monatliche Umsatz im Jahr 2012?", "es": "¿Cuáles fueron los ingresos mensuales en 2012?"}),
    Case("ch-04", CH, "median", "What is the median track length in minutes for each genre?",
         """WITH r AS (SELECT t.GenreId, t.Milliseconds, ROW_NUMBER() OVER (PARTITION BY t.GenreId ORDER BY t.Milliseconds) AS rn,
                              COUNT(*) OVER (PARTITION BY t.GenreId) AS cnt FROM tracks AS t)
            SELECT g.Name, AVG(r.Milliseconds) / 60000.0 AS median_minutes FROM r JOIN genres AS g ON r.GenreId = g.GenreId
            WHERE r.rn IN ((r.cnt + 1) / 2, (r.cnt + 2) / 2) GROUP BY g.Name ORDER BY g.Name""", _ch_median_len, ordered=True,
         variants={"fr": "Quelle est la durée médiane des morceaux en minutes pour chaque genre ?"}),
    Case("ch-05", CH, "time_series", "What was the total revenue per year?",
         "SELECT strftime('%Y', i.InvoiceDate) AS year, SUM(i.Total) AS revenue FROM invoices AS i GROUP BY year ORDER BY year",
         _ch_rev_year, ordered=True, variants={"hi": "हर साल का कुल राजस्व कितना था?"}),
    Case("ch-06", CH, "average_ranking", "Which 5 billing countries have the highest average invoice total?",
         """SELECT i.BillingCountry, AVG(i.Total) AS avg_total FROM invoices AS i GROUP BY i.BillingCountry
            ORDER BY avg_total DESC, i.BillingCountry LIMIT 5""", _ch_avg_invoice_country, ordered=True),
    Case("ch-07", CH, "join_count", "How many customers does each support rep look after?",
         """SELECT e.FirstName || ' ' || e.LastName AS rep, COUNT(*) AS customers FROM customers AS c
            JOIN employees AS e ON c.SupportRepId = e.EmployeeId GROUP BY e.EmployeeId""", _ch_rep,
         variants={"es": "¿Cuántos clientes atiende cada representante de soporte?"}),
    Case("ch-08", CH, "join_count", "How many tracks are there for each media type?",
         "SELECT m.Name, COUNT(*) AS tracks FROM tracks AS t JOIN media_types AS m ON t.MediaTypeId = m.MediaTypeId GROUP BY m.Name",
         _ch_media),
    Case("ch-09", CH, "join_ranking", "Who are the top 3 customers by total spend?",
         """SELECT c.FirstName || ' ' || c.LastName AS customer, SUM(i.Total) AS spend FROM invoices AS i
            JOIN customers AS c ON i.CustomerId = c.CustomerId GROUP BY c.CustomerId ORDER BY spend DESC, customer LIMIT 3""",
         _ch_top_customers, ordered=True,
         variants={"de": "Wer sind die Top 3 Kunden nach Gesamtausgaben?", "typo": "top 3 custmers by total spend"}),
    Case("ch-10", CH, "anti_join", "How many tracks have never been purchased?",
         "SELECT COUNT(*) AS never_sold FROM tracks AS t WHERE t.TrackId NOT IN (SELECT ii.TrackId FROM invoice_items AS ii)",
         _ch_never_bought, variants={"fr": "Combien de morceaux n'ont jamais été achetés ?"}),
    Case("ch-11", CH, "join_filter_sum", "What is the total revenue from customers in Germany?",
         """SELECT SUM(i.Total) AS revenue FROM invoices AS i JOIN customers AS c ON i.CustomerId = c.CustomerId WHERE c.Country = 'Germany'""",
         _ch_germany, variants={"es": "¿Cuáles son los ingresos totales de los clientes en Germany?"}),
    Case("ch-12", CH, "join_extreme", "Which playlist has the most tracks?",
         """SELECT p.Name, COUNT(*) AS tracks FROM playlist_track AS pt JOIN playlists AS p ON pt.PlaylistId = p.PlaylistId
            GROUP BY p.PlaylistId ORDER BY tracks DESC, p.Name LIMIT 1""", _ch_playlist_most, ordered=True),
]

ALL_CASES: List[Case] = CSV_CASES + XLSX_CASES + JSON_CASES + NW_CASES + CH_CASES
