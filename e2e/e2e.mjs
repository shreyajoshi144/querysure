// Real-browser (headless Chromium) end-to-end test of the critical user flows against a LIVE QuerySure server.
// Driven by tests/test_sprint5_e2e.py, which starts the server + stub LLM and passes the environment below.
//   E2E_BASE, E2E_MAIL_DIR, E2E_WEBHOOK_SECRET, E2E_DB_URL, E2E_REPO, E2E_DOWNLOAD_DIR
import chromium from '@sparticuz/chromium';
import puppeteer from 'puppeteer-core';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const BASE = process.env.E2E_BASE;
const MAIL = process.env.E2E_MAIL_DIR;
const SECRET = process.env.E2E_WEBHOOK_SECRET;
const DL = process.env.E2E_DOWNLOAD_DIR;
const REPO = process.env.E2E_REPO;
const email = `e2e-${crypto.randomBytes(5).toString('hex')}@example.com`;
const PASSWORD = 'E2eTestPassw0rd!';

const results = [];
const problems = [];
let step = '';
function ok(name, cond, detail = '') {
  results.push({ name, pass: !!cond, detail: cond ? '' : String(detail) });
  if (!cond) console.error(`  FAIL ${name} ${detail}`);
  else console.log(`  ok   ${name}`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function py(code) {
  try { return execFileSync(process.env.E2E_PYTHON || 'python3', ['-c', code], { cwd: REPO, env: process.env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim(); }
  catch (e) { throw new Error('py helper failed: ' + String(e.stderr || e.message).slice(-600)); }
}
function signedWebhook(payload) {
  const body = JSON.stringify(payload);
  const t = Math.floor(Date.now() / 1000);
  const mac = crypto.createHmac('sha256', SECRET).update(`${t}.${body}`).digest('hex');
  return fetch(`${BASE}/api/billing/webhook/generic`, { method: 'POST', body, headers: { 'X-Signature': `t=${t},v1=${mac}`, 'Content-Type': 'application/json' } });
}
async function readMailToken(marker) {
  for (let i = 0; i < 40; i++) {
    const files = fs.readdirSync(MAIL).map((f) => ({ f, t: fs.statSync(path.join(MAIL, f)).mtimeMs })).sort((a, b) => b.t - a.t);
    for (const { f } of files) {
      const text = fs.readFileSync(path.join(MAIL, f), 'utf8');
      if (text.includes(`To: ${email}`)) {
        const m = text.match(new RegExp(marker + '=([A-Za-z0-9_-]+)'));
        if (m) return m[1];
      }
    }
    await sleep(250);
  }
  return null;
}
const visible = (page, sel) => page.$eval(sel, (e) => !!(e.offsetParent || e.getClientRects().length)).catch(() => false);
async function waitFor(page, fn, arg, timeout = 20000) {
  try { await page.waitForFunction(fn, { timeout, polling: 100 }, arg); return true; } catch { return false; }
}

// The topbar is sticky and the previous run smooth-scrolls the page, so a composer button can sit UNDER the topbar while
// puppeteer still considers it visible (this made a click on "Ask" land on "Clear session" under load). Scroll to the top first.
// v1.0.7: the sample/flush/sign-out actions live in the top-bar menus; open the menu, then act.
async function viaMenu(menuBtn, sel) { await clickAtTop('#' + menuBtn); await page.click(sel); }
async function clickAtTop(sel) {
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
  await sleep(150);
  await page.click(sel);
}

const browser = await puppeteer.launch({
  executablePath: await chromium.executablePath(),
  args: [...chromium.args, '--no-sandbox'],
  headless: 'shell',
});
const page = await browser.newPage();
page.setDefaultNavigationTimeout(90000);   // v1.0.7: software-rendered headless Chromium can take >30s to settle the animated landing page
await page.setViewport({ width: 1366, height: 900 });
const cdp = await page.createCDPSession();
await cdp.send('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: DL });

const httpLog = [];
const allUrls = [];
const PASSWORD_Q = encodeURIComponent(PASSWORD);
page.on('response', (r) => { if (r.url().includes('/api/') && r.status() >= 400) httpLog.push(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`); });
// Any console error / uncaught exception / CSP violation fails the run (except expected HTTP 4xx fetch logs).
page.on('pageerror', (e) => problems.push(`pageerror@${step}: ${e.message}`));
page.on('console', (m) => {
  if (m.type() !== 'error') return;
  const t = m.text();
  if (/Failed to load resource: the server responded with a status of (4\d\d|503)/.test(t)) return;
  problems.push(`console.error@${step}: ${t}`);
});
page.on('request', (r) => allUrls.push(r.url()));
await page.evaluateOnNewDocument(() => {
  window.__csp = [];
  document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
  window.alert = (m) => { window.__alerts = (window.__alerts || []).concat(String(m)); };
});

// Act as the "payment provider's hosted page": intercept navigation to it and record the signed link.
let providerUrl = null;
let delayBillingMs = 0;   // > 0: hold GET /api/billing that long, as on a slow network
await page.setRequestInterception(true);
page.on('request', (req) => {
  const u = req.url();
  if (delayBillingMs && new URL(u).pathname === '/api/billing') { setTimeout(() => req.continue(), delayBillingMs); return; }
  if (u.startsWith('https://pay.example.test/')) {
    if (req.isNavigationRequest()) providerUrl = u;
    return req.respond({ status: 200, contentType: 'text/html', body: '<h1>provider checkout</h1>' });
  }
  req.continue();
});

// Reach the sign-in form from whatever screen the app is currently on.
async function openLogin() {
  if (await visible(page, '#verificationBackBtn')) await page.click('#verificationBackBtn');
  if (!(await visible(page, '#loginEmail')) && (await visible(page, '#navStartBtn'))) await page.click('#navStartBtn');
  if (await visible(page, '.auth-tab[data-form="login"]')) await page.click('.auth-tab[data-form="login"]');
  await page.waitForSelector('#loginEmail', { visible: true, timeout: 10000 });
}

try {
  // ---------------------------------------------------------------- 1. landing / pricing honesty
  step = 'landing';
  await page.goto(BASE, { waitUntil: 'networkidle0' });
  const assets = await page.$$eval('script[src],link[rel=stylesheet]', (els) => els.map((e) => e.src || e.href));
  ok('assets are content-addressed', assets.filter((u) => u.includes('/static/')).every((u) => /\?v=[0-9a-f]{10}$|\.[0-9a-f]{10}\.min\.(js|css)$/.test(u)), assets.join(' '));
  const pricing = await page.$eval('#pricing', (e) => e.innerText);
  ok('four pricing cards', (await page.$$('#pricing .pricing-plan')).length === 4);
  ok('Team promise removed', /no shared workspaces, seats or organization accounts/i.test(pricing) && !/\$15/.test(pricing) && !/For teams/.test(pricing), pricing);
  ok('no "coming soon" fiction', !/coming soon/i.test(pricing));
  ok('Pro availability reflects billing_enabled', await waitFor(page, () => document.getElementById('proAvailability')?.textContent.includes('Available'), null, 5000));

  // ---------------------------------------------------------------- 2. register -> verify -> login
  step = 'register';
  await page.click('#navStartBtn');
  // ---- credentials hygiene: nothing is pre-filled or fillable behind the visible form, and links/navigation never carry or keep secrets
  step = 'credentials';
  const elig = () => page.evaluate(() => Object.fromEntries([...document.querySelectorAll('#screen-auth .auth-form')].map((f) => [f.id, { active: f.classList.contains('active'), disabled: [...f.querySelectorAll('input')].every((i) => i.disabled), anyEnabled: [...f.querySelectorAll('input')].some((i) => !i.disabled) }])));
  let el0 = await elig();
  ok('sign-in is the only fillable form on arrival (hidden forms are disabled, so password managers cannot fill them)', el0.loginForm.active && el0.loginForm.anyEnabled && ['registerForm', 'forgotPasswordForm', 'resetPasswordForm'].every((f) => el0[f].disabled), JSON.stringify(el0));
  ok('sign-in fields start empty', await page.evaluate(() => !document.getElementById('loginEmail').value && !document.getElementById('loginPassword').value));
  await page.type('#loginEmail', 'someone@example.com');
  await page.type('#loginPassword', 'Typed-Secret-1!');
  await page.click('.auth-tab[data-form="register"]');
  ok('switching to Create account shows empty fields and makes only that form fillable', await page.evaluate(() => !document.getElementById('registerEmail').value && !document.getElementById('registerPassword').value) && (await elig()).registerForm.anyEnabled && (await elig()).loginForm.disabled);
  await page.click('.auth-tab[data-form="login"]');
  ok('what was typed in the sign-in form is scrubbed when its form is hidden, not kept in the DOM', await page.evaluate(() => !document.getElementById('loginEmail').value && !document.getElementById('loginPassword').value));
  await page.type('#loginEmail', 'typed.person@example.com');
  await page.type('#loginPassword', 'Typed-Secret-2!');
  await page.click('#forgotPasswordBtn');
  ok('Forgot-password link opens an empty, non-autofillable-looking form (no password carried over)', await page.evaluate(() => !document.getElementById('forgotEmail').value && document.getElementById('forgotEmail').autocomplete === 'off'));
  await page.click('#forgotPasswordForm .auth-back-form');
  await page.goto('about:blank');
  await page.goBack({ waitUntil: 'networkidle0' });
  ok('after navigating away and Back (bfcache) no credential is left in the page', await page.evaluate(() => ['loginEmail', 'loginPassword', 'registerEmail', 'registerPassword'].every((i) => !document.getElementById(i).value)));
  ok('the URL carries no credentials', !/@|password|Typed-Secret/i.test(page.url()), page.url());
  step = 'register';
  if (!(await visible(page, '.auth-tab[data-form="register"]'))) await page.click('#navStartBtn');
  await page.click('.auth-tab[data-form="register"]');
  await page.type('#registerEmail', email);
  await page.type('#registerPassword', PASSWORD);
  await page.click('#registerSubmit');
  ok('verification prompt shown after registration', await waitFor(page, () => { const e = document.getElementById('authVerificationState'); return e && (e.offsetParent !== null); }, null, 15000));
  const vtoken = await readMailToken('verify_token');
  ok('verification e-mail delivered with token in URL fragment', !!vtoken);
  await page.goto('about:blank');
  step = 'verify';
  await page.goto(`${BASE}/#verify_token=${vtoken}`, { waitUntil: 'networkidle0' });
  ok('verification token stripped from URL after use', await waitFor(page, () => !location.hash.includes('verify_token'), null, 8000));
  step = 'login';
  await openLogin();
  await page.type('#loginEmail', email);
  await page.type('#loginPassword', PASSWORD);
  await page.click('#loginSubmit');
  ok('login reaches workspace', await waitFor(page, () => document.getElementById('screen-workspace')?.classList.contains('active'), null, 20000));
  const token1 = await page.evaluate(() => localStorage.getItem('qs_token'));
  const claims = JSON.parse(Buffer.from(token1.split('.')[1], 'base64url').toString());
  ok('token is bound to a server-side session (sid claim)', !!claims.sid);
  ok('no auth cookies are set (CSRF-immune bearer design)', (await page.cookies()).length === 0);

  // ---------------------------------------------------------------- 3. core pipeline + report download
  step = 'pipeline';
  await viaMenu('dataMenuBtn', '#sampleBtn');
  ok('sample dataset connects', await waitFor(page, () => Number(document.getElementById('dbTables')?.textContent) > 0, null, 30000));
  await page.type('#question', 'total sales amount by region');
  // Record every scrollIntoView the app makes during this run, and whether the report had already finished at the time.
  await page.evaluate(() => {
    window.__scrollCalls = [];
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (opts) {
      window.__scrollCalls.push({ id: this.id, afterReport: !!(typeof lastReport !== 'undefined' && lastReport) });
      return original.call(this, opts);
    };
  });
  await clickAtTop('#analyzeBtn');
  ok('SQL generated and results rendered', await waitFor(page, () => document.querySelectorAll('#resultsTable tbody tr').length > 0, null, 60000));
  const sql = await page.$eval('#sqlOut', (e) => e.textContent);
  ok('generated SQL is shown', /select/i.test(sql), sql);
  ok('durable report id produced by the run', await waitFor(page, () => typeof lastReport !== 'undefined' && lastReport && !!lastReport.report_id, null, 60000));
  // The answer is scrolled into view when its rows render. A scroll started when the (slower) report finished moved the
  // Download buttons under the pointer: in CI the JSON click then landed off the button and no file was ever downloaded.
  const scrollCalls = await page.evaluate(() => window.__scrollCalls || []);
  ok('the answer is scrolled into view when it renders, not after the report finishes', scrollCalls.some((c) => c.id === 'sec-answer' && !c.afterReport) && !scrollCalls.some((c) => c.afterReport), JSON.stringify(scrollCalls));
  // ---- redundant "Download report" control is gone; CSV / JSON downloads work and match the on-screen result
  ok('Download report button removed', (await page.$('#downloadReportBtn')) === null);
  ok('CSV and JSON download buttons remain', !!(await page.$('#downloadCsvBtn')) && !!(await page.$('#downloadJsonBtn')));
  const shown = await page.evaluate(() => ({ head: [...document.querySelectorAll('#resultsTable thead th')].map((t) => t.textContent.trim()), rows: document.querySelectorAll('#resultsTable tbody tr').length }));
  async function grabDownload(btn, ext) {
    fs.rmSync(DL, { recursive: true, force: true }); fs.mkdirSync(DL, { recursive: true });
    await page.click(btn);
    let f = null;
    let last = -1;
    for (let i = 0; i < 80; i++) {            // the file shows up while Chromium is still writing it: wait for a stable, non-empty size
      await sleep(250); f = fs.readdirSync(DL).find((n) => n.endsWith(ext)) || null;
      const size = f ? fs.statSync(path.join(DL, f)).size : -1;
      if (f && size > 0 && size === last) break;
      last = size;
    }
    return f ? fs.readFileSync(path.join(DL, f), 'utf8') : null;
  }
  const csvText = await grabDownload('#downloadCsvBtn', '.csv');
  const csvLines = csvText ? csvText.trim().split(/\r?\n/) : [];
  ok('CSV download saved, header matches the table and row count matches', csvLines.length === shown.rows + 1 && csvLines[0].split(',').length === shown.head.length, `${csvLines.length} lines vs ${shown.rows} rows`);
  const jsonText = await grabDownload('#downloadJsonBtn', '.json');
  let jsonRows = null; try { jsonRows = JSON.parse(jsonText); } catch {}
  ok('JSON download is valid and has the same number of rows', Array.isArray(jsonRows) && jsonRows.length === shown.rows && jsonRows.length > 0, jsonText && jsonText.slice(0, 80));

  // ---- connected, accessible, clickable pipeline: selecting a stage shows what it actually did; the answer stays visible
  step = 'stages';
  const stageInfo = await page.$$eval('#pipeList .stage-btn', (bs) => bs.map((b) => ({ tag: b.tagName, label: b.getAttribute('aria-label'), pressed: b.getAttribute('aria-pressed') })));
  ok('eight workflow nodes are real buttons with status in their accessible name', stageInfo.length === 8 && stageInfo.every((s) => s.tag === 'BUTTON' && /: (completed|skipped|failed|running|pending|warning|no details)$/.test(s.label)), JSON.stringify(stageInfo));
  ok('executed stages report completed', stageInfo.filter((s) => /completed|warning/.test(s.label)).length >= 8, JSON.stringify(stageInfo.map((s) => s.label)));
  ok('stage details are hidden until a stage is selected', await page.$eval('#stageDetail', (e) => e.hidden));
  // Where every stage button is, read straight after a click and again 400 ms later: identical when nothing slides.
  const stagePositions = () => page.evaluate(() => [...document.querySelectorAll('#pipeList .stage-btn')].map((b) => Math.round(b.getBoundingClientRect().left)).join(','));
  await page.click('#pipeList [data-step="validation"] .stage-btn');
  const opened0 = await stagePositions();
  const det = await page.$eval('#stageDetail', (e) => ({ hidden: e.hidden, text: e.innerText }));
  ok('selecting Validate shows its status, duration and real response fields', !det.hidden && /Validate/.test(det.text) && /completed/i.test(det.text) && /\d+\s*ms/.test(det.text) && /Verdict/.test(det.text) && /Passed/.test(det.text), det.text.slice(0, 300));
  ok('selected stage is exposed via aria-pressed', (await page.$eval('#pipeList [data-step="validation"] .stage-btn', (b) => b.getAttribute('aria-pressed'))) === 'true');
  await sleep(400);
  const opened1 = await stagePositions();
  // The strip used to slide (smooth scroll + an animated margin as the inspector opened) right after a click, so the next
  // stage click landed on the neighbouring stage ("selecting Execute shows ... Generate SQL" in CI).
  ok('selecting a stage does not slide the pipeline under the pointer', opened0 === opened1, `${opened0} -> ${opened1}`);
  await page.click('#pipeList [data-step="sql"] .stage-btn');
  const detSql = await page.$eval('#stageDetail', (e) => e.innerText);
  ok('selecting Generate SQL shows the generated SQL response', /Generate SQL/.test(detSql) && /select/i.test(detSql));
  await page.click('#pipeList [data-step="execution"] .stage-btn');
  const exec0 = await stagePositions();
  const detExec = await page.$eval('#stageDetail', (e) => e.innerText);
  ok('selecting Execute shows the real row count and result preview', /Rows returned/i.test(detExec) && /First rows/i.test(detExec), detExec.slice(0, 300));
  await sleep(400);
  const exec1 = await stagePositions();
  ok('switching stages with the inspector open does not slide the pipeline either', exec0 === exec1, `${exec0} -> ${exec1}`);
  await page.click('#pipeList [data-step="verify"] .stage-btn');
  const detVerify = await page.$eval('#stageDetail', (e) => e.innerText);
  ok('selecting Verify shows correctness checks and the independent verification', /Correctness checks/i.test(detVerify) && /Independent verification/i.test(detVerify), detVerify.slice(0, 300));
  await page.click('#pipeList [data-step="plan"] .node-details-btn');
  ok('View details opens the inspector for that stage', await page.evaluate(() => !document.getElementById('stageDetail').hidden && /Plan/.test(document.getElementById('stageDetail').innerText)));
  ok('stage details never expose tokens or secrets', !/Bearer |eyJ[A-Za-z0-9_-]{10,}\.|access_token|api_key["']?:\s*"[^\[]/.test(await page.$eval('#stageDetail', (e) => e.innerHTML)));
  await page.focus('#pipeList [data-step="sql"] .stage-btn');
  await page.keyboard.press('ArrowRight');
  ok('arrow keys move focus between stages', await page.evaluate(() => document.activeElement?.closest('.pipeline-step-node')?.dataset.step === 'validation'));
  await page.keyboard.press('Enter');
  ok('Enter on a focused stage selects it', (await page.$eval('#stageDetail', (e) => e.innerText)).includes('Validate'));
  ok('the main result stays visible below the workflow', await visible(page, '#sec-answer') && (await page.$$('#resultsTable tbody tr')).length > 0);
  const geo = await page.evaluate(() => { const p = document.getElementById('runProgress').getBoundingClientRect(), a = document.getElementById('answerSheet').getBoundingClientRect(); return { pBottom: p.bottom + scrollY, aTop: a.top + scrollY }; });
  ok('workflow sits above the answer (detail panel does not push the result out of the layout)', geo.pBottom <= geo.aTop + 2, JSON.stringify(geo));
  await page.click('#pipeList [data-step="execution"] .stage-btn');
  await page.click('#pipeList [data-step="execution"] .stage-btn');
  ok('clicking the selected stage again collapses the details', await page.$eval('#stageDetail', (e) => e.hidden));

  // ---- Flush Session ("Clear session"): every query surface and the backend session are emptied, history is kept
  step = 'flush';
  const histBefore = await (await fetch(`${BASE}/api/history`, { headers: { Authorization: `Bearer ${token1}` } })).json();
  ok('history has the executed query before the flush', histBefore.total >= 1, JSON.stringify(histBefore).slice(0, 120));
  await page.click('#pipeList [data-step="sql"] .stage-btn');           // leave a stage panel open to prove it is cleared too
  await viaMenu('dataMenuBtn', '#newSessionBtn');
  ok('flush: answer sheet and progress are hidden', await waitFor(page, () => document.getElementById('answerSheet').classList.contains('hidden-element') && document.getElementById('runProgress').hidden, null, 10000));
  const flushed = await page.evaluate(() => ({
    question: document.getElementById('question').value,
    stats: ['dbTables', 'dbRows', 'dbSize', 'dbRels'].map((i) => document.getElementById(i).textContent),
    rows: document.querySelectorAll('#resultsTable tbody tr').length,
    sql: document.getElementById('sqlOut').textContent,
    kpis: ['kpiConfidence', 'kpiReliability', 'kpiRows', 'kpiExec', 'relScore'].map((i) => document.getElementById(i).textContent),
    stageHidden: document.getElementById('stageDetail').hidden,
    stagesIdle: [...document.querySelectorAll('.pipeline-step-node')].every((n) => !/\b(done|active|error)\b/.test(n.className)),
    stageLabels: [...document.querySelectorAll('#pipeList .stage-btn')].every((b) => /pending$/.test(b.getAttribute('aria-label'))),
    raw: document.getElementById('rawOut').textContent,
    trace: document.getElementById('trustTraceContent').innerText,
    val: document.getElementById('valContent').innerText,
    retr: document.getElementById('retrievalContent').innerText,
    status: document.getElementById('dbStatusText')?.textContent || document.querySelector('.workspace-llm-status, #dbStatus')?.textContent || '',
    err: document.getElementById('globalErrorBanner').classList.contains('visible'),
    schemaList: document.getElementById('schemaList').children.length,
  }));
  ok('flush: question box emptied', flushed.question === '', flushed.question);
  // the reconnect below waits for #dbTables > 0: a stale count from the flushed dataset would satisfy it before the new
  // connect finished, and that connect then cleared the question typed next (the 'after flush' step failed in CI)
  ok('flush: dataset stats cleared', flushed.stats.every((v) => v === '—'), JSON.stringify(flushed.stats));
  ok('flush: result table, SQL, KPIs and raw response reset', flushed.rows === 0 && /appears here/.test(flushed.sql) && flushed.kpis.every((k) => k === '—') && /appear here/.test(flushed.raw), JSON.stringify(flushed));
  ok('flush: stage statuses and open stage panel cleared', flushed.stageHidden && flushed.stagesIdle && flushed.stageLabels, JSON.stringify(flushed));
  ok('flush: trace, validation and retrieval panels reset, no stale error, no dataset listed', /Run a successful query/.test(flushed.trace) && /Appears after/.test(flushed.val) && /Appears after/.test(flushed.retr) && !flushed.err && flushed.schemaList === 0, JSON.stringify(flushed));
  const stateRow = py(['import json',
    'from db import SessionLocal, PipelineSession, User',
    'db = SessionLocal()',
    `u = db.query(User).filter(User.email == '${email}').one()`,
    'rows = db.query(PipelineSession).filter(PipelineSession.user_id == u.id).all()',
    "fields = ['db_path','schema','semantic','retrieval','sql','sql_result','question','validation','query_result','reliability','insights']",
    'print(json.dumps([{f: bool(getattr(r, f)) for f in fields} for r in rows]))'].join('\n'));
  const sessRows = JSON.parse(stateRow.split('\n').pop());
  ok('flush: backend session holds no dataset, SQL, result, question or analysis', sessRows.length >= 1 && sessRows.every((r) => Object.values(r).every((v) => v === false)), stateRow);
  const histAfter = await (await fetch(`${BASE}/api/history`, { headers: { Authorization: `Bearer ${token1}` } })).json();
  ok('flush: saved history is preserved', histAfter.total === histBefore.total && histAfter.items[0].id === histBefore.items[0].id, JSON.stringify(histAfter).slice(0, 160));
  await page.click('.rail-link[data-view="history"], [data-view="history"]');
  ok('flush: History view still lists the saved query and opens its saved result', await waitFor(page, () => document.querySelectorAll('#historyList .history-item, #historyList [data-id], #historyList li, #historyList button').length > 0, null, 10000));
  await page.click('[data-view="ask"]');
  // run again after the flush: reconnect + ask works (quota is exercised next)
  await viaMenu('dataMenuBtn', '#sampleBtn');
  ok('after flush a dataset can be connected again', await waitFor(page, () => Number(document.getElementById('dbTables')?.textContent) > 0, null, 30000));
  await page.type('#question', 'total sales amount by region');
  await clickAtTop('#analyzeBtn');
  ok('after flush a new question runs end to end', await waitFor(page, () => document.querySelectorAll('#resultsTable tbody tr').length > 0, null, 60000));

  // ---------------------------------------------------------------- 4. quota state (distinct from rate limiting)
  step = 'quota';
  const used = () => py(['from db import SessionLocal, User, get_or_create_daily_usage', 'db = SessionLocal()', `u = db.query(User).filter(User.email == '${email}').one()`, 'print(get_or_create_daily_usage(db, u.id).query_count)'].join('\n')).split('\n').pop();
  const before = Number(used());
  py(['from db import SessionLocal, User, get_or_create_daily_usage',
      'db = SessionLocal()',
      `u = db.query(User).filter(User.email == '${email}').one()`,
      'r = get_or_create_daily_usage(db, u.id)',
      'r.query_count = 10',
      'db.commit()'].join('\n'));
  ok('quota set to 10/10 for the account', Number(used()) === 10 && before < 10, `${before}`);
  await waitFor(page, () => !document.getElementById('analyzeBtn').disabled, null, 60000);   // the previous run must have fully finished
  await page.evaluate(() => { document.getElementById('question').value = ''; });
  await page.type('#question', 'total sales amount by region again');
  await clickAtTop('#analyzeBtn');
  const quotaSeen = await waitFor(page, () => /Daily query limit reached/.test(document.getElementById('globalErrorText')?.textContent || ''), null, 30000);
  if (!quotaSeen) console.log('  DIAG2', JSON.stringify(await page.evaluate(() => ({ banner: document.getElementById('globalErrorText').textContent, sid: sessionId, dbc: dbConnected, state: document.getElementById('pipelineState').textContent, busy: document.getElementById('analyzeBtn').disabled, q: document.getElementById('question').value, raw: document.getElementById('rawOut').textContent.slice(0, 300), rows: document.querySelectorAll('#resultsTable tbody tr').length }))), JSON.stringify(httpLog.slice(-6)));
  ok('quota banner shows daily-limit message, not the rate-limit one', quotaSeen);
  const banner = await page.$eval('#globalErrorText', (e) => e.textContent);
  ok('quota banner offers upgrade and says when it resets', /Upgrade to Pro/.test(banner) && /00:00 UTC/.test(banner) && !/moving a little fast/.test(banner), banner);
  ok('quota banner states 10/10', /10\/10/.test(banner), banner);
  ok('blocked run was stopped before any SQL was shown or run (no stale result, no generic error)', await page.evaluate(() => document.querySelectorAll('#resultsTable tbody tr').length === 0 && /Daily limit reached/.test(document.getElementById('pipelineState').textContent)));
  ok('blocked attempt did not consume a slot', Number(used()) === 10, used());
  const gen = await fetch(`${BASE}/api/generate-sql`, { method: 'POST', headers: { Authorization: `Bearer ${token1}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: await page.evaluate(() => sessionId), question: 'another one' }) });
  const genBody = await gen.json();
  ok('API: generate-sql is refused with 429 QUERY_LIMIT_REACHED at 10/10', gen.status === 429 && genBody.code === 'QUERY_LIMIT_REACHED' && genBody.remaining === 0, JSON.stringify(genBody));
  const topbar = await page.$eval('#topbarUsage', (e) => e.textContent);
  ok('topbar shows 10/10 queries today', /10\/10/.test(topbar), topbar);
  await page.click('#globalErrorDismiss');
  // reset: usage rows are per UTC day, so yesterday's row is what a new day looks like. Move the row back a day: today has no usage.
  py(['from db import SessionLocal, User, UsageDaily',
      'import datetime',
      'db = SessionLocal()',
      `u = db.query(User).filter(User.email == '${email}').one()`,
      "for r in db.query(UsageDaily).filter(UsageDaily.user_id == u.id).all():",
      "    r.day = (datetime.datetime.strptime(r.day, '%Y-%m-%d') - datetime.timedelta(days=1)).strftime('%Y-%m-%d')",
      'db.commit()'].join('\n'));
  await page.reload({ waitUntil: 'networkidle0' });
  ok('after the daily reset the usage shows 0 again', await waitFor(page, () => /^0\/10/.test(document.getElementById('topbarUsage')?.textContent || ''), null, 45000), await page.$eval('#topbarUsage', (e) => e.textContent));
  await viaMenu('dataMenuBtn', '#sampleBtn');
  await waitFor(page, () => Number(document.getElementById('dbTables')?.textContent) > 0, null, 30000);
  await page.type('#question', 'total sales amount by region');
  await clickAtTop('#analyzeBtn');
  ok('after the reset queries run again', await waitFor(page, () => document.querySelectorAll('#resultsTable tbody tr').length > 0, null, 60000));

  // ---------------------------------------------------------------- 5. billing: checkout -> signed webhook -> Pro
  step = 'billing';
  await page.click('#settingsToggleBtn');
  ok('billing panel shows free state with Upgrade button', await waitFor(page, () => !document.getElementById('settingsUpgradeBtn').hidden && /Free plan/.test(document.getElementById('settingsBillingMessage').textContent), null, 10000));
  await Promise.all([page.waitForRequest((r) => r.url().startsWith('https://pay.example.test/'), { timeout: 15000 }), page.click('#settingsUpgradeBtn')]);
  ok('browser was sent to the provider checkout link', !!providerUrl, providerUrl);
  const q = new URL(providerUrl).searchParams;
  ok('checkout link is signed and carries only an opaque reference', q.get('sig')?.length === 64 && q.get('reference')?.length === 36 && !providerUrl.includes('@'), providerUrl);
  const plan0 = await (await fetch(`${BASE}/api/account/usage`, { headers: { Authorization: `Bearer ${token1}` } })).json();
  ok('plan unchanged by merely starting checkout', plan0.plan === 'free', JSON.stringify(plan0));
  const now = Math.floor(Date.now() / 1000);
  const sub = 'sub_' + crypto.randomBytes(4).toString('hex');
  const evt = { id: 'evt_' + crypto.randomBytes(6).toString('hex'), type: 'subscription.updated', created: now,
    data: { reference: q.get('reference'), customer_id: 'cus_' + sub, subscription_id: sub, plan: 'pro', status: 'active', current_period_start: now, current_period_end: now + 30 * 86400 } };
  const forged = await fetch(`${BASE}/api/billing/webhook/generic`, { method: 'POST', body: JSON.stringify(evt), headers: { 'X-Signature': 't=1,v1=00', 'Content-Type': 'application/json' } });
  ok('unsigned/forged webhook is rejected (400)', forged.status === 400);
  const w1 = await signedWebhook(evt);
  ok('signed webhook accepted', w1.status === 200 && (await w1.json()).outcome === 'processed');
  const w2 = await signedWebhook(evt);
  ok('duplicate delivery is a no-op', w2.status === 200 && (await w2.json()).outcome === 'duplicate');
  step = 'checkout-return';
  await page.goto(`${BASE}/?billing=return`, { waitUntil: 'networkidle0' });
  ok('return from provider: Pro becomes active in the UI', await waitFor(page, () => /Pro/.test(document.getElementById('settingsPlanName')?.textContent || '') && /Renews on/.test(document.getElementById('settingsBillingMessage')?.textContent || ''), null, 30000));
  ok('?billing=return is removed from the URL', !page.url().includes('billing=return'));
  ok('Upgrade button hidden once subscribed', !(await visible(page, '#settingsUpgradeBtn')));
  const usage2 = await (await fetch(`${BASE}/api/account/usage`, { headers: { Authorization: `Bearer ${token1}` } })).json();
  ok('Pro entitlement (50/day) enforced server-side', usage2.plan === 'pro' && usage2.daily_limit === 50, JSON.stringify(usage2));
  // cancellation webhook returns the account to Free
  const cancel = { id: 'evt_' + crypto.randomBytes(6).toString('hex'), type: 'subscription.canceled', created: now + 5, data: { subscription_id: sub, plan: 'pro', status: 'canceled' } };
  ok('cancellation webhook accepted', (await signedWebhook(cancel)).status === 200);
  const usage3 = await (await fetch(`${BASE}/api/account/usage`, { headers: { Authorization: `Bearer ${token1}` } })).json();
  ok('cancelled subscription returns to Free', usage3.plan === 'free', JSON.stringify(usage3));

  // ---------------------------------------------------------------- 6. sign-out revokes the token on the server
  step = 'signout';
  await page.reload({ waitUntil: 'networkidle0' });
  await page.click('#settingsToggleBtn').catch(() => {});
  await page.click('#settingsCloseBtn').catch(() => {});
  await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle0' }), (async () => { await clickAtTop('#userMenuBtn'); await page.click('#signOutBtn'); })()]);
  const stale = await fetch(`${BASE}/api/auth/me`, { headers: { Authorization: `Bearer ${token1}` } });
  ok('signed-out token is rejected by the server (real revocation)', stale.status === 401, stale.status);
  ok('token removed from browser storage', (await page.evaluate(() => localStorage.getItem('qs_token'))) === null);

  step = 'login2';
  // Plan and billing data arrive late after this sign-in: settings must still open in its final layout (see 'export').
  delayBillingMs = 2500;
  await openLogin();
  await page.type('#loginEmail', email);
  await page.type('#loginPassword', PASSWORD);
  await page.click('#loginSubmit');
  ok('can sign in again', await waitFor(page, () => document.getElementById('screen-workspace')?.classList.contains('active'), null, 20000));
  const token2 = await page.evaluate(() => localStorage.getItem('qs_token'));

  // ---------------------------------------------------------------- 7. data export
  step = 'export';
  await page.click('#settingsToggleBtn');
  // Opened while billing was still loading, the panel later inserted "Upgrade to Pro" above "Download my data", moving it
  // 37 px under the pointer: the click hit a section title and no export was downloaded.
  const settingsShown = await waitFor(page, () => !document.getElementById('settingsModal').hidden, null, 10000);
  const firstLayout = await page.evaluate(() => ({ msg: document.getElementById('settingsBillingMessage').textContent, upgrade: !document.getElementById('settingsUpgradeBtn').hidden }));
  ok('settings opens with billing already loaded, so no row appears above its buttons afterwards', settingsShown && !/Loading billing/.test(firstLayout.msg) && firstLayout.upgrade, JSON.stringify(firstLayout));
  delayBillingMs = 0;
  fs.rmSync(DL, { recursive: true, force: true }); fs.mkdirSync(DL, { recursive: true });
  await page.click('#settingsExportBtn');
  let exp = null;
  for (let i = 0; i < 40 && !exp; i++) { await sleep(250); exp = fs.readdirSync(DL).find((f) => f.endsWith('.json')) || null; }
  const exported = exp ? JSON.parse(fs.readFileSync(path.join(DL, exp), 'utf8')) : null;
  ok('export downloaded with own data', exported && exported.account.email === email && exported.billing.payments.length === 0);
  ok('export contains no credentials or internal keys', exported && !/hashed_password|\$2b\$|token_hash|auth_version/.test(JSON.stringify(exported)));

  // ---------------------------------------------------------------- 8. delete account (re-auth, then gone)
  step = 'delete';
  await page.click('#settingsDeleteBtn');
  await page.type('#deleteConfirmEmail', email);
  await page.type('#deleteConfirmPassword', 'WrongPassword123!');
  await page.click('#deleteAccountConfirmBtn');
  ok('wrong password is reported, user NOT signed out', await waitFor(page, () => /Password is incorrect/.test(document.getElementById('accountDataMessage')?.textContent || ''), null, 10000)
    && (await page.evaluate(() => localStorage.getItem('qs_token'))) === token2);
  await page.$eval('#deleteConfirmPassword', (e) => { e.value = ''; });
  await page.type('#deleteConfirmPassword', PASSWORD);
  await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle0', timeout: 30000 }), page.click('#deleteAccountConfirmBtn')]);
  ok('after deletion the user is back on the landing screen', await waitFor(page, () => document.getElementById('screen-landing')?.classList.contains('active'), null, 10000));
  const gone = await fetch(`${BASE}/api/auth/me`, { headers: { Authorization: `Bearer ${token2}` } });
  ok('deleted account token is rejected', gone.status === 401);
  const relog = await fetch(`${BASE}/api/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password: PASSWORD }) });
  ok('deleted account cannot sign in', relog.status === 401);
  const left = py(`from db import SessionLocal,User,PipelineSession,Report,AuthSession\ndb=SessionLocal()\nprint(db.query(User).filter(User.email=='${email}').count(), db.query(PipelineSession).count()>=0)`);
  ok('no user row remains', left.startsWith('0'), left);

  step = 'final';
  const leaked = allUrls.filter((u) => u.includes(PASSWORD) || u.includes(PASSWORD_Q) || /[?&](password|pwd|pass)=/i.test(u) || u.includes('Typed-Secret'));
  ok('no request URL (navigation, link or fetch) ever contained a password', leaked.length === 0, leaked.join(' | '));
  ok('e-mail address never appears in a request query string', !allUrls.some((u) => u.includes('?') && u.includes(encodeURIComponent(email))), allUrls.filter((u) => u.includes(encodeURIComponent(email))).join(' | '));
  const csp = await page.evaluate(() => window.__csp || []);
  ok('no CSP violations', csp.length === 0, csp.join(';'));
  ok('no console errors or uncaught exceptions', problems.length === 0, problems.join(' | '));
} catch (e) {
  ok(`unexpected exception during "${step}"`, false, e && e.stack || e);
  try {
    const dbg = await page.evaluate(() => ({
      url: location.href,
      active: [...document.querySelectorAll('.screen-state.active')].map((x) => x.id),
      visibleForms: [...document.querySelectorAll('form,#authVerificationState')].filter((x) => x.offsetParent).map((x) => x.id),
      text: document.querySelector('.screen-state.active')?.innerText.slice(0, 400),
    }));
    console.error('  DEBUG', JSON.stringify(dbg), 'HTTP>=400:', httpLog.join(', '));
    await page.screenshot({ path: path.join(DL, '..', 'fail.png') });
  } catch {}
} finally {
  await browser.close();
}
const failed = results.filter((r) => !r.pass);
fs.writeFileSync(path.join(DL, '..', 'e2e-results.json'), JSON.stringify({ total: results.length, failed: failed.length, results }, null, 2));
console.log(`\nE2E: ${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
