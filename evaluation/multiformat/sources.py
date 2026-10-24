"""Raw-source readers used to compute EXPECTED results. Deliberately independent of modules/ingestion.py."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Dict, Optional

import pandas as pd

# dataset key -> (file name, upload extension)
DATASETS: Dict[str, tuple] = {
    "csv_financial": ("business-financial-data-march-2026-quarter.csv", ".csv"),
    "xlsx_projects": ("Project-Management-Sample-Data.xlsx", ".xlsx"),
    "json_countries": ("european-countries.json", ".json"),
    "sqlite_northwind": ("northwind_small.sqlite", ".sqlite"),
    "db_chinook": ("chinook.db", ".db"),
}

DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                "tests", "fixtures", "user")


@dataclass
class Sources:
    data_dir: str

    def path(self, key: str) -> str:
        return os.path.join(self.data_dir, DATASETS[key][0])

    def available(self, key: str) -> bool:
        return os.path.exists(self.path(key))

    def csv(self) -> pd.DataFrame:
        return pd.read_csv(self.path("csv_financial"), dtype={"Period": str})

    def xlsx(self) -> pd.DataFrame:
        raw = pd.read_excel(self.path("xlsx_projects"), header=None)
        hdr = next(i for i, r in raw.iterrows() if r.notna().sum() >= 5)
        df = raw.iloc[hdr + 1:].copy()
        df.columns = list(raw.iloc[hdr])
        df = df.loc[:, df.columns.notna()].dropna(how="all").reset_index(drop=True)
        for c in ("Start Date", "End Date"):
            df[c] = pd.to_datetime(df[c])
        df["Days Required"] = df["Days Required"].astype(int)
        df["Progress"] = df["Progress"].astype(float)
        return df

    def json_countries(self) -> pd.DataFrame:
        with open(self.path("json_countries"), encoding="utf-8") as fh:
            return pd.DataFrame(json.load(fh)["countries"])

    def table(self, key: str, name: str) -> pd.DataFrame:
        con = sqlite3.connect(f"file:{self.path(key)}?mode=ro", uri=True)
        try:
            return pd.read_sql_query(f'SELECT * FROM "{name}"', con)
        finally:
            con.close()


def resolve_data_dir(data_dir: Optional[str]) -> str:
    return data_dir or os.environ.get("QUERYSURE_EVAL_DATA") or DEFAULT_DATA_DIR
