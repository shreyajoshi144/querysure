"""Held-out cases, written AFTER the pipeline fixes and never used to tune them (see docs/03_EVALUATION.md)."""
from __future__ import annotations

from .cases import CH, CSV_T, J_T, NW, X_T, Case, _ch, _j, _l1, _nw, _one, _t, _x


def _h_csv_salaries_top3(src):
    d = _l1(src)
    d = d[(d.Series_title_1 == "Salaries and wages") & (d.Period == "2026.03")].groupby("Series_title_2").Data_value.sum()
    d = d.reset_index().sort_values(["Data_value", "Series_title_2"], ascending=[False, True]).head(3)
    return _t(d, ["Series_title_2", "Data_value"])


def _h_csv_quarters_2025(src):
    d = _l1(src)
    d = d[(d.Series_title_2 == "Retail Trade and Accommodation") & (d.Series_title_1 == "Sales (operating income)") & (d.Period.str[:4] == "2025")]
    g = d.groupby("Period").Data_value.sum().reset_index().sort_values("Period")
    return [(float(p), v) for p, v in zip(g.Period, g.Data_value)]


def _h_x_person_most_days(src):
    g = _x(src).groupby("Assigned to")["Days Required"].sum().reset_index().sort_values(["Days Required", "Assigned to"], ascending=[False, True]).head(1)
    return _t(g, ["Assigned to", "Days Required"])


def _h_x_overdue_eng(src):
    d = _x(src)
    d = d[(d["Project Name"] == "Engineering") & (d.Progress < 1)].sort_values("Task Name")
    return [(t,) for t in d["Task Name"]]


def _h_x_avg_days_project(src):
    g = _x(src).groupby("Project Name")["Days Required"].mean().reset_index()
    return _t(g, ["Project Name", "Days Required"])


def _h_j_biggest_eu(src):
    d = _j(src)
    d = d[d.eu_member].sort_values(["area_km2", "name"], ascending=[False, True]).head(1)
    return _t(d, ["name", "area_km2"])


def _h_j_currency_pop(src):
    g = _j(src).groupby("currency").population.sum().reset_index().sort_values(["population", "currency"], ascending=[False, True]).head(3)
    return _t(g, ["currency", "population"])


def _h_nw_country_orders(src):
    o = _nw(src, "Order")
    g = o.groupby("ShipCountry").size().reset_index(name="n").sort_values(["n", "ShipCountry"], ascending=[False, True]).head(3)
    return _t(g, ["ShipCountry", "n"])


def _h_nw_cat_products(src):
    p, c = _nw(src, "Product"), _nw(src, "Category")
    m = p.merge(c, left_on="CategoryId", right_on="Id", suffixes=("", "_c"))
    g = m.groupby("CategoryName").size().reset_index(name="n")
    return _t(g, ["CategoryName", "n"])


def _h_nw_late(src):
    o = _nw(src, "Order")
    o = o[o.ShippedDate.notna()]
    return _one(int((o.ShippedDate > o.RequiredDate).sum()))


def _h_ch_artist_albums(src):
    a, ar = _ch(src, "albums"), _ch(src, "artists")
    m = a.merge(ar, on="ArtistId")
    g = m.groupby(["ArtistId", "Name"]).size().reset_index(name="n").sort_values(["n", "Name"], ascending=[False, True]).head(3)
    return _t(g, ["Name", "n"])


def _h_ch_q4_2011(src):
    i = _ch(src, "invoices")
    i = i[(i.InvoiceDate >= "2011-10-01") & (i.InvoiceDate < "2012-01-01")]
    return _one(float(i.Total.sum()))


def _h_ch_avg_track_rock(src):
    t, g = _ch(src, "tracks"), _ch(src, "genres")
    m = t.merge(g, on="GenreId", suffixes=("", "_g"))
    return _one(float(m[m.Name_g == "Rock"].Milliseconds.mean() / 1000.0))


