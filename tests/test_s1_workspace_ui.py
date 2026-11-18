"""Sprint 1 workspace UI: the real static/js/app.js functions rendered in jsdom with hostile data, plus static wiring
checks. Names, e-mails and audit details come from other users, so they must render as text, never as HTML."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from tests.test_xss_escaping import APP_JS, REPO_ROOT, _extract_function, _node_available

needs_node = pytest.mark.skipif(not _node_available(), reason="node + jsdom are required to exercise the real app.js")
SRC = open(APP_JS, encoding="utf-8").read()
HTML = open(os.path.join(REPO_ROOT, "static", "index.html"), encoding="utf-8").read()
EVIL = '<img src=x onerror="window.pwned=1">'

SCRIPT = """
const { JSDOM } = require('jsdom');
const dom = new JSDOM(`<!doctype html><div id="wsDatasets"></div><ul id="wsMembers"></ul><ul id="wsAudit"></ul>
  <button id="wsAuditMore"></button><select id="wsSelect"></select><p id="uploadWorkspaceHint"></p>`);
global.document = dom.window.document; global.window = dom.window;
const $ = id => document.getElementById(id);
const fmtInt = n => String(n);
const EVIL = __EVIL__;
const responses = {
  datasets: {success: true, your_role: 'admin', datasets: [{id: 'd1', name: EVIL, file_format: 'csv', table_count: 1,
             row_count: 2, bytes: 10, created_by: 'u2', created_by_email: EVIL, created_at: null, can_delete: true}]},
  members: {success: true, your_role: 'owner', members: [{user_id: 'u2', email: EVIL, role: 'viewer', joined_at: null},
            {user_id: 'u1', email: 'me@example.com', role: 'owner', joined_at: null}]},
  audit: {success: true, next_cursor: null, entries: [{id: 'a1', actor_type: 'user', actor_id: 'u2', actor_email: EVIL,
          action: EVIL, details: {name: EVIL, role: 'viewer'}, created_at: null}]},
};
const apiGet = async (url) => url.includes('/datasets') ? responses.datasets : url.includes('/members') ? responses.members : responses.audit;
const run = new Function('$', 'apiGet', 'fmtInt', `
  const currentUser = {id: 'u1'};
  let workspaceList = [{id: 'w1', name: ${JSON.stringify(EVIL)}, kind: 'team', role: 'owner'}];
  let activeWorkspaceId = 'w1', wsAuditCursor = null;
  const WS_ROLE_RANK = {viewer: 10, analyst: 20, admin: 30, owner: 40};
  const WS_ACTION_LABELS = {};
  __FNS__
  return (async () => {
    renderWorkspaceSwitcher();
    await loadWsDatasets(); await loadWsMembers(); await loadWsAudit(false);
    return {
      imgs: document.querySelectorAll('img').length,
      pwned: !!window.pwned,
      datasetsText: $('wsDatasets').textContent,
      membersText: $('wsMembers').textContent,
      auditText: $('wsAudit').textContent,
      option: $('wsSelect').options[0].textContent,
      roleOptions: Array.from(document.querySelectorAll('#wsMembers select option')).map(o => o.value),
      deleteButtons: document.querySelectorAll('#wsDatasets button[data-dataset-id]').length,
    };
  })();
`);
run($, apiGet, fmtInt).then(r => console.log(JSON.stringify(r)));
"""

FUNCS = ("wsEl", "wsBytes", "wsWhen", "activeWorkspace", "wsCan", "renderWorkspaceSwitcher", "loadWsDatasets",
         "loadWsMembers", "describeAudit", "loadWsAudit")


@needs_node
def test_workspace_panels_render_hostile_values_as_text():
    parts = []
    for name in FUNCS:
        fn = _extract_function(SRC, name)
        if f"async function {name}(" in SRC:
            fn = "async " + fn
        parts.append(fn)
    fns = "\n".join(parts).replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    script = SCRIPT.replace("__EVIL__", json.dumps(EVIL)).replace("__FNS__", fns)
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["imgs"] == 0 and out["pwned"] is False
    for key in ("datasetsText", "membersText", "auditText", "option"):
        assert EVIL in out[key], key
    assert out["roleOptions"] == ["admin", "analyst", "viewer"]  # the owner may manage the viewer; not themself
    assert out["deleteButtons"] == 1


def test_workspace_module_never_assigns_html():
    start = SRC.index("// ---- Workspaces (Sprint 1)")
    end = SRC.index("(async function boot() {")
    block = SRC[start:end]
    assert "innerHTML" not in block and "insertAdjacentHTML" not in block


def test_workspace_view_is_wired():
    assert 'data-view="workspace"' in HTML and 'id="view-workspace"' in HTML
    assert "if (name === 'workspace') loadWorkspaceView();" in SRC
    # A viewer's uploads stay personal; only analyst+ sends the active workspace.
    assert "if (activeWorkspaceId && wsCan('analyst')) fd.set('workspace_id', activeWorkspaceId);" in SRC
