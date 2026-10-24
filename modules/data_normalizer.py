"""Value-level normalization for CSV and Excel uploads.

Why this exists (root causes):
  * Excel date cells were written to SQLite as 'YYYY-MM-DD 00:00:00', so an
    inclusive range such as BETWEEN '2023-01-01' AND '2023-01-31' silently
    dropped every row dated 2023-01-31.
  * CSV numbers stored as text ("$1,234.50", "(12.00)", "12.5%") were kept as
    TEXT, so SUM/AVG/ORDER BY/comparisons were lexical or returned 0.
  * Date text in CSV in mixed layouts was never made sortable/filterable.

Rules are deliberately conservative: a column is converted only when (almost)
every non-empty value matches, and ambiguous day/month layouts are left alone.
JSON ingestion does not use this module.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

import pandas as pd

_MIN_MATCH = 0.98
_NUM_RE = re.compile(
    r"^\(?\s*[-+]?\s*[$€£¥₹]?\s*[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*[$€£¥₹%]?\s*\)?$"  # grouped
    r"|^\(?\s*[-+]?\s*[$€£¥₹]\s*[-+]?\d+(?:\.\d+)?\s*\)?$"                               # currency
    r"|^\(?\s*[-+]?\d+(?:\.\d+)?\s*%\s*\)?$"                                              # percent
    r"|^\(\s*\d+(?:\.\d+)?\s*\)$"                                                         # (123.4)
    r"|^[-+]?\d+(?:\.\d+)?$"                                                              # plain
)


def _to_number(text: str) -> tuple[Optional[float], bool]:
    """Return (value, is_percent). value None when not numeric."""
    t = text.strip()
    if not _NUM_RE.match(t):
        return None, False
    neg = t.startswith("(") and t.endswith(")")
    is_pct = t.rstrip(")").rstrip().endswith("%")
    cleaned = re.sub(r"[()$€£¥₹%,\s]", "", t)
    try:
        v = float(cleaned)
    except ValueError:
        return None, False
    return (-abs(v) if neg else v), is_pct


def convert_numeric_text(df: pd.DataFrame) -> pd.DataFrame:
    """Convert object columns holding currency / grouped / percent / (negative) text."""
    df = df.copy()
    renames: dict[str, str] = {}
    for col in list(df.columns):
        s = df[col]
        if not (s.dtype == object or pd.api.types.is_string_dtype(s)):
            continue
        vals = s.dropna().astype(str)
        vals = vals[vals.str.strip() != ""]
        if vals.empty:
            continue
        parsed = [_to_number(v) for v in vals]
        ok = [p for p in parsed if p[0] is not None]
        if len(ok) / len(parsed) < _MIN_MATCH:
            continue
        # Require a formatting marker; plain digit text (zip codes, ids with
        # leading zeros) was deliberately kept as text upstream.
        marked = any(re.search(r"[$€£¥₹%,()]", v) for v in vals)
        if not marked:
            continue
        # any value that is not numeric (e.g. "N/A") becomes NULL, but only <2% of them
        pct_cols = sum(1 for p in ok if p[1])
        out = s.map(lambda x: _to_number(str(x))[0] if isinstance(x, str) else x)
        df[col] = pd.to_numeric(out, errors="coerce")
        if pct_cols and re.search(r"pct|percent", str(col)) is None:
            renames[col] = f"{col}_pct"
    if renames:
        existing = set(df.columns)
        for old, new in renames.items():
            if new not in existing:
                df = df.rename(columns={old: new})
                existing.add(new)
    return df


_DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
    "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%d %B %Y", "%Y%m%d",
]
_SLASH_RE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$")


def _parse_all(vals: list[str], fmt: str) -> Optional[list[datetime]]:
    out = []
    for v in vals:
        try:
            out.append(datetime.strptime(v.strip(), fmt))
        except ValueError:
            return None
    return out


def _format_dates(parsed: list[datetime]) -> list[str]:
    if all(d.hour == d.minute == d.second == 0 for d in parsed):
        return [d.strftime("%Y-%m-%d") for d in parsed]
    return [d.strftime("%Y-%m-%d %H:%M:%S") for d in parsed]


def _slash_layout(vals: list[str]) -> Optional[str]:
    """Return '%d/%m/%Y' or '%m/%d/%Y' only when the column disambiguates it."""
    first_gt12 = second_gt12 = False
    for v in vals:
        m = _SLASH_RE.match(v.strip())
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2))
        first_gt12 |= a > 12
        second_gt12 |= b > 12
        if a > 31 or b > 31:
            return None
    if first_gt12 and not second_gt12:
        return "DMY"
    if second_gt12 and not first_gt12:
        return "MDY"
    return None


def normalize_date_text(df: pd.DataFrame) -> pd.DataFrame:
    """Rewrite date-like text columns to sortable ISO text ('YYYY-MM-DD')."""
    df = df.copy()
    for col in list(df.columns):
        s = df[col]
        if not (s.dtype == object or pd.api.types.is_string_dtype(s)):
            continue
        mask = s.notna() & (s.astype(str).str.strip() != "")
        vals = s[mask].astype(str).tolist()
        if len(vals) < 2:
            continue
        # must look like dates, not ids: need a separator or month name
        if not any(re.search(r"[-/. ,]", v) for v in vals):
            continue
        parsed = None
        for fmt in _DATE_FORMATS:
            parsed = _parse_all(vals, fmt)
            if parsed:
                break
        if parsed is None:
            layout = _slash_layout(vals)
            if layout:
                parsed_list = []
                for v in vals:
                    m = _SLASH_RE.match(v.strip())
                    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    d, mo = (a, b) if layout == "DMY" else (b, a)
                    try:
                        parsed_list.append(datetime(y, mo, d))
                    except ValueError:
                        parsed_list = None
                        break
                parsed = parsed_list
        if not parsed:
            continue
        new = s.copy().astype(object)
        new[mask] = _format_dates(parsed)
        df[col] = new
    return df


def normalize_datetime_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Excel datetime cells -> ISO text; date-only when every time is midnight."""
    df = df.copy()
    for col in list(df.columns):
        s = df[col]
        if pd.api.types.is_datetime64_any_dtype(s):
            nn = s.dropna()
            midnight = bool((nn == nn.dt.normalize()).all()) if len(nn) else True
            fmt = "%Y-%m-%d" if midnight else "%Y-%m-%d %H:%M:%S"
            txt = s.dt.strftime(fmt)
            df[col] = txt.where(s.notna(), None)
        elif s.dtype == object:
            # mixed python datetime/date objects (openpyxl) -> same rule
            nn = s.dropna()
            if len(nn) and all(isinstance(v, datetime) for v in nn):
                midnight = all(v.hour == v.minute == v.second == 0 for v in nn)
                fmt = "%Y-%m-%d" if midnight else "%Y-%m-%d %H:%M:%S"
                df[col] = s.map(lambda v: v.strftime(fmt) if isinstance(v, datetime) else None)
    return df


def normalize_tabular_values(df: pd.DataFrame) -> pd.DataFrame:
    df = normalize_datetime_columns(df)
    df = convert_numeric_text(df)
    df = normalize_date_text(df)
    return df