HOLDOUT_CASES = [
    Case("h-csv-01", "csv_financial", "ranking", "Name the three top-level industries that paid the most in salaries and wages in the March 2026 quarter (unadjusted).",
         f"""SELECT b.series_title_2, SUM(b.data_value) AS wages FROM {CSV_T} AS b WHERE b.series_title_1 = 'Salaries and wages'
             AND b.series_title_4 = 'Unadjusted' AND b."group" = 'Industry by financial variable (NZSIOC Level 1)' AND b.period = 2026.03
             GROUP BY b.series_title_2 ORDER BY wages DESC, b.series_title_2 LIMIT 3""", _h_csv_salaries_top3, ordered=True,
         variants={"es": "Nombra las tres industrias de nivel superior que más pagaron en salarios en el trimestre de marzo de 2026 (sin ajustar).",
                   "informal": "3 biggest payers of wages, mar 2026 qtr, unadjusted top level"}),
    Case("h-csv-02", "csv_financial", "time_series", "Quarterly unadjusted sales for Retail Trade and Accommodation during 2025",
         f"""SELECT b.period, SUM(b.data_value) AS sales FROM {CSV_T} AS b WHERE b.series_title_2 = 'Retail Trade and Accommodation'
             AND b.series_title_1 = 'Sales (operating income)' AND b.series_title_4 = 'Unadjusted'
             AND b."group" = 'Industry by financial variable (NZSIOC Level 1)' AND b.period >= 2025 AND b.period < 2026
             GROUP BY b.period ORDER BY b.period""", _h_csv_quarters_2025, ordered=True,
         variants={"fr": "Ventes trimestrielles non ajustées de Retail Trade and Accommodation en 2025"}),
    Case("h-x-01", "xlsx_projects", "ranking", "Who has the biggest workload in days?",
         f"SELECT p.assigned_to, SUM(p.days_required) AS days FROM {X_T} AS p GROUP BY p.assigned_to ORDER BY days DESC, p.assigned_to LIMIT 1",
         _h_x_person_most_days, ordered=True,
         variants={"de": "Wer hat die größte Arbeitslast in Tagen?", "typo": "who has the bigest workload in days"}),
    Case("h-x-02", "xlsx_projects", "filter", "List the unfinished Engineering tasks",
         f"SELECT p.task_name FROM {X_T} AS p WHERE p.project_name = 'Engineering' AND p.progress < 1 ORDER BY p.task_name",
         _h_x_overdue_eng, ordered=True,
         variants={"es": "Enumera las tareas de Engineering sin terminar", "incomplete": "engineering tasks not finished"}),
    Case("h-x-03", "xlsx_projects", "average", "Average days required for each project",
         f"SELECT p.project_name, AVG(p.days_required) AS avg_days FROM {X_T} AS p GROUP BY p.project_name", _h_x_avg_days_project,
         variants={"hi": "प्रत्येक प्रोजेक्ट के लिए औसत आवश्यक दिन"}),
    Case("h-j-01", "json_countries", "extreme", "Which EU member has the largest area?",
         f"SELECT c.name, c.area_km2 FROM {J_T} AS c WHERE c.eu_member = 1 ORDER BY c.area_km2 DESC, c.name LIMIT 1", _h_j_biggest_eu, ordered=True,
         variants={"fr": "Quel membre de l'UE a la plus grande superficie ?", "es": "¿Qué miembro de la UE tiene la mayor superficie?"}),
    Case("h-j-02", "json_countries", "group_ranking", "Which three currencies cover the largest total population?",
         f"SELECT c.currency, SUM(c.population) AS pop FROM {J_T} AS c GROUP BY c.currency ORDER BY pop DESC, c.currency LIMIT 3",
         _h_j_currency_pop, ordered=True),
    Case("h-j-03", "json_countries", "lookup", "Does Norway use the euro?",
         f"SELECT c.name, c.currency FROM {J_T} AS c WHERE c.name = 'Norway'",
         lambda s: _t(_j(s)[_j(s).name == "Norway"], ["name", "currency"]),
         variants={"de": "Verwendet Norway den Euro?"}),
    Case("h-nw-01", NW, "ranking", "Which 3 countries received the most orders?",
         """SELECT o.ShipCountry, COUNT(*) AS orders FROM "Order" AS o GROUP BY o.ShipCountry ORDER BY orders DESC, o.ShipCountry LIMIT 3""",
         _h_nw_country_orders, ordered=True,
         variants={"es": "¿Qué 3 países recibieron más pedidos?", "fr": "Quels 3 pays ont reçu le plus de commandes ?"}),
    Case("h-nw-02", NW, "join_count", "Number of products in every category",
         "SELECT c.CategoryName, COUNT(*) AS products FROM Product AS p JOIN Category AS c ON p.CategoryId = c.Id GROUP BY c.CategoryName",
         _h_nw_cat_products),
    Case("h-nw-03", NW, "column_compare", "How many orders shipped after their required date?",
         """SELECT COUNT(*) AS late FROM "Order" AS o WHERE o.ShippedDate IS NOT NULL AND o.ShippedDate > o.RequiredDate""", _h_nw_late,
         variants={"de": "Wie viele Bestellungen wurden nach dem geforderten Datum versandt?"}),
    Case("h-ch-01", CH, "join_ranking", "Which 3 artists have released the most albums?",
         """SELECT ar.Name, COUNT(*) AS albums FROM albums AS a JOIN artists AS ar ON a.ArtistId = ar.ArtistId
            GROUP BY ar.ArtistId ORDER BY albums DESC, ar.Name LIMIT 3""", _h_ch_artist_albums, ordered=True,
         variants={"es": "¿Qué 3 artistas han publicado más álbumes?"}),
    Case("h-ch-02", CH, "date_sum", "Total sales in Q4 2011",
         "SELECT SUM(i.Total) AS sales FROM invoices AS i WHERE i.InvoiceDate >= '2011-10-01' AND i.InvoiceDate < '2012-01-01'",
         _h_ch_q4_2011, variants={"fr": "Ventes totales au T4 2011", "informal": "sales q4 2011?"}),
    Case("h-ch-03", CH, "filter_avg", "Average length in seconds of Rock tracks",
         """SELECT AVG(t.Milliseconds) / 1000.0 AS avg_seconds FROM tracks AS t JOIN genres AS g ON t.GenreId = g.GenreId WHERE g.Name = 'Rock'""",
         _h_ch_avg_track_rock, variants={"de": "Durchschnittliche Länge von Rock-Titeln in Sekunden"}),
]
