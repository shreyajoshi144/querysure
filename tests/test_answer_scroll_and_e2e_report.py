"""Regressions behind two CI e2e reports on PR #1 (tests/test_sprint5_e2e.py).

1. "JSON download is valid and has the same number of rows null": no JSON file was ever downloaded. The app smooth-scrolled
   the page to the answer only when the report job finished -- seconds after the answer and its Download buttons were on
   screen -- and the e2e (like a user) clicks Download right then, so on a slow runner the click landed off the moving button.
   Reproduced locally under CPU load: after the CSV click the page kept scrolling (810 -> 875..1003 px), and a download click
   that started no download at all (Chrome's own download events). The answer is now scrolled into view when its rows render.
2. "verification token stripped from URL after use" was not a failure: that check passed (`ok` in the same log). The pytest
   message was the last 4000 characters of stdout, so its headline began wherever that cut fell; the failed checks now come first.
"""
from __future__ import annotations

import os
import re

from tests.test_sprint5_e2e import _failure_message

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_question_body() -> str:
    src = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
    start = src.index("markStep('execution','active')")
    return src[start:src.index("function renderTrustTrace", start)]


def test_the_answer_is_scrolled_into_view_when_rows_render_not_after_the_report():
    body = _run_question_body()
    scrolls = [m.start() for m in re.finditer(r"\$\('sec-answer'\)\.scrollIntoView\(", body)]
    assert len(scrolls) == 1, "exactly one scroll to the answer per run"
    assert body.index("renderTable(rows)") < scrolls[0] < body.index("/api/generate-report"), \
        "the scroll must happen once the rows are rendered, before the report job"
    after_report = body[body.index("lastReport = report"):]
    assert "scrollIntoView" not in after_report, "nothing may scroll the page after the report finishes"


def test_the_e2e_failure_message_leads_with_the_failed_checks():
    passed = "".join(f"  ok   check number {i} passed\n" for i in range(300))
    stdout = passed + "  ok   verification token stripped from URL after use\n" + passed + "\nE2E: 600/601 passed\n"
    stderr = "  FAIL JSON download is valid and has the same number of rows null\n"
    msg = _failure_message(stdout, stderr)
    assert msg.startswith("FAILED CHECKS:\n  FAIL JSON download is valid")
    assert msg.index("FAIL JSON download") < msg.index("E2E: 600/601 passed")
    assert "E2E: 600/601 passed" in msg                      # the tail of the log is still there for context
