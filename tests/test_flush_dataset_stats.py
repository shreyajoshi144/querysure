"""After a flush the dataset panel must not keep the previous dataset's numbers.

Root cause of the CI e2e failure "after flush a new question runs end to end": resetWorkspaceUI() -> updateDatasetUI() only
wrote #dbTables/#dbRows/#dbSize/#dbRels when a dataset was connected, so after a flush they kept the flushed dataset's values.
The e2e waits for #dbTables > 0 to know the sample reconnected; the stale count satisfied it at once, the question was typed
while the connect was still in flight, and the connect's onDatasetActivated() then cleared it (Ask ran with an empty box).
Runs the real updateDatasetUI() from static/js/app.js in jsdom (same approach as tests/test_xss_escaping.py).
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from tests.test_xss_escaping import APP_JS, REPO_ROOT, _extract_function, _node_available

IDS = ["answerSheet", "chipsRow", "emptyState", "emptyTitle", "emptyBody", "emptyActions", "dsEmpty", "dsLayout", "dsStats",
       "dsSubtitle", "dbTables", "dbRows", "dbSize", "dbRels"]


@pytest.mark.skipif(not _node_available(), reason="node + jsdom are required to exercise the real app.js")
def test_disconnecting_clears_the_dataset_stats():
    fn = _extract_function(open(APP_JS, encoding="utf-8").read(), "updateDatasetUI")
    body = "".join(f'<div id="{i}">—</div>' for i in IDS)
    script = f"""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM('<!doctype html><html><body>{body}</body></html>');
    global.document = dom.window.document;
    const $ = id => document.getElementById(id);
    const stats = () => ['dbTables', 'dbRows', 'dbSize', 'dbRels'].map(i => $(i).textContent);
    const run = new Function('$', 'numFmt', 'countRelationships', 'state', `
      let dbConnected, currentDataset, datasetTables = [];
      ${{{json.dumps(fn)}}}
      dbConnected = true; currentDataset = {{name: 'retail.db', tables: 5, rows: 1200, size: '348 KB', sample: true}};
      updateDatasetUI(); state.connected = state.read();
      dbConnected = false; currentDataset = null;       // what resetWorkspaceUI() does on a flush
      updateDatasetUI(); state.flushed = state.read();
    `);
    const state = {{ read: stats }};
    run($, n => String(n), () => 4, state);
    console.log(JSON.stringify({{connected: state.connected, flushed: state.flushed}}));
    """
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=REPO_ROOT)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["connected"] == ["5", "1200", "348 KB", "4"]
    assert out["flushed"] == ["—", "—", "—", "—"], "a flushed workspace still shows the old dataset's numbers"
