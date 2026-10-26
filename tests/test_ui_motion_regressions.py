"""Two CI e2e flakes were the UI moving a button while it was being clicked. These run the real functions from
static/js/app.js in jsdom (as tests/test_xss_escaping.py does); jsdom has no layout, so element geometry is stubbed.

1. Stage panel ("selecting Execute shows ... Generate SQL"): every stage selection smooth-scrolled the pipeline strip (and
   the inspector opened with an animated margin), so the next stage click landed on the neighbouring stage. A user-chosen
   stage now moves the strip only if it is not fully visible, and instantly.
2. Account export ("export downloaded with own data"): settings opened before billing had loaded right after sign-in; the
   late data then showed "Upgrade to Pro" above "Download my data", moving it 37 px under the pointer (the click hit a
   section title, no download). When that data is not loaded yet, the panel now loads it before appearing (bounded).
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from tests.test_xss_escaping import APP_JS, REPO_ROOT, _extract_function, _node_available

needs_node = pytest.mark.skipif(not _node_available(), reason="node + jsdom are required to exercise the real app.js")
SRC = open(APP_JS, encoding="utf-8").read()
CSS = open(os.path.join(REPO_ROOT, "static", "css", "styles.css"), encoding="utf-8").read()


def _node(script: str) -> dict:
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


STRIP = """
const { JSDOM } = require('jsdom');
const nodes = Array.from({length: 8}, (_, i) => `<li class="pipeline-step-node" data-step="n${i}"></li>`).join('');
const dom = new JSDOM(`<!doctype html><div id="wfScroll"><ul>${nodes}</ul></div><aside id="stageDetail" hidden></aside>`);
global.document = dom.window.document; global.window = dom.window;
window.matchMedia = () => ({ matches: false });
const $ = id => document.getElementById(id);
const def = (el, props) => { for (const [k, v] of Object.entries(props)) Object.defineProperty(el, k, { configurable: true, get: () => v }); };
const scroller = $('wfScroll');
def(scroller, { scrollWidth: 1200, clientWidth: 800 });
scroller.scrollLeft = 0;
scroller.getBoundingClientRect = () => ({ left: 0, right: 800, top: 0, bottom: 120 });
document.querySelectorAll('.pipeline-step-node').forEach((li, i) => def(li, { offsetLeft: i * 150, offsetWidth: 140 }));
const calls = [];
scroller.scrollTo = (o) => calls.push(o);
const run = new Function('$', 'cases', `
  let selectedStage = null;
  ${FN}
  const out = {};
  for (const c of cases) {
    calls.length = 0; selectedStage = c.selected;
    document.querySelectorAll('.pipeline-step-node').forEach(li => li.classList.toggle('active', li.dataset.step === c.active));
    const insp = $('stageDetail'); insp.hidden = !c.inspectorLeft;
    Object.defineProperty(insp, 'offsetLeft', { configurable: true, get: () => c.inspectorLeft || 0 });
    Object.defineProperty(insp, 'offsetTop', { configurable: true, get: () => 0 });
    Object.defineProperty(insp, 'offsetWidth', { configurable: true, get: () => c.inspectorLeft ? 440 : 0 });
    keepActiveInView(c.user);
    out[c.name] = calls.map(o => o.behavior);
  }
  return out;
`);
"""


@needs_node
def test_a_stage_the_user_picks_never_slides_the_pipeline():
    fn = _extract_function(SRC, "keepActiveInView")
    cases = [
        {"name": "visible_click", "selected": "n2", "user": True},                         # x 300..440 within 0..800
        {"name": "offscreen_click", "selected": "n7", "user": True},                       # x 1050: must be brought in
        {"name": "under_inspector", "selected": "n3", "user": True, "inspectorLeft": 500},  # x 450..590 partly covered
        {"name": "visible_with_inspector", "selected": "n1", "user": True, "inspectorLeft": 500},
        {"name": "run_progress", "selected": None, "active": "n6", "user": False},
    ]
    script = STRIP.replace("${FN}", fn.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")) + \
        f"console.log(JSON.stringify(run($, {json.dumps(cases)})));"
    out = _node(script)
    assert out["visible_click"] == [], "a visible, just-clicked stage must not move"
    assert out["visible_with_inspector"] == []
    assert out["offscreen_click"] == ["auto"], "a user selection may move the strip only instantly"
    assert out["under_inspector"] == ["auto"], "a node partly under the inspector is brought out, instantly"
    assert out["run_progress"] == ["smooth"], "following a running pipeline stays animated"


def test_opening_the_inspector_does_not_animate_the_workspace_margin():
    rule = re.search(r"body\.inspector-open \.workspace-container \{([^}]*)\}", CSS)
    assert rule and "margin-right: 440px" in rule.group(1)
    assert "transition" not in rule.group(1), "an animated margin reflows the pipeline while it is being clicked"


SETTINGS = """
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="settingsModal" hidden></div>');
global.document = dom.window.document;
const $ = id => document.getElementById(id);
const log = [];
new dom.window.MutationObserver(() => { if (!$('settingsModal').hidden) log.push('revealed'); })
  .observe($('settingsModal'), { attributes: true });
const main = new Function('$', 'log', 'scenario', `
  let accountUsage = scenario.cached ? {plan: 'free'} : null, billingState = scenario.cached ? {status: 'inactive'} : null;
  const later = (ms, fn) => new Promise(r => setTimeout(() => { fn(); r(); }, ms));
  const sleep = (ms) => new Promise(r => setTimeout(r, scenario.capMs ?? ms));
  const setupSettingsGoogle = () => {}, refreshSettingsView = () => {};
  const refreshAccountUsage = () => scenario.hang ? new Promise(() => {}) : later(60, () => { accountUsage = {plan: 'free'}; log.push('usage-loaded'); });
  const refreshBillingState = () => scenario.hang ? new Promise(() => {}) : later(80, () => { billingState = {status: 'inactive'}; log.push('billing-loaded'); });
  ${CONSTS}
  ${FN}
  return (async () => {
    const p = openSettings();
    await new Promise(r => setTimeout(r, 0));
    log.push('hidden-right-after-click:' + $('settingsModal').hidden);
    await p;
    await new Promise(r => setTimeout(r, 120));
    return log;
  })();
`);
"""


@needs_node
@pytest.mark.parametrize("scenario, expected", [
    ({"cached": False}, ["hidden-right-after-click:true", "usage-loaded", "billing-loaded", "revealed"]),
    ({"cached": True}, ["revealed", "hidden-right-after-click:false", "usage-loaded", "billing-loaded"]),
    ({"cached": False, "hang": True, "capMs": 30}, ["hidden-right-after-click:true", "revealed"]),
])
def test_settings_appears_only_with_its_final_rows(scenario, expected):
    fn = _extract_function(SRC, "openSettings")
    consts = "\n".join(re.findall(r"^(?:const SETTINGS_DATA_WAIT_MS = \d+;|let settingsOpening = false;)$", SRC, re.M))
    assert consts.count("\n") == 1, "SETTINGS_DATA_WAIT_MS / settingsOpening declarations not found"
    esc = lambda t: t.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")   # noqa: E731
    script = SETTINGS.replace("${FN}", "async " + esc(fn) if not fn.startswith("async") else esc(fn)).replace("${CONSTS}", esc(consts)) + \
        f"main($, log, {json.dumps(scenario)}).then(l => console.log(JSON.stringify({{log: l}})));"
    assert _node(script)["log"] == expected
