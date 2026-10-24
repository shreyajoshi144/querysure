"""
The chart truncation fix (renderChart() previously hard-capped at
rows.slice(0, 10) and then, for bar charts, a further slice(0, 8)) lives in
static/js/app.js, which is browser code with DOM dependencies this test
environment can't execute (no browser/DOM runtime available here). This is
a static-source regression check instead of a behavioral one: it asserts
the specific hardcoded-cap pattern is gone and a named, raised, one-place
constant exists in its place. It is not a substitute for opening the app
in a browser with a >8-row result and confirming the chart visually -- see
the deployment report for what was and wasn't actually run.
"""
from __future__ import annotations

import os

APP_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "js", "app.js")


def _source() -> str:
    with open(APP_JS, "r", encoding="utf-8") as f:
        return f.read()


def test_old_hardcoded_bar_chart_cap_is_gone():
    src = _source()
    assert "data10.length > 8" not in src
    import re
    # Chart data must not be capped at 8; unrelated .slice(0, 8) uses (e.g. masking a token) are fine.
    assert not re.search(r"(rows|data\w*|chartSubset|subset)\.slice\(0,\s*8\)", src)


def test_old_hardcoded_top10_slice_is_gone():
    src = _source()
    assert "rows.slice(0, 10)" not in src


def test_chart_point_limit_is_a_named_configurable_constant():
    src = _source()
    assert "CHART_MAX_POINTS" in src
    # It should be set well above the old 8/10 caps so a "top 20" request
    # actually renders 20 points.
    import re
    m = re.search(r"CHART_MAX_POINTS\s*=\s*(\d+)", src)
    assert m, "CHART_MAX_POINTS constant not found with a numeric value"
    assert int(m.group(1)) >= 20
