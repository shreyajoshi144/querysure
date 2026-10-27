const STEPS = ['connect','schema','semantic','retrieval','sql','validation','execution','reliability','insights','report'];
let sessionId = '';
let lastRows = [];
let lastReport = null;
let lastStageError = '';
let currentSql = '';
let dbConnected = false;
let currentUser = { id: '', email: '' };
let currentDataset = null;
let serverLlmConfigured = false;
let apiKeyOverride = localStorage.getItem('qs_api_key') || '';
let allowClientApiKey = true; // narrowed once /api/provider-status responds
let accountUsage = null;
let billingState = null;
let datasetTables = [];
let selectedTable = null;
const previewCache = {};
let datasetEpoch = 0;          // bumped whenever a different dataset becomes active; late responses for an older one are dropped
let evaluationSummaryPromise = null;

const $ = id => document.getElementById(id);

function pretty(obj) { return JSON.stringify(obj, null, 2); }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function clamp(v, lo=0, hi=100) { return Math.max(lo, Math.min(hi, v)); }
function pctStr(v) {
  if (v === null || v === undefined || v === '—') return '—';
  if (typeof v === 'string') {
    if (v === 'High') return 'High';
    if (v === 'Medium') return 'Medium';
    if (v === 'Low') return 'Low';
    const n = parseFloat(v);
    return isNaN(n) ? v : Math.round(n) + '%';
  }
  if (typeof v === 'number') return Math.round(v) + '%';
  return '—';
}
function numFmt(n) {
  if (n === null || n === undefined) return '—';
  const parsed = typeof n === 'number' ? n : parseInt(n, 10);
  if (isNaN(parsed)) return '—';
  return parsed >= 1000 ? (parsed/1000).toFixed(1)+'K' : String(parsed);
}

// --- Auth State Core Infrastructure Integration Router Hooks ---
let authToken = localStorage.getItem('qs_token') || '';

function setAuthToken(t) {
  authToken = t || '';
  if (authToken) localStorage.setItem('qs_token', authToken);
  else localStorage.removeItem('qs_token');
}

function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  if (authToken) h['Authorization'] = 'Bearer ' + authToken;
  return h;
}

function onUnauthorized() {
  setAuthToken('');
  showAuthGate('Your session expired. Please sign in again.');
}

// Every response carries an X-Request-ID header (set server-side by
// middleware) so a failed request can show "Reference: <id>" -- something
// a user can hand back to support/logs instead of a dead-end error string.
// 429s also get special handling here in one place: the server now returns
// a concrete `retry_after_seconds` (mirrored in the Retry-After header)
// instead of the frontend having to guess a cooldown.
async function _handleResponse(r, opts) {
  const requestId = r.headers.get('X-Request-ID');
  // A 401 normally means the session is gone. Calls that re-authenticate with a password (account deletion) pass
  // keepAuthOn401 so a mistyped password is reported as an error instead of silently signing the user out.
  if (r.status === 401 && !(opts && opts.keepAuthOn401)) { onUnauthorized(); return {success:false, error:'Not authenticated.', request_id: requestId}; }
  let data;
  try { data = await r.json(); }
  catch (_) {
    // Proxy / gateway error pages and empty bodies are not JSON; never let that surface as an uncaught exception.
    data = {success:false, error: r.status >= 500 ? 'The service is temporarily unavailable. Please try again in a moment.' : 'The server sent an unexpected response.'};
  }
  if (requestId && data && typeof data === 'object') data.request_id = data.request_id || requestId;
  if (r.status === 429 && data && data.code === 'QUERY_LIMIT_REACHED') {
    // Daily quota, not rate limiting: waiting 60s will not help, so say what actually happened.
    showQuotaBanner(data);
    refreshAccountUsage();
    return data;
  }
  if (r.status === 429) {
    const retryAfter = data.retry_after_seconds || parseInt(r.headers.get('Retry-After') || '60', 10);
    showRateLimitBanner(retryAfter);
  }
  return data;
}
const NETWORK_ERROR = {success:false, error:'Network error. Check your connection and try again.'};
async function _request(url, init, opts) {
  let r;
  try { r = await fetch(url, init); } catch (_) { return Object.assign({}, NETWORK_ERROR); }
  return _handleResponse(r, opts);
}
async function post(url, body, opts) {
  return _request(url, {method:'POST', headers: authHeaders({'Content-Type':'application/json'}), body: JSON.stringify(body)}, opts);
}
async function postForm(url, fd) {
  return _request(url, {method:'POST', headers: authHeaders(), body: fd});
}
async function apiGet(url) {
  return _request(url, {headers: authHeaders()});
}

function extractErrorMessage(data, fallback) {
  if (!data) return fallback;
  if (typeof data.error === 'string') return data.error;
  if (typeof data.detail === 'string') return data.detail;
  if (Array.isArray(data.detail)) return data.detail.map(d => d.msg || JSON.stringify(d)).join(' ');
  return fallback;
}

// Router states transitions configuration
function switchScreen(screenId) {
  document.querySelectorAll('.screen-state').forEach(el => el.classList.remove('active'));
  $(screenId).classList.add('active');
}

function setCurrentUser(me, methods) {
  currentUser = {
    id: me.id || '',
    email: me.email,
    googleLinked: !!(methods && methods.google_linked),
    hasPassword: !methods || methods.has_password !== false,
  };
}

// ---- Google Identity Services: loaded and initialised once; every button shares one dispatcher so the
// sign-in, "link Google" and "confirm deletion" buttons can be rendered at different times without
// re-initialising GIS (which Google warns against).
let googleAuthConfig = null;
let googleReady = null;
let googleCredentialHandler = null;

function loadGoogleIdentity() {
  if (googleReady) return googleReady;
  googleReady = (async () => {
    let cfg;
    try {
      const r = await fetch('/api/auth/google/config');
      cfg = await r.json();
    } catch (_) { return { state: 'unavailable' }; }
    googleAuthConfig = cfg;
    if (!cfg.enabled || !cfg.client_id) return { state: 'unconfigured' };
    try {
      await new Promise((resolve, reject) => {
        if (window.google?.accounts?.id) return resolve();
        const existing = document.querySelector('script[data-google-gis]');
        const script = existing || document.createElement('script');
        script.addEventListener('load', resolve, { once: true });
        script.addEventListener('error', reject, { once: true });
        if (!existing) {
          script.src = 'https://accounts.google.com/gsi/client';
          script.async = true;
          script.defer = true;
          script.dataset.googleGis = 'true';
          document.head.appendChild(script);
        }
      });
      window.google.accounts.id.initialize({
        client_id: cfg.client_id,
        callback: (response) => { if (googleCredentialHandler) googleCredentialHandler(response); },
        auto_select: false,
        cancel_on_tap_outside: true,
        context: 'signin',
      });
    } catch (_) { return { state: 'unavailable' }; }
    return { state: 'ready' };
  })();
  return googleReady;
}

// Renders Google's button into `host` and routes its credential to `handler`. Returns true when a button is showing.
async function mountGoogleButton(host, handler, text) {
  if (!host) return false;
  host.classList.remove('google-unconfigured');
  host.textContent = 'Loading Google sign-in…';
  const { state } = await loadGoogleIdentity();
  if (state === 'unconfigured') {
    host.textContent = 'Google sign-in is not set up for this deployment.';
    host.classList.add('google-unconfigured');
    return false;
  }
  if (state === 'unavailable') {
    googleReady = null; // let the next attempt retry instead of caching the failure
    host.textContent = 'Google sign-in could not load. Check your connection and try again.';
    host.classList.add('google-unconfigured');
    return false;
  }
  googleCredentialHandler = handler;
  host.textContent = '';
  // GIS only accepts widths of 200-400px; anything else makes it refuse to draw the button.
  const width = Math.max(200, Math.min(400, Math.round(host.clientWidth || 360)));
  window.google.accounts.id.renderButton(host, {
    type: 'standard', theme: 'outline', size: 'large', text: text || 'continue_with',
    shape: 'rectangular', width, logo_alignment: 'left',
  });
  return true;
}

function showAuthError(message) {
  const err = $('authError');
  if (message) { err.textContent = message; err.classList.add('visible'); }
  else { err.textContent = ''; err.classList.remove('visible'); }
}

async function onGoogleSignInCredential(response) {
  try {
    const r = await fetch('/api/auth/google', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({credential: response.credential})
    });
    let data = {};
    try { data = await r.json(); } catch (_) { /* handled below */ }
    if (!r.ok || !data.access_token) throw new Error(extractErrorMessage(data, 'Google sign-in could not be completed. Please try again.'));
    setAuthToken(data.access_token);
    resetAuthForms();
    await bootApp();
  } catch (err) {
    showAuthError(err.message);
  }
}

const AUTH_HEADINGS = {
  login: 'Sign in',
  register: 'Create your account',
  forgotPassword: 'Reset your password',
  resetPassword: 'Choose a new password',
};

// One place that decides which auth form is showing, so the tabs, heading and Google block never disagree.
function activateAuthForm(name) {
  hideVerificationState();
  document.querySelectorAll('.auth-tab').forEach(t => t.classList.toggle('active', t.dataset.form === name));
  document.querySelectorAll('.auth-form').forEach(f => f.classList.toggle('active', f.id === name + 'Form'));
  $('authHeading').textContent = AUTH_HEADINGS[name] || AUTH_HEADINGS.login;
  syncAuthFieldEligibility();
  const tabs = document.querySelector('.auth-tabs');
  if (tabs) tabs.hidden = !(name === 'login' || name === 'register');
  showAuthError('');
}

// Browser password managers fill every credential-looking field they can find, including fields of forms that are not on
// screen (sign-up, reset, delete-account). Those values then "appear" when the person clicks a link that reveals the form.
// Only the visible form's fields are enabled, and a disabled field is never autofilled. Values are scrubbed when a form is
// deactivated so nothing a password manager dropped in survives the switch.
function syncAuthFieldEligibility() {
  document.querySelectorAll('#screen-auth .auth-form').forEach(f => {
    const active = f.classList.contains('active');
    f.querySelectorAll('input').forEach(i => {
      if (!active && !i.disabled) { i.value = ''; updatePasswordPolicy(i); }
      i.disabled = !active;
    });
  });
}
function setDeleteFieldsEligible(on) {
  ['deleteConfirmEmail', 'deleteConfirmPassword'].forEach(id => { const el = $(id); if (el) { if (!on) el.value = ''; el.disabled = !on; } });
}

// Password rules shown beside the registration / reset fields. These mirror the server's validator (auth.py): 12+ characters,
// upper and lower case, a number and a special character. The server stays the authority; this is feedback only.
const PASSWORD_RULES = {
  length: v => v.length >= 12,
  upper: v => /[A-Z]/.test(v),
  lower: v => /[a-z]/.test(v),
  number: v => /[0-9]/.test(v),
  special: v => /[^A-Za-z0-9]/.test(v),
};
function updatePasswordPolicy(input) {
  const policy = document.querySelector(`.pw-policy[data-policy-for="${input.id}"]`);
  if (!policy) return;
  const value = input.value || '';
  let met = 0;
  policy.querySelectorAll('li[data-rule]').forEach(li => {
    const ok = !!(PASSWORD_RULES[li.dataset.rule] && PASSWORD_RULES[li.dataset.rule](value));
    li.classList.toggle('is-met', ok);
    if (ok) met += 1;
  });
  policy.dataset.score = String(met);
  policy.classList.toggle('has-value', value.length > 0);
}
document.querySelectorAll('.pw-policy[data-policy-for]').forEach(policy => {
  const input = document.getElementById(policy.dataset.policyFor);
  if (input) input.addEventListener('input', () => updatePasswordPolicy(input));
});

// Clears every credential field. Called whenever the auth screen is entered from elsewhere, left, or restored from the
// browser's back/forward cache, so a previous person's e-mail or password is never sitting in the form (typed values used
// to survive Back -> Get started, tab switches and bfcache restores).
function resetAuthForms() {
  document.querySelectorAll('#screen-auth form').forEach(f => f.reset());
  document.querySelectorAll('#screen-auth .field input').forEach(input => {
    input.value = '';
    if (input.dataset.pwInput !== undefined || input.closest('.field')?.querySelector('.pw-toggle')) input.type = 'password';
    updatePasswordPolicy(input);
  });
  document.querySelectorAll('#screen-auth .pw-toggle').forEach(btn => {
    btn.textContent = 'Show';
    btn.setAttribute('aria-pressed', 'false');
    btn.setAttribute('aria-label', 'Show password');
  });
}
window.addEventListener('pageshow', () => resetAuthForms());

function showAuthGate(message) {
  const alreadyOpen = $('screen-auth').classList.contains('active');
  if (!alreadyOpen) { resetAuthForms(); activateAuthForm('login'); }
  switchScreen('screen-auth');
  // Google's block stays hidden unless sign-in is actually configured and loaded; end users are not shown deployment notes.
  const social = $('googleAuthBtn');
  social.classList.add('is-unavailable');
  mountGoogleButton($('googleButtonInner'), onGoogleSignInCredential, 'continue_with')
    .then(ok => social.classList.toggle('is-unavailable', !ok));
  showAuthError(message || '');
}

function hideAuthGate() {
  switchScreen('screen-workspace');
}

// FIX: "Flush Session" previously created a brand-new (empty) backend
// session without resetting any of the workspace UI, so the pipeline
// still believed a database was connected (it only checked a status
// text string) and later steps like schema-intelligence / generate-sql
// failed against the new, empty session. This puts the UI and the
// `dbConnected` flag back in sync with whatever backend session is
// actually active.


// FIX: pipeline errors used to be written only into the JSON payload tab,
// so a failed run silently looked like nothing happened unless the user
// happened to click into "JSON Payload Core". This surfaces a visible,
// dismissible banner above the query composer for any pipeline failure.
let _rateLimitCountdownTimer = null;

function showGlobalError(message, requestId) {
  const banner = $('globalErrorBanner');
  $('globalErrorText').textContent = requestId ? `${message} (Reference: ${requestId})` : message;
  banner.classList.add('visible');
}
function clearGlobalError() {
  $('globalErrorBanner').classList.remove('visible');
  $('globalErrorText').textContent = '';
  if (_rateLimitCountdownTimer) { clearInterval(_rateLimitCountdownTimer); _rateLimitCountdownTimer = null; }
}

// Distinct messaging for rate limiting vs. everything else: a 429 is not a
// real failure, it's "slow down" -- the session and any generated SQL are
// still completely fine, so this shouldn't look like a pipeline error.
// Shows a live countdown driven by the server's retry_after_seconds instead
// of a static guess.
function showRateLimitBanner(seconds) {
  if (_rateLimitCountdownTimer) clearInterval(_rateLimitCountdownTimer);
  let remaining = Math.max(1, Math.round(seconds));
  const banner = $('globalErrorBanner');
  const render = () => { $('globalErrorText').textContent = `You're moving a little fast. Try again in ${remaining}s.`; };
  render();
  banner.classList.add('visible');
  _rateLimitCountdownTimer = setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) { clearInterval(_rateLimitCountdownTimer); _rateLimitCountdownTimer = null; clearGlobalError(); return; }
    render();
  }, 1000);
}

function showQuotaBanner(q) {
  if (_rateLimitCountdownTimer) { clearInterval(_rateLimitCountdownTimer); _rateLimitCountdownTimer = null; }
  const limit = q.daily_limit == null ? '' : `${q.used_today}/${q.daily_limit} `;
  let msg = `Daily query limit reached (${limit}on the ${q.plan || 'current'} plan). It resets at 00:00 UTC.`;
  if (q.plan === 'free' && billingState && billingState.billing_enabled) msg += ' Upgrade to Pro in Settings for a higher limit.';
  $('globalErrorText').textContent = msg;
  $('globalErrorBanner').classList.add('visible');
}

// FIX: nothing previously stopped a user from clicking Sample DB / Upload /
// Flush Session while a pipeline run was already in flight against the
// same session, which could interleave two requests against one session
// row and leave the UI showing a mix of both. Locks the competing controls
// for the duration of runAll().
// ---- Views: Ask, Datasets, Evaluation
function switchView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
  document.querySelectorAll('.nav-tab[data-view]').forEach(b => {
    const on = b.dataset.view === name;
    b.classList.toggle('active', on);
    if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  if (name === 'evaluation') loadWorkspaceEvaluation(false);
  if (name === 'history') loadHistory(false);
  window.scrollTo({top: 0, behavior: 'auto'});
}

function fmtInt(n) {
  const v = typeof n === 'number' ? n : parseInt(n, 10);
  return Number.isFinite(v) ? v.toLocaleString() : '—';
}

function countRelationships(tables) {
  return (tables || []).reduce((sum, t) => sum + ((t.foreign_keys || []).length), 0);
}

// Keeps every dataset-dependent surface in step: status pill, Ask empty state, suggestion chips, Dataset Explorer.
function updateDatasetUI() {
  const connected = !!(dbConnected && currentDataset);
  const sheetVisible = !$('answerSheet').classList.contains('hidden-element');
  $('chipsRow').hidden = !(connected && currentDataset.sample);
  $('emptyState').style.display = sheetVisible ? 'none' : '';
  if (connected) {
    $('emptyTitle').textContent = 'Ask your first question';
    $('emptyBody').textContent = `${currentDataset.name} is connected. Type a question above, or pick one of the suggestions.`;
    $('emptyActions').hidden = true;
  } else {
    $('emptyTitle').textContent = 'Connect a dataset to begin';
    $('emptyBody').textContent = 'Use the bundled retail sample to try QuerySure right away, or upload your own CSV, JSON, Excel or SQLite file.';
    $('emptyActions').hidden = false;
  }
  $('dsEmpty').classList.toggle('hidden-element', connected);
  $('dsLayout').classList.toggle('hidden-element', !connected);
  $('dsStats').hidden = !connected;
  $('dsSubtitle').textContent = connected
    ? `${currentDataset.name}. Browse tables, columns, keys and a safe preview of the connected data.`
    : 'Browse tables, columns, keys and a safe preview of the connected data.';
  if (connected) {
    $('dbTables').textContent = currentDataset.tables ?? '—';
    $('dbRows').textContent = numFmt(currentDataset.rows);
    $('dbSize').textContent = currentDataset.size || '—';
    $('dbRels').textContent = countRelationships(datasetTables);
  } else {
    // A flushed or disconnected workspace must not keep the previous dataset's numbers, even hidden: a stale table count
    // reads as "a dataset is connected" while the next connect is still in flight.
    ['dbTables', 'dbRows', 'dbSize', 'dbRels'].forEach(id => { $(id).textContent = '—'; });
  }
}

// ---- Dataset Explorer
function renderSchemaList(tables) {
  datasetTables = Array.isArray(tables) ? tables : [];
  selectedTable = null;
  Object.keys(previewCache).forEach(k => { delete previewCache[k]; });
  const list = $('schemaList');
  if (!datasetTables.length) { list.innerHTML = ''; $('dsDetail').innerHTML = ''; updateDatasetUI(); return; }
  list.innerHTML = datasetTables.map(t => {
    const cols = t.columns || [];
    const hasPk = cols.some(c => c.pk);
    const fks = (t.foreign_keys || []).length;
    const meta = [`${cols.length} column${cols.length === 1 ? '' : 's'}`, hasPk ? 'primary key' : 'no primary key', fks ? `${fks} foreign key${fks === 1 ? '' : 's'}` : ''].filter(Boolean).join(' · ');
    return `<button type="button" class="ds-table-btn" data-table="${escapeHtml(t.name)}"><span class="ds-table-name">${escapeHtml(t.name)}</span><span class="ds-table-rows">${fmtInt(t.rows)}</span><span class="ds-table-meta">${escapeHtml(meta)}</span></button>`;
  }).join('');
  updateDatasetUI();
  selectTable(datasetTables[0].name);
}

function relationshipsFor(name) {
  const table = datasetTables.find(t => t.name === name);
  const outgoing = ((table && table.foreign_keys) || []).map(fk => ({ column: fk.from, table: fk.to_table, to: fk.to_col }));
  const incoming = [];
  datasetTables.forEach(t => (t.foreign_keys || []).forEach(fk => {
    if (fk.to_table === name) incoming.push({ table: t.name, column: fk.from, to: fk.to_col });
  }));
  return { outgoing, incoming };
}

function selectTable(name) {
  const table = datasetTables.find(t => t.name === name);
  if (!table) return;
  selectedTable = name;
  document.querySelectorAll('.ds-table-btn').forEach(b => {
    const on = b.dataset.table === name;
    b.classList.toggle('selected', on);
    if (on) b.setAttribute('aria-current', 'true'); else b.removeAttribute('aria-current');
  });
  const fkByCol = {};
  (table.foreign_keys || []).forEach(fk => { fkByCol[fk.from] = fk; });
  const colRows = (table.columns || []).map(c => {
    const fk = fkByCol[c.name];
    const keys = (c.pk ? '<span class="key-badge key-pk">PK</span>' : '') +
      (fk ? `<span class="key-badge key-fk">FK</span><span class="muted">${escapeHtml(fk.to_table)}.${escapeHtml(fk.to_col)}</span>` : '');
    return `<tr><td>${escapeHtml(c.name)}</td><td class="type">${escapeHtml(c.type || 'unknown')}</td><td>${keys || '<span class="muted">—</span>'}</td></tr>`;
  }).join('');
  const rel = relationshipsFor(name);
  const link = (t) => `<button type="button" class="rel-link" data-goto="${escapeHtml(t)}">${escapeHtml(t)}</button>`;
  const relItems = rel.outgoing.map(r => `<li><span class="rel-link" style="border-style:dashed">${escapeHtml(name)}.${escapeHtml(r.column)}</span><span class="rel-arrow">→</span>${link(r.table)}<span class="rel-link" style="border-style:dashed">${escapeHtml(r.to)}</span><span class="rel-kind">references</span></li>`)
    .concat(rel.incoming.map(r => `<li>${link(r.table)}<span class="rel-link" style="border-style:dashed">${escapeHtml(r.column)}</span><span class="rel-arrow">→</span><span class="rel-link" style="border-style:dashed">${escapeHtml(name)}.${escapeHtml(r.to)}</span><span class="rel-kind">referenced by</span></li>`));
  $('dsDetail').innerHTML = `
    <div class="ds-card"><div class="ds-head"><h2>${escapeHtml(name)}</h2><span>${fmtInt(table.rows)} rows · ${(table.columns || []).length} columns</span></div></div>
    <div class="ds-card"><h3>Columns</h3><div style="overflow-x:auto"><table class="col-table"><thead><tr><th>Name</th><th>Type</th><th>Keys</th></tr></thead><tbody>${colRows || '<tr><td colspan="3" class="muted">No columns were profiled for this table.</td></tr>'}</tbody></table></div></div>
    <div class="ds-card"><h3>Relationships</h3>${relItems.length ? `<ul class="rel-list">${relItems.join('')}</ul>` : '<p class="muted">This table has no declared foreign keys and no other table references it.</p>'}</div>
    <div class="ds-card"><h3>Preview</h3><div id="dsPreview"><p class="muted">Loading the first rows…</p></div></div>`;
  loadPreview(name);
}

function renderPreview(name, data) {
  const host = $('dsPreview');
  if (!host || selectedTable !== name) return;
  if (!data || data.success === false) {
    host.innerHTML = `<p class="settings-detail error">${escapeHtml((data && data.error) || 'This table could not be previewed.')}</p>`;
    return;
  }
  const cols = data.columns || [];
  const rows = data.rows || [];
  if (!rows.length) { host.innerHTML = '<p class="muted">This table has no rows.</p>'; return; }
  const numeric = cols.map(c => rows.every(r => r[c] === null || r[c] === undefined || typeof r[c] === 'number'));
  const head = cols.map((c, i) => `<th class="${numeric[i] ? 'num' : ''}">${escapeHtml(c)}</th>`).join('');
  const body = rows.map(r => '<tr>' + cols.map((c, i) => {
    const v = r[c];
    if (v === null || v === undefined) return '<td class="null">null</td>';
    if (typeof v === 'number') return `<td class="num">${v.toLocaleString(undefined, {maximumFractionDigits: 6})}</td>`;
    return `<td title="${escapeHtml(v)}">${escapeHtml(v)}</td>`;
  }).join('') + '</tr>').join('');
  const total = typeof data.total_rows === 'number' ? ` of ${fmtInt(data.total_rows)}` : '';
  const page = data.page || 1, pages = data.total_pages || 1, size = data.page_size || rows.length;
  const first = (page - 1) * size + 1;
  const sizeOpts = [10, 25, 50].map(n => `<option value="${n}"${n === size ? ' selected' : ''}>${n}</option>`).join('');
  host.innerHTML = `<div class="table-wrapper"><table class="premium-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>
    <div class="preview-pager">
      <p class="preview-note">Rows ${fmtInt(first)}–${fmtInt(first + rows.length - 1)}${total}. Read-only preview; it does not use your daily queries.</p>
      <div class="pager-controls">
        <label>Rows per page <select id="dsPageSize" aria-label="Rows per page">${sizeOpts}</select></label>
        <button type="button" class="btn-secondary" id="dsPrev" ${page <= 1 ? 'disabled' : ''} aria-label="Previous page">Previous</button>
        <span class="pager-pos" aria-live="polite">Page ${fmtInt(page)} of ${fmtInt(pages)}</span>
        <button type="button" class="btn-secondary" id="dsNext" ${page >= pages ? 'disabled' : ''} aria-label="Next page">Next</button>
      </div>
    </div>`;
  const go = (p, sz) => loadPreview(name, p, sz);
  const prev = $('dsPrev'), next = $('dsNext'), sel = $('dsPageSize');
  if (prev) prev.addEventListener('click', () => go(page - 1, size));
  if (next) next.addEventListener('click', () => go(page + 1, size));
  if (sel) sel.addEventListener('change', () => go(1, parseInt(sel.value, 10) || 10));
}

async function loadPreview(name, page = 1, pageSize = 10) {
  const key = `${name}|${page}|${pageSize}`;
  if (previewCache[key]) { renderPreview(name, previewCache[key]); return; }
  if (!sessionId) { renderPreview(name, {success: false, error: 'No active session.'}); return; }
  const epoch = datasetEpoch;
  const res = await apiGet(`/api/dataset/preview?session_id=${encodeURIComponent(sessionId)}&table=${encodeURIComponent(name)}&page=${page}&page_size=${pageSize}`);
  if (epoch !== datasetEpoch) return;   // the dataset changed while this request was in flight
  if (res && res.success !== false) previewCache[key] = res;
  renderPreview(name, res);
}

// ---- Answer sheet
function setResultView(view) {
  const chartOk = $('resultSeg').dataset.chart === '1';
  const showChart = chartOk && view === 'chart';
  $('chartArea').classList.toggle('hidden-element', !showChart);
  $('tableWrap').classList.toggle('hidden-element', showChart);
  document.querySelectorAll('#resultSeg button').forEach(b => b.classList.toggle('active', b.dataset.resultView === (showChart ? 'chart' : 'table')));
}

function clearSheet() {
  $('sheetQuestion').textContent = '';
  $('execNote').textContent = 'The answer appears here once the query has run.';
  ['kpiConfidence', 'kpiReliability', 'kpiRows', 'kpiExec', 'relScore'].forEach(id => { $(id).textContent = '—'; });
  $('relLevel').textContent = 'Not assessed yet';
  $('relStatus').textContent = 'Run a query to assess the data';
  $('relArc').style.strokeDashoffset = '175.9';
  $('relChecks').innerHTML = '';
  $('findingsList').innerHTML = '<div class="bullet-note-node"><span class="bullet-mark">·</span><span>Nothing to report yet.</span></div>';
  $('anomaliesList').innerHTML = '<div class="bullet-note-node"><span class="bullet-mark">·</span><span>Nothing flagged.</span></div>';
  $('recoList').innerHTML = '<div class="bullet-note-node"><span class="bullet-mark">·</span><span>No follow-ups suggested.</span></div>';
  $('sqlOut').textContent = 'The generated SQL appears here.';
  $('sqlMetaTags').innerHTML = '';
  $('sqlExplanation').hidden = true;
  $('sqlExplanation').textContent = '';
  $('valContent').innerHTML = '<p class="muted">Appears after the SQL is validated.</p>';
  $('retrievalContent').innerHTML = '<p class="muted">Appears after a question is asked.</p>';
  $('trustTraceContent').innerHTML = '<div class="trust-trace-empty">Run a successful query to view how the answer was produced.</div>';
  $('rawOut').textContent = 'Responses appear here as each step completes.';
  $('resultsState').style.display = 'none';
  $('resultsState').classList.add('hidden-element');
  $('chartArea').classList.add('hidden-element');
  $('tableWrap').classList.remove('hidden-element');
  $('resultSeg').hidden = true;
  $('resultSeg').dataset.chart = '0';
  $('resultsTable').querySelector('thead').innerHTML = '';
  $('resultsTable').querySelector('tbody').innerHTML = '';
  stageInfo = {}; selectedStage = null; runQuestion = null; lastTrace = null; refreshStages();
  resetDetailFolds();
  lastRows = []; lastReport = null; currentSql = '';
}

// A newly activated dataset must never show the previous dataset's question, answer, SQL, results or verification.
function resetAnswerState() {
  clearSheet();
  $('answerSheet').classList.add('hidden-element');
  const q = $('question');
  if (q) q.value = '';
  setPipelineState('Idle');
}

function onDatasetActivated() {
  datasetEpoch += 1;
  resetAnswerState();
}

function resetWorkspaceUI() {
  datasetEpoch += 1;
  dbConnected = false;
  setStatus('No dataset connected', false);
  currentDataset = null;
  datasetTables = []; selectedTable = null;
  $('schemaList').innerHTML = '';
  $('dsDetail').innerHTML = '';
  clearSheet();
  $('answerSheet').classList.add('hidden-element');
  $('runProgress').hidden = true;
  setPipelineState('Idle');
  clearGlobalError();
  setBar('usageBarFill', 0);
  updateDatasetUI();
}

// Locks the competing controls while a run is in flight, so two requests never interleave on one session row.
function setPipelineBusy(busy) {
  pipelineIsBusy = !!busy; refreshStages();
  ['analyzeBtn', 'sampleBtn', 'fileInput', 'newSessionBtn', 'emptySampleBtn', 'dsSampleBtn'].forEach(id => { const el = $(id); if (el) el.disabled = busy; });
}

// ---- Evaluation: real benchmark metadata and real stored results only; nothing is computed or invented here.
function fetchEvaluationSummary(force) {
  if (!evaluationSummaryPromise || force) {
    evaluationSummaryPromise = fetch('/api/evaluation/product-summary', { headers: { 'Accept': 'application/json' } })
      .then(r => (r.ok ? r.json() : null))
      .then(d => (d && d.success ? d : null))
      .catch(() => null);
  }
  return evaluationSummaryPromise;
}

const EVAL_METRIC_LABELS = [
  ['result_accuracy', 'Result accuracy', 'Answerable cases whose executed result matches the expected result.'],
  ['sql_execution_success', 'SQL execution success', 'Answerable cases whose validated SQL ran without a database error.'],
  ['schema_grounding', 'Schema grounding', 'Share of expected columns the generated SQL actually used.'],
  ['hallucinated_identifiers', 'Hallucinated identifiers', 'Share of table or column references that do not exist. Lower is better.'],
  ['correct_rejection', 'Correct rejection', 'Questions that should be declined and were declined (strict).'],
];

function barRows(counts, total) {
  const entries = Object.entries(counts || {});
  if (!entries.length) return '<p class="muted">Not available.</p>';
  const max = Math.max(...entries.map(([, n]) => n), 1);
  return `<div class="bars">${entries.sort((a, b) => b[1] - a[1]).map(([label, n]) =>
    `<div class="bar-row" title="${escapeHtml(label)}: ${n} of ${total}"><span>${escapeHtml(label)}</span><i style="--w:${(n / max * 100).toFixed(1)}%"></i><b>${n}</b></div>`).join('')}</div>`;
}

function renderWorkspaceEvaluation(data) {
  const body = $('wsEvalBody');
  const b = data && data.benchmark;
  if (!b || !b.available) {
    body.innerHTML = '<div class="eval-card"><h3>Benchmark</h3><p class="muted">The benchmark definition is not available on this deployment, so there is nothing to show yet.</p></div>';
    return;
  }
  const cats = b.categories || [];
  const hasMetrics = data.evaluated_cases > 0 && Object.values(data.metrics || {}).some(v => typeof v === 'number');
  const pct = v => `${(v * 100).toFixed(1)}%`;
  let html = `<div class="eval-card"><div class="eval-hero">
      <div class="eval-big">${fmtInt(b.cases)}<small>benchmark cases</small></div>
      <p>A fixed set of questions with known-good SQL against the retail sample database, across ${cats.length} categories${b.version ? ` (version ${escapeHtml(String(b.version))})` : ''}. Answerable cases are scored by comparing executed results, not SQL text.</p>
    </div></div>
    <div class="eval-cols">
      <div class="eval-card"><h3>Cases by category</h3>${barRows(b.category_counts, b.cases)}</div>
      <div class="eval-card"><h3>Cases by difficulty</h3>${barRows(b.difficulty_counts, b.cases)}<h3 style="margin-top:22px">Expected behavior</h3>${barRows(b.behavior_counts, b.cases)}</div>
    </div>`;
  if (hasMetrics) {
    html += `<div class="eval-card"><h3>Latest live run · ${fmtInt(data.evaluated_cases)} cases</h3><div class="eval-metrics">${EVAL_METRIC_LABELS
      .filter(([k]) => typeof data.metrics[k] === 'number')
      .map(([k, label, hint]) => `<div class="metric-tile" title="${escapeHtml(hint)}"><span>${label}</span><strong>${pct(data.metrics[k])}</strong></div>`).join('')}</div>
      <p class="preview-note">Measured on the cases above by a live run of the evaluation harness. Hover a tile for its definition.</p></div>`;
  } else {
    html += `<div class="eval-card"><h3>Measured results</h3><p class="eval-note">No live evaluation run is recorded on this deployment, so no accuracy figures are shown. QuerySure never displays a score that a run did not produce.</p>
      <p class="preview-note">A live run calls your configured model provider and costs money. From the repository:</p><div class="eval-cli">python -m evaluation live</div></div>`;
  }
  const de = data.deepeval || {};
  const syn = data.synthetic || {};
  const deMetrics = de.status === 'available' ? Object.entries(de.metrics || {}).filter(([, v]) => typeof v === 'number') : [];
  html += `<div class="eval-card"><h3>Supplementary layers</h3><div class="eval-status-list">
      <div class="eval-status"><strong>Deterministic benchmark</strong><span>${hasMetrics ? `${fmtInt(data.evaluated_cases)} cases evaluated` : 'Not run'}</span><small>The source of truth for correctness.</small></div>
      <div class="eval-status"><strong>DeepEval</strong><span>${de.status === 'available' ? `${fmtInt(de.cases)} cases evaluated` : 'Not run'}</span><small>${deMetrics.length ? deMetrics.map(([k, v]) => `${escapeHtml(k.replaceAll('_', ' '))}: ${typeof v === 'number' && v <= 1 ? pct(v) : v}`).join(' · ') : 'Supplementary model-judge signals, available only when a run exists.'}</small></div>
      <div class="eval-status"><strong>Synthetic coverage</strong><span>${syn.status === 'available' ? `${fmtInt(syn.cases)} cases` : 'Not run'}</span><small>Additional generated cases, available only when generated.</small></div>
    </div></div>
    <p class="eval-note">The Answer Trace on each query explains one answer. This page measures the system against a fixed benchmark. They answer different questions.</p>`;
  body.innerHTML = html;
}

async function loadWorkspaceEvaluation(force) {
  if (!force && $('wsEvalBody').dataset.loaded === '1') return;
  $('wsEvalBody').innerHTML = '<p class="muted">Loading benchmark summary…</p>';
  const data = await fetchEvaluationSummary(!!force);
  if (!data) { $('wsEvalBody').innerHTML = '<div class="eval-card"><p class="muted">The evaluation summary could not be loaded. Try Refresh.</p></div>'; return; }
  $('wsEvalBody').dataset.loaded = '1';
  renderWorkspaceEvaluation(data);
}


async function doLogin(email, password) {
  const r = await fetch('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({email, password})});
  const data = await r.json();
  if (!r.ok || !data.access_token) throw new Error(extractErrorMessage(data, 'Incorrect email or password.'));
  return data.access_token;
}

async function doRegister(email, password) {
  const r = await fetch('/api/auth/register', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({email, password})});
  const data = await r.json();
  if (!r.ok || !data.id) throw new Error(extractErrorMessage(data, 'Registration failed.'));
  return data;
}

async function doVerifyEmail(token) {
  const r = await fetch('/api/auth/verify-email?token=' + encodeURIComponent(token), {method:'POST'});
  let data = null;
  try { data = await r.json(); } catch (_) { /* Controlled user-facing fallback below. */ }
  if (!r.ok || !data?.success) {
    const fallback = r.status >= 500
      ? 'Verification could not be completed. Please try again.'
      : data?.error_code === 'EXPIRED_VERIFICATION_TOKEN'
        ? 'Verification link expired. Please request a new verification email.'
        : 'This verification link is invalid or has already been used.';
    throw new Error(extractErrorMessage(data, fallback));
  }
  return data;
}

async function doForgotPassword(email) {
  const r = await fetch('/api/auth/forgot-password', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({email})});
  const data = await r.json();
  if (!r.ok) throw new Error(extractErrorMessage(data, 'Unable to process password reset.'));
  return data;
}

async function doResetPassword(token, password) {
  const r = await fetch('/api/auth/reset-password?token=' + encodeURIComponent(token), {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password})});
  const data = await r.json();
  if (!r.ok || !data.success) throw new Error(extractErrorMessage(data, 'Password reset link is invalid or expired.'));
  return data;
}

async function doResendVerification(email) {
  const r = await fetch('/api/auth/resend-verification', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({email})});
  const data = await r.json();
  if (!r.ok) throw new Error(extractErrorMessage(data, 'Unable to resend verification.'));
  return data;
}

async function bootApp() {
  hideAuthGate();
  const me = await apiGet('/api/auth/me');
  if (me && me.email) setCurrentUser(me, await apiGet('/api/auth/methods'));
  refreshSettingsView();
  resetWorkspaceUI();
  await newSession();
  refreshUsage();
  refreshAccountUsage();
  refreshBillingState();
  refreshProviderStatus();
}

// Site routing. One canonical home per topic:
//  - product, how-it-works, evaluation, pricing, security and faq exist ONLY as sections of the landing page (index.html).
//    Their routes are deep links: the server serves the landing page and the router scrolls to the section. They used to be
//    re-implemented here as separate script-built pages with different copy, a different logo and a second footer.
//  - documentation, contact, privacy and terms have no landing section, so they render as their own page below.
const SECTION_ROUTES = {
  '/product': 'features',
  '/how-it-works': 'how-it-works',
  '/evaluation': 'evaluation',
  '/pricing': 'pricing',
  '/security': 'security',
  '/faq': 'faq',
};
const PAGE_ROUTES = new Set(['/documentation', '/contact', '/privacy', '/terms']);
const SITE_ROUTES = new Set([...Object.keys(SECTION_ROUTES), ...PAGE_ROUTES]);
const SITE_PAGE_META = {
  '/documentation': {kicker:'DOCUMENTATION', title:'Understand the product before you use it.', intro:'A concise guide to the existing QuerySure workflow, trust information, and usage model. This is product documentation, not an API reference.'},
  '/contact': {kicker:'CONTACT', title:'Questions, feedback, or product inquiries?', intro:'Use the product contact mechanism configured for this deployment.'},
  '/privacy': {kicker:'PRIVACY', title:'How QuerySure handles application data.', intro:'This product page describes only behavior supported by the current application. It is not a substitute for legal advice or a separately maintained legal policy.'},
  '/terms': {kicker:'TERMS OF SERVICE', title:'Terms for using QuerySure.', intro:'Conservative product terms based on the current application. This page requires legal review before being treated as authoritative legal text.'}
};

// The standalone pages reuse the landing page's own header and footer (cloned, ids stripped) so there is one implementation.
function cloneLandingChrome(selector) {
  const src = document.querySelector('#screen-landing ' + selector);
  if (!src) return '';
  const clone = src.cloneNode(true);
  clone.removeAttribute('id');
  clone.classList.remove('is-scrolled');
  clone.querySelectorAll('[id]').forEach(el => el.removeAttribute('id'));
  clone.querySelectorAll('.nav-link.is-active').forEach(el => el.classList.remove('is-active'));
  return clone.outerHTML;
}
function sitePageFooter() { return cloneLandingChrome('footer.site-footer'); }
function sitePageHeader() { return cloneLandingChrome('nav.nav-container'); }

function sitePageBody(route) {
  const workflow = `<div class="site-page-flow"><div><span>01</span><strong>Question</strong><small>Natural-language intent</small></div><i>→</i><div><span>02</span><strong>Ground</strong><small>Schema + semantic context</small></div><i>→</i><div><span>03</span><strong>Generate</strong><small>SQL from grounded context</small></div><i>→</i><div><span>04</span><strong>Validate</strong><small>AST + read-only checks</small></div><i>→</i><div><span>05</span><strong>Execute</strong><small>Connected data result</small></div><i>→</i><div><span>06</span><strong>Explain</strong><small>Result + reliability + trace</small></div></div>`;
  const content = {
    '/documentation': `<div class="site-page-docs"><section><span>GETTING STARTED</span><h2>Four steps into the existing workspace.</h2><ol><li>Create an account or sign in.</li><li>Connect or use supported data through the existing workspace.</li><li>Ask a natural-language question.</li><li>Inspect the result and recorded path.</li></ol></section><section><span>QUERY PIPELINE</span><h2>Question → Ground → Generate → Validate → Execute → Explain</h2>${workflow}</section><section><span>TRUST / TRACE</span><h2>What the recorded path represents.</h2><p>The trace follows question, grounding, SQL, validation, execution, reliability, and answer. Validation means the existing SQL/read-only safeguards ran; reliability information reflects existing signals.</p></section><section><span>PLANS &amp; USAGE</span><h2>Daily capacity is explicit.</h2><p>Free: 10 queries/day · Pro: 50 queries/day. Business is arranged manually through the contact address. Team (shared workspaces and seats) does not exist yet. Upgrades are available only where the deployment has billing enabled.</p></section></div>`,
    '/contact': `<div class="site-page-contact"><div class="site-page-contact-copy"><span>CONTACT QUERY SURE</span><h2>Questions, feedback, or product inquiries?</h2><p>Use the configured product contact method for product questions, feedback, partnerships, or other inquiries.</p></div><div id="siteContactState" class="site-page-contact-action"><span>CONTACT</span><strong>Loading configured contact information…</strong></div></div>`,
    '/privacy': `<div class="site-page-legal"><div class="legal-review">PRODUCT PRIVACY PAGE / REVIEW REQUIRED</div><section><h2>1. Information the application processes</h2><p>QuerySure processes information required by the current application to provide authenticated access, connected-data querying, account usage, and related product workflows.</p></section><section><h2>2. Account information</h2><p>The application has account and authentication flows, including email verification and password reset.</p></section><section><h2>3. Query and uploaded data</h2><p>Users may connect or upload data for the existing analytics workflow. The application records query and workflow state needed by its current functionality.</p></section><section><h2>4. Authentication</h2><p>Authentication-related information is processed as part of the existing account and protected-workspace flows.</p></section><section><h2>5. Product communications</h2><p>Product contact information is provided through the configured product contact mechanism.</p></section><section><h2>6. User controls</h2><p>Users can use the existing account, workspace, settings, and usage surfaces.</p></section><section><h2>7. Contact</h2><p>Use the configured product contact mechanism for privacy questions.</p></section></div>`,
    '/terms': `<div class="site-page-legal"><div class="legal-review">PRODUCT TERMS PAGE / LEGAL REVIEW REQUIRED</div><section><h2>1. Using QuerySure</h2><p>QuerySure provides the analytics workflow described in the product.</p></section><section><h2>2. Accounts</h2><p>Protected workspace functionality uses the existing account and authentication flow.</p></section><section><h2>3. Data and queries</h2><p>Users are responsible for the questions they submit and the data they connect or upload.</p></section><section><h2>4. Plans and usage limits</h2><p>Free and Pro plans have daily query limits. Business is arranged manually. There are no team workspaces, seats or organization accounts, and Team is not offered.</p></section><section><h2>5. Acceptable use</h2><p>Use the product consistently with its intended analytics workflow and do not attempt to bypass authentication, authorization, validation, read-only safeguards, or usage enforcement.</p></section><section><h2>6. Service availability</h2><p>Product behavior, availability, and supporting features may change as the application evolves.</p></section><section><h2>7. Changes to the service</h2><p>QuerySure may evolve as features and supporting systems change.</p></section><section><h2>8. Contact</h2><p>Use the configured product contact mechanism for product questions or feedback.</p></section></div>`
  };
  return content[route] || '<p>Page not found.</p>';
}

function applyBillingAvailability(data) {
  const text = data && data.billing_enabled ? 'Available: upgrade from Settings' : 'Not yet available';
  ['proAvailability', 'siteProAvailability'].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = text; });
}
function loadSiteContact() {
  const state = document.getElementById('siteContactState');
  const business = document.getElementById('siteBusinessContact');
  if (!state && !business) return;
  fetch('/api/product-info').then(r => r.ok ? r.json() : null).then(data => {
    applyBillingAvailability(data);
    const email = data?.contact_email;
    if (email) {
      const safe = escapeHtml(email);
      if (state) state.innerHTML = `<a class="site-contact-email" href="mailto:${safe}">${safe}</a>`;
      if (business) business.innerHTML = `<a href="mailto:${safe}">${safe}</a>`;
    } else {
      if (state) state.innerHTML = '<span class="site-contact-fallback">Contact email is currently unavailable.</span>';
      if (business) business.textContent = 'Contact email not configured.';
    }
  }).catch(() => {
    if (state) state.innerHTML = '<span class="site-contact-fallback">Contact email is currently unavailable.</span>';
  });
}

function renderSitePage(route, {push=true}={}) {
  const host = $('screen-site-page');
  if (!host || !PAGE_ROUTES.has(route)) return false;
  if (push && location.pathname !== route) history.pushState({querysureRoute:route}, '', route);
  const meta = SITE_PAGE_META[route];
  host.innerHTML = `${sitePageHeader()}<main class="site-page-main"><div class="site-page-kicker">${meta.kicker}</div><h1>${meta.title}</h1><p class="site-page-intro">${meta.intro}</p><div class="site-page-content">${sitePageBody(route)}</div></main>${sitePageFooter()}`;
  switchScreen('screen-site-page');
  loadSiteContact();
  window.scrollTo({top:0, behavior:'auto'});
  return true;
}

function markActiveSection(route) {
  document.querySelectorAll('#screen-landing .nav-links-main .nav-link').forEach(a => {
    a.classList.toggle('is-active', a.getAttribute('data-site-route') === route);
  });
}

// Shows the landing page and brings one of its sections to the top. Used by the header/footer links, by direct loads and
// refreshes of /pricing etc., and by browser Back/Forward.
function showLandingSection(route, {push=true, smooth=true}={}) {
  const id = SECTION_ROUTES[route];
  if (!id) return false;
  if (push && location.pathname !== route) history.pushState({querysureRoute:route}, '', route);
  switchScreen('screen-landing');
  markActiveSection(route);
  const go = (behavior) => document.getElementById(id)?.scrollIntoView({behavior, block:'start'});
  go(smooth ? 'smooth' : 'auto');
  if (!smooth) {
    // Fonts and images settle after first paint and move the section; correct once, only if the reader has not scrolled.
    const y = window.scrollY;
    window.addEventListener('load', () => { if (Math.abs(window.scrollY - y) < 4) go('auto'); }, {once:true});
  }
  return true;
}

function showLandingTop({push=true}={}) {
  if (push && location.pathname !== '/') history.pushState({querysureRoute:'/'}, '', '/');
  switchScreen('screen-landing');
  markActiveSection('');
  window.scrollTo({top:0, behavior:'auto'});
}

function navigateSiteAction(action) {
  if (action === 'back') { history.back(); return; }
  const signedIn = !!(authToken && currentUser?.email);
  if (action === 'signin' || action === 'signup') {
    if (signedIn) { switchScreen('screen-workspace'); history.pushState({querysureRoute:'/'}, '', '/'); }
    else showAuthGate();
    return;
  }
  if (action === 'settings') {
    if (signedIn) { switchScreen('screen-workspace'); openSettings(); }
    else showAuthGate('Sign in to open your settings.');
  }
}

function openRoute(route, opts) {
  if (route === '/') { showLandingTop(opts); return true; }
  if (SECTION_ROUTES[route]) return showLandingSection(route, opts);
  if (PAGE_ROUTES.has(route)) return renderSitePage(route, opts);
  return false;
}

document.addEventListener('click', (event) => {
  const routeLink = event.target.closest('[data-site-route]');
  if (routeLink && !event.defaultPrevented && !(event.metaKey || event.ctrlKey || event.shiftKey || event.button)) {
    const route = routeLink.getAttribute('data-site-route');
    if (route === '/' || SITE_ROUTES.has(route)) { event.preventDefault(); openRoute(route); return; }
  }
  const action = event.target.closest('[data-site-action]');
  if (action) { event.preventDefault(); navigateSiteAction(action.getAttribute('data-site-action')); }
});

window.addEventListener('popstate', () => {
  const route = location.pathname.replace(/\/+$/, '') || '/';
  if (route === '/') showLandingTop({push:false});
  else openRoute(route, {push:false, smooth:false});
});

function bootSiteRoute() {
  const route = location.pathname.replace(/\/+$/, '') || '/';
  if (!openRoute(route, {push:false, smooth:false})) switchScreen('screen-landing');
}

// Navbar landing wire integrations
$('heroPrimaryCta').onclick = () => showAuthGate();
$('heroSecondaryCta').onclick = () => document.getElementById('how-it-works')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
$('finalCta').onclick = () => showAuthGate();

// FIX: landing -> auth had no way back, and there was no way to return to
// the landing page from either the auth screen or the workspace without a
// full sign-out. These make the three screens navigable in both directions.
$('authBackBtn').onclick = () => { resetAuthForms(); activateAuthForm('login'); showLandingTop({push:false}); };
// Revoke the token on the server first (best effort: if the network is down the local token is still discarded).
async function serverSignOut() {
  try { await fetch('/api/auth/sign-out', {method:'POST', headers: authHeaders()}); } catch (_) {}
  setAuthToken('');
}
$('workspaceLogo').onclick = async (e) => {
  e.preventDefault();
  if (confirm('Return to the home page? You can sign back in to resume.')) {
    await serverSignOut();
    location.reload();
  }
};

// Auth tab switching
document.querySelectorAll('.auth-tab').forEach(tab => {
  tab.onclick = () => activateAuthForm(tab.dataset.form);
});

// Password visibility toggle (presentation only; does not touch auth state or submission)
document.querySelectorAll('.pw-toggle').forEach(btn => {
  btn.onclick = () => {
    const input = btn.closest('.field')?.querySelector('input');
    if (!input) return;
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    btn.textContent = show ? 'Hide' : 'Show';
    btn.setAttribute('aria-pressed', show ? 'true' : 'false');
    btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
  };
});

$('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('loginSubmit');
  btn.disabled = true; btn.textContent = 'Signing in…';
  try {
    const token = await doLogin($('loginEmail').value.trim(), $('loginPassword').value);
    setAuthToken(token);
    resetAuthForms();
    await bootApp();
  } catch (err) {
    showAuthError(err.message);
  } finally {
    btn.disabled = false; btn.textContent = 'Sign in';
  }
});

function showVerificationState(title, message, variant = 'pending') {
  const state = $('authVerificationState');
  $('authVerificationTitle').textContent = title;
  $('authVerificationMessage').textContent = message;
  $('authVerificationKicker').textContent = variant === 'success' ? 'Email verified' : variant === 'error' ? 'Verification issue' : 'Check your email';
  state.classList.remove('success', 'error');
  if (variant === 'success' || variant === 'error') state.classList.add(variant);
  state.hidden = false;
  document.querySelectorAll('.auth-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.auth-form').forEach(f => f.classList.remove('active'));
  syncAuthFieldEligibility();
  $('authError').classList.remove('visible');
}

function hideVerificationState() {
  $('authVerificationState').hidden = true;
  $('authVerificationState').classList.remove('success', 'error');
}

$('registerForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('registerSubmit');
  btn.disabled = true; btn.textContent = 'Creating account…';
  const email = $('registerEmail').value.trim();
  const password = $('registerPassword').value;
  try {
    await doRegister(email, password);
    resetAuthForms();
    showVerificationState(
      'Check your email',
      'Your account was created. We sent a verification link to your email. Verify your email before signing in.'
    );
  } catch (err) {
    showAuthError(err.message);
  } finally {
    btn.disabled = false; btn.textContent = 'Create account';
  }
});

$('forgotPasswordBtn').onclick = () => {
  // Nothing is carried over from the sign-in form: a value a password manager dropped there must not reappear here.
  $('forgotEmail').value = '';
  activateAuthForm('forgotPassword');
};

$('resendVerificationBtn').onclick = async () => {
  const email = $('loginEmail').value.trim();
  if (!email) { showAuthError('Enter your email address first.'); return; }
  try {
    await doResendVerification(email);
    showAuthError('If that account exists and still needs verifying, a new verification email is on its way.');
  } catch (err) { $('authError').textContent = err.message; $('authError').classList.add('visible'); }
};

document.querySelectorAll('.auth-back-form').forEach(btn => {
  btn.onclick = () => activateAuthForm('login');
});

$('forgotPasswordForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    await doForgotPassword($('forgotEmail').value.trim());
    showAuthError('If that account exists, a password reset link is on its way.');
  } catch (err) { $('authError').textContent = err.message; $('authError').classList.add('visible'); }
});

$('resetPasswordForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const token = new URLSearchParams(location.hash.replace(/^#/, '')).get('reset_token');
  const password = $('resetPassword').value;
  if (!token) { $('authError').textContent = 'Password reset link is missing.'; $('authError').classList.add('visible'); return; }
  if (password !== $('resetPasswordConfirm').value) { $('authError').textContent = 'Passwords do not match.'; $('authError').classList.add('visible'); return; }
  try {
    await doResetPassword(token, password);
    history.replaceState({}, '', location.pathname);
    resetAuthForms();
    activateAuthForm('login');
    showAuthError('Password reset. You can now sign in.');
    $('authError').classList.remove('visible');
    $('authError').classList.add('visible', 'is-info');
  } catch (err) { $('authError').textContent = err.message; $('authError').classList.add('visible'); }
});

async function handleAuthLink() {
  const params = new URLSearchParams(location.hash.replace(/^#/, ''));
  const verifyToken = params.get('verify_token');
  const resetToken = params.get('reset_token');
  if (!verifyToken && !resetToken) return;
  showAuthGate();
  if (verifyToken) {
    try {
      await doVerifyEmail(verifyToken);
      history.replaceState({}, '', location.pathname);
      showVerificationState('Email verified', 'Your email has been verified. You can now sign in.', 'success');
      $('verificationBackBtn').focus();
    } catch (err) {
      history.replaceState({}, '', location.pathname);
      showVerificationState('Verification could not be completed', err.message || 'Verification could not be completed. Please try again.', 'error');
    }
  } else {
    activateAuthForm('resetPassword');
  }
}

$('verificationBackBtn').onclick = () => activateAuthForm('login');

$('signOutBtn').onclick = async () => {
  await serverSignOut();
  location.reload();
};

// Workspace Settings uses the existing frontend state only. Secrets are never
// rendered into the DOM; replacement starts with an empty password field.
function shortId(value) {
  if (!value) return '—';
  return value.length > 16 ? value.slice(0, 8) + '…' + value.slice(-4) : value;
}
function refreshSettingsView() {
  if ($('settingsEmail')) $('settingsEmail').textContent = currentUser.email || '—';
  { const em = currentUser.email || '';
    if ($('userEmailLabel')) $('userEmailLabel').textContent = em || 'Account';
    if ($('userMenuEmail')) $('userMenuEmail').textContent = em || '—';
    if ($('userAvatar')) $('userAvatar').textContent = (em.trim().charAt(0) || '?').toUpperCase(); }
  if ($('settingsUserId')) $('settingsUserId').textContent = shortId(currentUser.id);
  if ($('settingsSessionId')) $('settingsSessionId').textContent = shortId(sessionId);
  const configured = !!(serverLlmConfigured || apiKeyOverride);
  if ($('settingsLlmStatus')) $('settingsLlmStatus').textContent = configured ? 'Configured' : 'Not configured';
  if ($('settingsLlmConfigured')) $('settingsLlmConfigured').hidden = !configured;
  if ($('settingsLlmUnconfigured')) $('settingsLlmUnconfigured').hidden = configured;
  if ($('llmSidebarStatus')) $('llmSidebarStatus').textContent = configured ? 'Configured' : 'Optional';
  if (accountUsage) {
    const price = accountUsage.price_monthly_usd == null ? 'Custom' : `$${accountUsage.price_monthly_usd}/month`;
    $('settingsPlanName').textContent = accountUsage.plan_name || accountUsage.plan || 'Free';
    $('settingsPlanPrice').textContent = price;
    $('settingsQueryUsage').textContent = accountUsage.daily_limit == null ? `${accountUsage.used_today} today` : `${accountUsage.used_today} / ${accountUsage.daily_limit}`;
    $('settingsQueriesRemaining').textContent = accountUsage.remaining_today == null ? 'Custom' : String(accountUsage.remaining_today);
    $('settingsResetAt').textContent = accountUsage.reset_at ? new Date(accountUsage.reset_at).toLocaleString([], {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'}) : '—';
  }
  if ($('settingsPasswordStatus')) {
    $('settingsPasswordStatus').textContent = currentUser.hasPassword === false ? 'Not set' : 'Enabled';
    $('settingsGoogleStatus').textContent = currentUser.googleLinked ? 'Linked' : (googleAuthConfig && !googleAuthConfig.enabled ? 'Not available' : 'Not linked');
  }
  if ($('topbarUsage')) {
    $('topbarUsage').textContent = accountUsage ? (accountUsage.daily_limit == null ? `${accountUsage.used_today} queries today` : `${accountUsage.used_today}/${accountUsage.daily_limit} queries today`) : '';
  }
  if ($('settingsBillingStatus')) renderBillingPanel();
  if ($('settingsDatasets')) {
    if (!currentDataset) {
      $('settingsDatasets').textContent = 'No datasets connected.';
    } else {
      const tableText = currentDataset.tables == null ? '' : `${currentDataset.tables} table${currentDataset.tables === 1 ? '' : 's'}`;
      const rowText = currentDataset.rows == null ? '' : `${numFmt(currentDataset.rows)} rows`;
      const meta = [tableText, rowText].filter(Boolean).join(' · ');
      $('settingsDatasets').innerHTML = `<div class="settings-dataset-name">${escapeHtml(currentDataset.name || 'Connected dataset')}</div><div>${escapeHtml(meta || 'Connected')}</div>`;
    }
  }
}

// ---- Billing panel: every state is explicit (not enabled / free / activating / active / ending / past due / canceled / error)
let billingNotice = '';
function renderBillingPanel() {
  const b = billingState || {};
  const status = b.status || 'inactive';
  const paid = (accountUsage && accountUsage.plan && accountUsage.plan !== 'free');
  $('settingsBillingStatus').textContent = status.charAt(0).toUpperCase() + status.slice(1);
  const start = b.current_period_start ? new Date(b.current_period_start) : null;
  const end = b.current_period_end ? new Date(b.current_period_end) : null;
  const fmt = (d, y) => d.toLocaleDateString([], {month:'short', day:'numeric', year: y ? 'numeric' : undefined});
  $('settingsBillingPeriod').textContent = start && end ? `${fmt(start)} – ${fmt(end, true)}` : 'Not active';
  let msg = billingNotice;
  if (!msg) {
    if (!billingState) msg = 'Loading billing information…';
    else if (b.billing_enabled === false && !paid) msg = 'Payments are not enabled on this deployment, so upgrades are unavailable.';
    else if (status === 'past_due') msg = 'Your last payment failed. Update your payment method with the provider to keep Pro access' + (end ? ` (access continues for a short grace period after ${fmt(end, true)}).` : '.');
    else if (status === 'active' && b.cancel_at_period_end && end) msg = `Your subscription is cancelled and stays active until ${fmt(end, true)}.`;
    else if (status === 'active') msg = end ? `Renews on ${fmt(end, true)}.` : 'Subscription active.';
    else if (status === 'canceled') msg = 'Your subscription has ended. You are on the Free plan.';
    else msg = 'You are on the Free plan.';
  }
  const m = $('settingsBillingMessage'); if (m) m.textContent = msg;
  const canBuy = !!(b.billing_enabled && (b.purchasable_plans || []).includes('pro') && !paid);
  const up = $('settingsUpgradeBtn'); if (up) up.hidden = !canBuy;
  const portal = $('settingsManageBilling');
  if (portal) {
    const ok = typeof b.portal_url === 'string' && /^https:\/\//.test(b.portal_url) && status !== 'inactive';
    portal.hidden = !ok;
    if (ok) portal.href = b.portal_url;
  }
}
async function startCheckout() {
  const btn = $('settingsUpgradeBtn');
  btn.disabled = true; const label = btn.textContent; btn.textContent = 'Starting checkout…';
  billingNotice = '';
  const res = await post('/api/billing/checkout', {plan: 'pro'});
  btn.disabled = false; btn.textContent = label;
  if (!res || res.success === false || !res.checkout_url) {
    billingNotice = (res && (res.error || res.detail)) || 'Could not start checkout. Please try again.';
    if (res && res.error_code === 'ALREADY_SUBSCRIBED') { refreshBillingState(); refreshAccountUsage(); }
    renderBillingPanel(); return;
  }
  let target;
  try { target = new URL(res.checkout_url); } catch (_) { target = null; }
  if (!target || (target.protocol !== 'https:' && !(target.protocol === 'http:' && location.protocol === 'http:'))) {
    billingNotice = 'The payment provider returned an invalid checkout address.'; renderBillingPanel(); return;
  }
  window.location.assign(target.href);
}
// After the provider redirects back (?billing=return) the plan changes only when its signed webhook arrives, which can
// lag by seconds or minutes: poll for a bounded time, then say plainly that it is still processing.
async function handleCheckoutReturn() {
  const params = new URLSearchParams(location.search);
  if (params.get('billing') !== 'return') return;
  history.replaceState({}, '', location.pathname + location.hash);
  openSettings();
  billingNotice = 'Payment received. Activating your plan…'; renderBillingPanel();
  for (let i = 0; i < 12; i++) {
    await refreshAccountUsage(); await refreshBillingState();
    if (accountUsage && accountUsage.plan && accountUsage.plan !== 'free') { billingNotice = ''; renderBillingPanel(); return; }
    await sleep(5000);
  }
  billingNotice = 'Still processing. This can take a few minutes; your plan will update automatically once the payment is confirmed.';
  renderBillingPanel();
}

// ---- Account data: export, sign out everywhere, delete
function setAccountMessage(text, isError) {
  const el = $('accountDataMessage'); if (!el) return;
  el.textContent = text || ''; el.classList.toggle('error', !!isError);
}
async function exportAccountData() {
  setAccountMessage('Preparing your export…');
  let r;
  try { r = await fetch('/api/account/export', {headers: authHeaders()}); } catch (_) { setAccountMessage(NETWORK_ERROR.error, true); return; }
  if (r.status === 401) { onUnauthorized(); return; }
  if (r.status === 429) { setAccountMessage('You can export your data 3 times per hour. Please try again later.', true); return; }
  if (!r.ok) { setAccountMessage('Export failed. Please try again.', true); return; }
  download('querysure-account-export.json', await r.text(), 'application/json');
  setAccountMessage('Export downloaded.');
}
async function signOutEverywhere() {
  if (!confirm('Sign out of every device, including this one?')) return;
  const res = await post('/api/auth/logout-all', {}, {keepAuthOn401: true});
  if (res && res.success === false) { setAccountMessage(res.error || 'Could not sign out everywhere.', true); return; }
  setAuthToken(''); location.reload();
}
async function deleteWithGoogle(response) {
  const email = $('deleteConfirmEmail').value.trim();
  if (!email) { setAccountMessage('Enter your account email first, then confirm with Google.', true); return; }
  setAccountMessage('Confirming with Google…');
  const res = await post('/api/account/delete', {confirm_email: email, google_credential: response.credential}, {keepAuthOn401: true});
  finishDelete(res, 'Google could not confirm your identity. Use the Google account linked to this email.');
}
function finishDelete(res, reauthMessage) {
  if (res && res.success !== false && res.deleted) { setAuthToken(''); alert('Your account and data have been deleted.'); location.reload(); return; }
  const messages = {
    REAUTH_FAILED: reauthMessage, CONFIRMATION_MISMATCH: 'The email you typed does not match this account.',
    SUBSCRIPTION_ACTIVE: 'Cancel your subscription with the payment provider first, then delete your account.',
    JOBS_IN_PROGRESS: 'Background work is still running. Try again in a few minutes.',
    DELETE_INCOMPLETE: 'We could not remove all stored files, so nothing was deleted. Please try again.',
  };
  setAccountMessage(messages[res && res.error_code] || (res && res.error) || 'Could not delete the account.', true);
}
async function linkGoogleCredential(response) {
  $('settingsAuthMessage').classList.remove('error');
  $('settingsAuthMessage').textContent = 'Linking your Google account…';
  const res = await post('/api/auth/google/link', {credential: response.credential}, {keepAuthOn401: true});
  if (res && res.success !== false && res.google_linked) {
    currentUser.googleLinked = true;
    refreshSettingsView();
    $('settingsGoogleLink').hidden = true;
    $('settingsAuthMessage').textContent = 'Google account linked. You can now sign in with Google or your password.';
    return;
  }
  $('settingsAuthMessage').classList.add('error');
  $('settingsAuthMessage').textContent = (res && res.error) || 'Could not link the Google account.';
}
async function setupSettingsGoogle() {
  const wrap = $('settingsGoogleLink');
  if (currentUser.googleLinked) { wrap.hidden = true; return; }
  const { state } = await loadGoogleIdentity();
  refreshSettingsView();
  if (state !== 'ready') { wrap.hidden = true; return; }
  wrap.hidden = false;
  await mountGoogleButton($('settingsGoogleHost'), linkGoogleCredential, 'continue_with');
}
async function showDeleteForm() {
  const form = $('deleteAccountForm');
  form.hidden = !form.hidden;
  setDeleteFieldsEligible(!form.hidden);
  setAccountMessage('');
  if (form.hidden) return;
  const googleOnly = currentUser.hasPassword === false;
  $('deleteConfirmPassword').hidden = googleOnly;
  $('deleteAccountConfirmBtn').hidden = googleOnly;
  $('deleteGoogleWrap').hidden = !googleOnly;
  if (googleOnly) await mountGoogleButton($('deleteGoogleHost'), deleteWithGoogle, 'continue_with');
}
async function deleteAccount() {
  const email = $('deleteConfirmEmail').value.trim(), password = $('deleteConfirmPassword').value;
  if (!email || !password) { setAccountMessage('Enter your email and password to confirm.', true); return; }
  const btn = $('deleteAccountConfirmBtn'); btn.disabled = true;
  const res = await post('/api/account/delete', {confirm_email: email, password}, {keepAuthOn401: true});
  btn.disabled = false;
  finishDelete(res, 'Password is incorrect.');
}
// Plan, usage and billing decide which rows the panel shows ("Upgrade to Pro" sits above "Download my data"). Right after
// sign-in they may not be loaded yet; revealing the panel first and filling it in afterwards pushed the account buttons
// down while the user was clicking them. So when they are unknown, load them before the panel appears (bounded, so a slow
// API never keeps it closed); when they are already known (the usual case) it opens at once and refreshes in place.
const SETTINGS_DATA_WAIT_MS = 4000;
let settingsOpening = false;
async function openSettings() {
  if (settingsOpening) return;
  setupSettingsGoogle();
  refreshSettingsView();
  const loads = Promise.allSettled([refreshAccountUsage(), refreshBillingState()]);
  if ($('settingsModal').hidden && (!accountUsage || !billingState)) {
    settingsOpening = true;
    try { await Promise.race([loads, sleep(SETTINGS_DATA_WAIT_MS)]); } finally { settingsOpening = false; }
    refreshSettingsView();
  }
  $('settingsModal').hidden = false;
  document.body.classList.add('settings-open');
}
function closeSettings() {
  $('settingsModal').hidden = true;
  document.body.classList.remove('settings-open');
  if ($('settingsKeyEditor')) $('settingsKeyEditor').hidden = true;
  if ($('apiKeyInput')) $('apiKeyInput').value = '';
}
// ---- Top navigation menus (data source, account): one open at a time; Escape and an outside click close them.
const NAV_MENUS = [['dataMenuBtn', 'dataMenu'], ['userMenuBtn', 'userMenu']];
function closeNavMenus(except) {
  NAV_MENUS.forEach(([b, m]) => {
    if (m === except) return;
    const menu = $(m); if (menu && !menu.hidden) { menu.hidden = true; $(b).setAttribute('aria-expanded', 'false'); }
  });
}
NAV_MENUS.forEach(([b, m]) => {
  $(b).addEventListener('click', ev => {
    ev.stopPropagation();
    const menu = $(m), open = menu.hidden;
    closeNavMenus(open ? m : null);
    menu.hidden = !open; $(b).setAttribute('aria-expanded', String(open));
  });
  $(m).addEventListener('click', ev => {
    ev.stopPropagation();
    if (ev.target.closest('#sampleBtn, #newSessionBtn, #signOutBtn')) closeNavMenus(null);
  });
});
document.addEventListener('click', () => closeNavMenus(null));
document.addEventListener('keydown', ev => {
  if (ev.key !== 'Escape') return;
  const open = NAV_MENUS.find(([, m]) => !$(m).hidden);
  if (open) { closeNavMenus(null); $(open[0]).focus(); }
});
$('fileInput').addEventListener('change', () => closeNavMenus(null));

$('settingsToggleBtn').onclick = openSettings;
$('settingsCloseBtn').onclick = closeSettings;
if ($('settingsViewPlans')) $('settingsViewPlans').onclick = (e) => { e.preventDefault(); closeSettings(); showLandingSection('/pricing'); };
document.querySelector('[data-settings-close]').onclick = closeSettings;
$('settingsModal').addEventListener('click', (event) => {
  const button = event.target.closest('[data-copy-target]');
  if (!button) return;
  const target = $(button.dataset.copyTarget);
  const value = target === $('settingsUserId') ? currentUser.id : sessionId;
  if (!value || !navigator.clipboard) return;
  navigator.clipboard.writeText(value).then(() => {
    const old = button.textContent; button.textContent = 'Copied';
    setTimeout(() => { button.textContent = old; }, 900);
  }).catch(() => {});
});
$('settingsReplaceKeyBtn').onclick = () => {
  $('settingsKeyEditor').hidden = false;
  $('apiKeyInput').value = '';
  $('apiKeyInput').focus();
};
$('apiKeyCancelBtn').onclick = () => { $('apiKeyInput').value = ''; $('settingsKeyEditor').hidden = true; };
$('apiKeySaveBtn').onclick = () => {
  apiKeyOverride = $('apiKeyInput').value.trim();
  if (apiKeyOverride) localStorage.setItem('qs_api_key', apiKeyOverride);
  else localStorage.removeItem('qs_api_key');
  $('apiKeyInput').value = '';
  $('settingsKeyEditor').hidden = true;
  refreshSettingsView();
};

// FIX: on a production deployment /api/generate-sql ignores any body-supplied
// api_key (server-managed keys only there), so letting the Settings panel
// accept and store one anyway was misleading -- and stashing an LLM key in
// localStorage isn't something a public deployment should invite. This
// checks the server's actual policy and swaps the input for a status line
// when the server doesn't allow a client-supplied key.
async function refreshProviderStatus() {
  const status = await apiGet('/api/provider-status');
  if (!status || status.success === false) return;
  allowClientApiKey = !!status.allow_client_api_key;
  serverLlmConfigured = !!status.llm_configured;
  if (!allowClientApiKey) {
    apiKeyOverride = '';
    localStorage.removeItem('qs_api_key');
  }
  refreshSettingsView();
}
$('globalErrorDismiss').onclick = clearGlobalError;

function setStatus(text, ok) {
  $('statusText').textContent = text;
  const led = $('statusLed');
  led.className = 'status-live-led';
  if (ok) led.classList.add('on');
}

function setPipelineState(label) {
  $('pipelineState').textContent = label;
}

// ---- Workflow canvas -------------------------------------------------------------------------------------------------------
// Eight nodes drawn from the REAL per-stage records in `stageInfo` (status, browser-measured round trip, the backend's own
// response). Nothing here invents progress, timings, logs or success: a stage that has not run is "pending", and a stage whose
// backend response did not include something says so instead of filling the gap.
const STATUS_WORD = {pending: 'pending', active: 'running', done: 'completed', warn: 'warning', error: 'failed', skipped: 'skipped', na: 'no details'};
const NODE_DEFS = [
  {key: 'question', label: 'Question', stages: [],
    purpose: 'The question exactly as it was submitted. Every later stage works from this text.'},
  {key: 'ground', label: 'Ground Data', stages: ['connect', 'schema', 'semantic', 'retrieval'],
    purpose: 'Reads the schema, adds business meaning to the columns, and picks the tables and columns that matter for the question.'},
  {key: 'plan', label: 'Plan', stages: ['sql'],
    purpose: 'A structured plan for the query (measure, grouping, filters, ordering) that the SQL is later checked against. It is produced inside the SQL-generation request, so it is not timed separately.'},
  {key: 'sql', label: 'Generate SQL', stages: ['sql'],
    purpose: 'Asks the model for read-only SQL, grounded on the retrieved context, real values from your data and the plan.'},
  {key: 'validation', label: 'Validate', stages: ['validation'],
    purpose: 'Checks syntax, read-only safety, schema references and joins. Nothing runs if this fails.'},
  {key: 'execution', label: 'Execute', stages: ['execution'],
    purpose: 'Runs the validated SQL read-only against your data.'},
  {key: 'verify', label: 'Verify', stages: ['reliability'],
    purpose: 'Compares the SQL with the plan, re-checks the result independently where it can, and scores data reliability. A passing run does not prove the answer is right.'},
  {key: 'answer', label: 'Answer', stages: ['insights', 'report'],
    purpose: 'Summarises the result in plain language and prepares the saved report.'},
];
const NODE_BY_KEY = Object.fromEntries(NODE_DEFS.map(n => [n.key, n]));
const STAGE_NAME = {connect: 'Connect dataset', schema: 'Read schema', semantic: 'Add meaning', retrieval: 'Find tables', sql: 'Write SQL',
  validation: 'Validate SQL', execution: 'Run query', reliability: 'Check data', insights: 'Summarize', report: 'Prepare report'};
const VERIFY_WORD = {verified_match: 'Verified', mismatch: 'Mismatch', suspicious_empty: 'Suspiciously empty', empty_plausible: 'Empty, unconfirmed',
  not_independently_verified: 'Not independently verified', error: 'Verification failed'};

let stageInfo = {};
let selectedStage = null;
let nodeStates = {};
let runQuestion = null;        // {text, at}: set when a run starts, cleared by a flush
let pipelineIsBusy = false;
let lastTrace = null;          // the sanitised /api/answer-trace payload for the current answer, when the backend returned one
let inspectorReturnFocus = null;

const SECRET_KEY = /(token|secret|password|passwd|api[_-]?key|authorization|cookie|credential)/i;
function redactForDisplay(v, depth = 0) {
  if (depth > 6 || v === null || typeof v !== 'object') return v;
  if (Array.isArray(v)) return v.slice(0, 50).map(x => redactForDisplay(x, depth + 1));
  const out = {};
  Object.keys(v).forEach(k => { out[k] = SECRET_KEY.test(k) ? '[hidden]' : redactForDisplay(v[k], depth + 1); });
  return out;
}

// Stage records. `stageStart/End/Skip/Done` are the only writers; the canvas only reads.
function stageStart(step) { stageInfo[step] = {status: 'running', t0: performance.now()}; refreshStages(); }
function stageEnd(step, data, status = 'done', note = '') {
  const st = stageInfo[step] || (stageInfo[step] = {});
  if (st.status === 'done' && status === 'done' && !data) return;
  st.status = status; st.ms = st.t0 ? Math.round(performance.now() - st.t0) : null;
  if (data !== undefined) st.data = data;
  if (note) st.note = note;
  refreshStages();
}
function stageSkip(step, note) { stageInfo[step] = {status: 'skipped', note}; refreshStages(); }
function stageDone(step, data) { $('rawOut').textContent = pretty(data); stageEnd(step, data, 'done'); }

const sd = k => (stageInfo[k] && stageInfo[k].data) || null;
function sumMs(keys) {
  let total = 0, any = false;
  keys.forEach(k => { const s = stageInfo[k]; if (s && typeof s.ms === 'number') { total += s.ms; any = true; } });
  return any ? total : null;
}
const cap1 = s => s.charAt(0).toUpperCase() + s.slice(1);
const fmtMs = ms => (ms >= 1000 ? (ms / 1000).toFixed(ms >= 10000 ? 0 : 1) + ' s' : ms + ' ms');

// Derived node state. One backend request can serve two nodes (Plan and Generate SQL), and one node can span several requests
// (Ground Data, Answer); both are handled here so the canvas never claims more precision than the backend recorded.
function nodeState(key) {
  const out = {state: 'pending', note: '', ms: null};
  const S = k => stageInfo[k];
  const mapSingle = (k) => {
    const s = S(k); if (!s) return out;
    out.ms = typeof s.ms === 'number' ? s.ms : null;
    if (s.status === 'running') out.state = 'active';
    else if (s.status === 'skipped') { out.state = 'skipped'; out.note = s.note || ''; }
    else if (s.status === 'error') { out.state = 'error'; out.note = s.note || ''; }
    else if (s.status === 'done') out.state = 'done';
    return out;
  };
  switch (key) {
    case 'question':
      if (runQuestion) out.state = 'done';
      return out;
    case 'ground': {
      const subs = ['schema', 'semantic', 'retrieval'];
      const present = ['connect', ...subs].filter(k => S(k));
      out.ms = sumMs(['connect', ...subs]);
      const bad = present.find(k => S(k).status === 'error');
      if (bad) { out.state = 'error'; out.note = S(bad).note || ''; return out; }
      if (present.some(k => S(k).status === 'running')) { out.state = 'active'; return out; }
      if (!subs.some(k => S(k))) return out;    // connecting a dataset alone does not start grounding
      const finished = subs.every(k => S(k) && (S(k).status === 'done' || S(k).status === 'skipped'));
      if (finished) out.state = 'done';
      else { out.state = pipelineIsBusy ? 'active' : 'warn'; out.note = pipelineIsBusy ? '' : 'Grounding did not finish.'; }
      return out;
    }
    case 'plan': {
      const s = S('sql'); if (!s) return out;
      if (s.status === 'running') { out.state = 'active'; return out; }
      if (s.status === 'skipped') { out.state = 'skipped'; out.note = s.note || ''; return out; }
      if (s.status === 'error') { out.state = 'na'; out.note = 'No plan was returned because SQL generation failed.'; return out; }
      const plan = s.data && s.data.plan;
      if (plan && typeof plan === 'object') out.state = 'done';
      else { out.state = 'na'; out.note = 'The backend returned no plan for this query.'; }
      return out;
    }
    case 'sql':
    case 'execution': {
      mapSingle(key);
      if (key === 'execution' && out.state === 'done') {
        const meta = (sd('execution') || {}).execution_metadata || {};
        if (meta.truncated) { out.state = 'warn'; out.note = `The result was cut off at ${meta.max_rows != null ? numFmt(meta.max_rows) : 'the row limit'} rows.`; }
      }
      return out;
    }
    case 'validation': {
      mapSingle('validation');
      if (out.state === 'done') {
        const d = sd('validation') || {};
        const issues = ((d.warnings || []).length) + ((d.issues || []).length);
        const joinBad = d.join_validation && d.join_validation.valid === false;
        const aggBad = d.aggregation_validation && d.aggregation_validation.valid === false;
        const fan = /^(medium|high)$/i.test(String(d.fanout_risk || ''));
        if (issues || joinBad || aggBad || fan) { out.state = 'warn'; out.note = 'The SQL passed, with findings worth reading.'; }
      }
      return out;
    }
    case 'verify': {
      mapSingle('reliability');
      if (out.state === 'done') {
        const ex = sd('execution') || {}, rel = sd('reliability') || {};
        const vs = ex.verification && ex.verification.status;
        const sem = ex.semantic_validation && ex.semantic_validation.status;
        const reasons = [];
        if (vs !== 'verified_match') reasons.push(VERIFY_WORD[vs] || 'Not independently verified');
        if (sem === 'needs_attention') reasons.push('The SQL does not fully match the plan');
        if (rel.capped_reason) reasons.push(cap1(String(rel.capped_reason)));
        if (reasons.length) { out.state = 'warn'; out.note = reasons.join('. ') + '.'; }
      }
      return out;
    }
    case 'answer': {
      const i = S('insights'), r = S('report');
      out.ms = sumMs(['insights', 'report']);
      if (!i && !r) return out;
      const bad = [i, r].find(s => s && s.status === 'error');
      if (bad) {
        if (bad === r && i && i.status === 'done') { out.state = 'warn'; out.note = 'The summary is ready, but the report could not be prepared. ' + (r.note || ''); }
        else { out.state = 'error'; out.note = bad.note || ''; }
        return out;
      }
      if ((i && i.status === 'running') || (r && r.status === 'running')) { out.state = 'active'; return out; }
      if (i && i.status === 'done' && r && (r.status === 'done' || r.status === 'skipped')) out.state = 'done';
      else if (i && i.status === 'done') out.state = pipelineIsBusy ? 'active' : 'warn';
      return out;
    }
  }
  return out;
}

// Short captions on the connectors. Only ever filled from numbers the backend actually returned.
function edgeLabel(i) {
  const reached = nodeStates[NODE_DEFS[i].key] && /^(done|warn)$/.test(nodeStates[NODE_DEFS[i].key].state);
  if (!reached) return '';
  if (i === 1) {
    const r = sd('retrieval'); const n = r && (Array.isArray(r.relevant_tables) ? r.relevant_tables.length : Array.isArray(r.top_tables) ? r.top_tables.length : null);
    return n != null ? `${n} table${n === 1 ? '' : 's'}` : '';
  }
  if (i === 4) { const v = sd('validation'); return v && typeof v.validation_score === 'number' ? `score ${v.validation_score}` : ''; }
  if (i === 5) { const e = sd('execution'); const n = e && (e.row_count ?? (e.rows ? e.rows.length : null)); return n != null ? `${numFmt(n)} row${n === 1 ? '' : 's'}` : ''; }
  if (i === 6) { const r = sd('reliability'); return r && typeof r.reliability_score === 'number' ? `${r.reliability_score}/100` : ''; }
  return '';
}
function edgeState(i) {
  const a = nodeStates[NODE_DEFS[i].key], b = nodeStates[NODE_DEFS[i + 1].key];
  if (!a || !b) return 'idle';
  if (b.state === 'active') return 'active';
  if (/^(done|warn|skipped)$/.test(a.state) && /^(done|warn|skipped)$/.test(b.state)) return b.state === 'warn' ? 'warn' : 'done';
  if (/^(done|warn|skipped)$/.test(a.state) && b.state === 'error') return 'error';
  return 'idle';
}
function drawEdges() {
  const canvas = $('wfCanvas'), svg = $('wfEdges'), labels = $('wfEdgeLabels');
  if (!canvas || !svg || !labels || canvas.offsetParent === null) return;
  const cr = canvas.getBoundingClientRect();
  const tiles = Array.from(canvas.querySelectorAll('.node-tile'));
  if (tiles.length < 2) return;
  svg.setAttribute('viewBox', `0 0 ${Math.round(cr.width)} ${Math.round(cr.height)}`);
  svg.setAttribute('width', Math.round(cr.width)); svg.setAttribute('height', Math.round(cr.height));
  let paths = '', chips = '';
  for (let i = 0; i < tiles.length - 1; i++) {
    const a = tiles[i].getBoundingClientRect(), b = tiles[i + 1].getBoundingClientRect();
    const y = Math.round(a.top + a.height / 2 - cr.top);
    const x1 = Math.round(a.right - cr.left + 5), x2 = Math.round(b.left - cr.left - 5);
    if (x2 - x1 < 12) continue;
    const st = edgeState(i);
    const sel = selectedStage && (NODE_DEFS[i].key === selectedStage || NODE_DEFS[i + 1].key === selectedStage) ? ' sel' : '';
    paths += `<g class="edge ${st}${sel}"><path class="edge-base" d="M${x1} ${y}H${x2}"/><path class="edge-flow" d="M${x1} ${y}H${x2}"/><path class="edge-arrow" d="M${x2 - 7} ${y - 4.5}L${x2 - 0.5} ${y}L${x2 - 7} ${y + 4.5}"/></g>`;
    const text = edgeLabel(i);
    if (text) chips += `<span class="edge-chip ${st}" style="left:${Math.round((x1 + x2) / 2)}px;top:${y - 22}px">${escapeHtml(text)}</span>`;
  }
  svg.innerHTML = paths;
  labels.innerHTML = chips;
}

// userSelected: the user just picked a stage (click / keyboard). Then the strip moves only if that node is not fully visible,
// and instantly: a smooth slide moved the next node under the pointer, so a second click landed on the neighbouring stage.
function keepActiveInView(userSelected = false) {
  const scroller = $('wfScroll'); if (!scroller || scroller.scrollWidth <= scroller.clientWidth + 2) return;
  const sel = selectedStage && document.querySelector(`.pipeline-step-node[data-step="${selectedStage}"]`);
  const li = (userSelected ? sel : null) || document.querySelector('.pipeline-step-node.active') || sel;
  if (!li) return;
  // With the inspector open, centre the node in the part of the canvas it does not cover. Measured, not assumed: on wide
  // screens the workspace makes room for the inspector (margin), on narrower ones the inspector overlaps the canvas.
  const sr = scroller.getBoundingClientRect();
  const insp = $('stageDetail');
  // offsetLeft/offsetTop (the inspector is position: fixed): its layout box, without the slide-in transform.
  const open = sel && li === sel && insp && !insp.hidden && insp.offsetWidth > 0;
  const visibleRight = (open && insp.offsetLeft < sr.right && insp.offsetTop < sr.bottom) ? Math.max(sr.left + 160, insp.offsetLeft) : sr.right;
  const visibleWidth = Math.min(scroller.clientWidth, visibleRight - sr.left);
  const anchor = visibleWidth / 2;
  if (userSelected) {
    const nodeLeft = li.offsetLeft - scroller.scrollLeft;
    if (nodeLeft >= 0 && nodeLeft + li.offsetWidth <= visibleWidth) return;
  }
  const left = li.offsetLeft + li.offsetWidth / 2 - anchor;
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  scroller.scrollTo({left: Math.max(0, left), behavior: (reduce || userSelected) ? 'auto' : 'smooth'});
}

// ---- Inspector (side panel) ---------------------------------------------------------------------------------------------------
function fmtVal(v, depth = 0) {
  if (v === null || v === undefined || v === '') return '—';
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (typeof v === 'number') return Number.isFinite(v) ? numFmt(v) : String(v);
  if (typeof v === 'string') return v.length > 400 ? v.slice(0, 397) + '…' : v;
  if (Array.isArray(v)) {
    if (!v.length) return 'none';
    return v.slice(0, 12).map(x => (x !== null && typeof x === 'object' ? JSON.stringify(redactForDisplay(x)) : fmtVal(x, depth + 1))).join(', ') + (v.length > 12 ? ` … (+${v.length - 12})` : '');
  }
  const s = JSON.stringify(redactForDisplay(v)); return s.length > 300 ? s.slice(0, 297) + '…' : s;
}
function inspFacts(rows) {
  const keep = rows.filter(r => r && r[1] !== undefined && r[1] !== null && r[1] !== '');
  if (!keep.length) return '';
  return '<dl class="insp-facts">' + keep.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(typeof v === 'string' ? v : fmtVal(v))}</dd>`).join('') + '</dl>';
}
function inspSection(title, body) { return body ? `<section class="insp-sec"><h4>${escapeHtml(title)}</h4>${body}</section>` : ''; }
function inspList(items, cls = '') {
  const xs = (items || []).filter(x => x !== null && x !== undefined && x !== '');
  if (!xs.length) return '';
  return `<ul class="insp-list ${cls}">` + xs.slice(0, 40).map(x => `<li>${escapeHtml(typeof x === 'string' ? x : fmtVal(x))}</li>`).join('') + '</ul>';
}
function inspCode(text) { return text ? `<pre class="insp-code">${highlightSql(String(text))}</pre>` : ''; }
function inspPill(text, tone) { return `<span class="pill-tag ${tone || 'tag-neutral'}">${escapeHtml(String(text))}</span>`; }
function issueText(x) {
  if (x === null || x === undefined) return '';
  if (typeof x === 'string') return x;
  if (typeof x === 'object') return [x.type, x.message || x.detail || x.text].filter(Boolean).join(': ') || fmtVal(x);
  return String(x);
}
function previewTable(rows) {
  if (!Array.isArray(rows) || !rows.length) return '';
  const keys = Object.keys(rows[0]).slice(0, 8);
  const cell = v => (v === null || v === undefined ? '<td class="null">null</td>' : `<td>${escapeHtml(typeof v === 'number' ? numFmt(v) : String(v).slice(0, 60))}</td>`);
  return '<div class="insp-table-wrap"><table class="insp-table"><thead><tr>' + keys.map(k => `<th>${escapeHtml(k)}</th>`).join('') + '</tr></thead><tbody>' +
    rows.slice(0, 5).map(r => '<tr>' + keys.map(k => cell(r[k])).join('') + '</tr>').join('') + '</tbody></table></div>';
}

const NODE_BODY = {
  question() {
    if (!runQuestion) return '';
    return inspSection('Input', inspFacts([
      ['Question', runQuestion.text],
      ['Characters', runQuestion.text.length],
      ['Submitted', runQuestion.at.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit', second: '2-digit'})],
    ]));
  },
  ground() {
    const r = sd('retrieval'), sc = sd('schema'), sm = sd('semantic'), sql = sd('sql');
    let html = inspSection('Input', inspFacts([
      ['Dataset', currentDataset ? currentDataset.name : null],
      ['Question', runQuestion ? runQuestion.text : null],
    ]));
    const steps = ['connect', 'schema', 'semantic', 'retrieval'].filter(k => stageInfo[k]).map(k => {
      const s = stageInfo[k];
      return `<li><span>${escapeHtml(STAGE_NAME[k])}</span><em class="s-${escapeHtml(s.status)}">${escapeHtml(cap1(STATUS_WORD[({running: 'active', done: 'done', error: 'error', skipped: 'skipped'})[s.status] || 'pending']))}</em><b>${s.ms != null ? fmtMs(s.ms) : ''}</b></li>`;
    });
    if (steps.length) html += inspSection('Steps', `<ul class="insp-steps">${steps.join('')}</ul>`);
    if (sc && Array.isArray(sc.table_profiles)) html += inspSection('Schema read', inspFacts([['Tables profiled', sc.table_profiles.length]]));
    if (sm) {
      const count = x => (Array.isArray(x) ? x.length : (x && typeof x === 'object' ? Object.keys(x).length : null));
      html += inspSection('Business meaning', inspFacts([['Tables described', count(sm.table_business_names) ?? count(sm.tables)], ['Metric definitions', count(sm.metric_definitions)], ['Glossary terms', count(sm.domain_glossary)]]));
    }
    if (r) {
      const tables = (r.top_tables || []).slice(0, 8).map(t => `${t.table}${t.business_name ? ' (' + t.business_name + ')' : ''}${typeof t.score === 'number' ? ' · relevance ' + t.score.toFixed(2) : ''}`);
      html += inspSection('Tables chosen for this question', inspList(tables));
      const cols = (r.top_columns || []).slice(0, 10).map(c => `${c.table}.${c.column}${c.role ? ' · ' + c.role : ''}`);
      html += inspSection(`Relevant columns${Array.isArray(r.top_columns) ? ' (' + r.top_columns.length + ')' : ''}`, inspList(cols));
      html += inspSection('Join hints', inspList(r.join_hints));
    }
    if (sql && Array.isArray(sql.grounded_values) && sql.grounded_values.length) {
      html += inspSection('Values matched in your data', inspList(sql.grounded_values.slice(0, 10).map(v => fmtVal(v))));
    } else if (sql) {
      html += inspSection('Values matched in your data', '<p class="insp-muted">None were needed or found for this question.</p>');
    }
    if (lastTrace && lastTrace.available && lastTrace.dataset) html += inspSection('Dataset in the answer trace', inspFacts([['Tables', lastTrace.dataset.table_count], ['Rows', lastTrace.dataset.total_rows]]));
    return html;
  },
  plan() {
    const s = stageInfo.sql, plan = s && s.data && s.data.plan;
    if (!plan || typeof plan !== 'object') return '';
    const rows = [['Tables', plan.tables], ['Measure', plan.measure], ['Aggregation', plan.aggregation], ['Group by', plan.group_by],
      ['Filters', plan.filters], ['Date range', plan.date_range], ['Order', plan.order], ['Row limit', plan.limit], ['Can be re-computed independently', plan.verifiable]];
    return inspSection('Plan', inspFacts(rows.map(([k, v]) => [k, Array.isArray(v) && !v.length ? 'none' : v]))) + inspSection('Planner notes', inspList(plan.notes));
  },
  sql() {
    const d = sd('sql'); if (!d) return '';
    return inspSection('Input', inspFacts([['Question', runQuestion ? runQuestion.text : null], ['Plan available', !!d.plan]])) +
      inspSection('Generated SQL', inspCode(d.sql)) +
      inspSection('Details', inspFacts([['Explanation', d.explanation], ['Confidence', d.confidence], ['Complexity', d.complexity], ['Tables used', d.tables_used], ['Model', d.provider_used]]));
  },
  validation() {
    const d = sd('validation'); if (!d) return '';
    const verdict = d.is_valid === true ? 'Passed' : d.is_valid === false ? 'Rejected' : '—';
    let html = inspSection('Input', inspCode(sd('sql') ? sd('sql').sql : currentSql));
    html += inspSection('Result', inspFacts([['Verdict', verdict], ['Validation score', d.validation_score], ['Risk score', d.risk_score], ['Hallucination score', d.hallucination_score],
      ['Join fan-out risk', d.fanout_risk], ['Joins valid', d.join_validation ? d.join_validation.valid : null], ['Aggregation valid', d.aggregation_validation ? d.aggregation_validation.valid : null]]));
    html += inspSection(`Issues (${(d.issues || []).length})`, inspList((d.issues || []).map(issueText), 'bad'));
    html += inspSection(`Warnings (${(d.warnings || []).length})`, inspList((d.warnings || []).map(issueText), 'warn'));
    return html;
  },
  execution() {
    const d = sd('execution'); if (!d) return '';
    const meta = d.execution_metadata || {};
    let html = inspSection('Input', inspCode(d.sql || currentSql));
    html += inspSection('Result', inspFacts([['Status', d.status], ['Rows returned', d.row_count], ['Columns', d.columns], ['Query time (server)', meta.execution_time_ms != null ? meta.execution_time_ms + ' ms' : null],
      ['Row limit', meta.max_rows], ['Truncated', meta.truncated], ['Query id', d.query_id]]));
    html += inspSection('First rows', previewTable(d.rows));
    const nulls = d.result_profile && d.result_profile.null_counts;
    if (nulls) html += inspSection('Empty values per column', inspFacts(Object.entries(nulls)));
    return html;
  },
  verify() {
    const ex = sd('execution') || {}, rel = sd('reliability');
    let html = '';
    const dimName = {syntax: 'Syntax', security: 'Security', semantic_validation: 'Matches the plan', execution: 'Execution', result_correctness: 'Result correctness'};
    const tone = s => ({pass: 'tag-pass', verified: 'tag-pass', fail: 'tag-alert', mismatch: 'tag-alert'})[s] || (/^(needs_attention|suspicious|unconfirmed|not_verified)$/.test(s) ? 'tag-warn' : 'tag-neutral');
    if (ex.correctness) {
      html += inspSection('Correctness checks', '<ul class="insp-dims">' + Object.entries(ex.correctness).map(([k, v]) =>
        `<li>${inspPill(String(v.status || 'unknown').replaceAll('_', ' '), tone(v.status))}<div><strong>${escapeHtml(dimName[k] || k)}</strong><span>${escapeHtml(String(v.detail || ''))}</span></div></li>`).join('') + '</ul>');
    }
    const ver = ex.verification;
    if (ver) {
      html += inspSection('Independent verification', inspFacts([['Outcome', VERIFY_WORD[ver.status] || ver.status], ['Verified', ver.verified], ['Diagnosis', ver.diagnosis]]) +
        inspList((ver.checks || []).map(c => `${c.check}: ${c.result}${c.detail ? ' — ' + c.detail : ''}`)) + (ver.reference_sql ? inspCode(ver.reference_sql) : ''));
    }
    const semIssues = ((ex.semantic_validation || {}).issues || []).map(issueText);
    html += inspSection('Where the SQL differs from the plan', inspList(semIssues, 'warn'));
    if (rel) {
      html += inspSection('Reliability', inspFacts([['Score', rel.reliability_score != null ? rel.reliability_score + ' / 100' : null], ['Level', rel.reliability_level], ['Answer verified', rel.answer_verified],
        ['Capped because', rel.capped_reason], ['What the score means', rel.score_meaning]]));
      const dims = ['completeness', 'freshness', 'integrity', 'drift', 'uniqueness'].filter(k => rel[k] && typeof rel[k] === 'object');
      html += inspSection('Data checks', '<ul class="insp-dims">' + dims.map(k => `<li>${inspPill(rel[k].status || '—', /pass/i.test(rel[k].status || '') ? 'tag-pass' : 'tag-warn')}<div><strong>${escapeHtml(cap1(k))}${rel[k].score != null ? ' · ' + escapeHtml(String(rel[k].score)) : ''}</strong><span>${escapeHtml(String(rel[k].details || ''))}</span></div></li>`).join('') + '</ul>');
    }
    return html;
  },
  answer() {
    const i = sd('insights'), r = sd('report'); let html = '';
    if (i) {
      html += inspSection('Summary', i.executive_note || i.summary ? `<p class="insp-prose">${escapeHtml(String(i.executive_note || i.summary))}</p>` : '');
      html += inspSection('Key findings', inspList(i.key_findings));
      html += inspSection('Anomalies', inspList(i.anomalies, 'warn'));
      html += inspSection('Recommendations', inspList(i.recommendations));
      html += inspSection('Confidence', inspFacts([['Insight confidence', i.insight_confidence]]));
    }
    const rs = stageInfo.report;
    if (rs) html += inspSection('Report', inspFacts([['Status', cap1(STATUS_WORD[({running: 'active', done: 'done', error: 'error', skipped: 'skipped'})[rs.status] || 'pending'])], ['Report id', r && r.report_id ? shortId(r.report_id) : null], ['Round trip', rs.ms != null ? fmtMs(rs.ms) : null]]));
    return html;
  },
};

function rawBlocks(def) {
  return def.stages.filter(k => stageInfo[k] && stageInfo[k].data).map(k =>
    `<details class="insp-raw"><summary>Raw response: ${escapeHtml(STAGE_NAME[k])}</summary><pre>${escapeHtml(pretty(redactForDisplay(stageInfo[k].data)).slice(0, 8000))}</pre></details>`).join('');
}

function renderStageDetail() {
  const box = $('stageDetail');
  if (!box) return;
  if (!selectedStage) { box.hidden = true; box.innerHTML = ''; document.body.classList.remove('inspector-open'); return; }
  const def = NODE_BY_KEY[selectedStage], ns = nodeStates[selectedStage] || nodeState(selectedStage);
  const idx = NODE_DEFS.indexOf(def);
  const icon = document.querySelector(`.pipeline-step-node[data-step="${selectedStage}"] .node-icon`);
  const timing = [];
  if (ns.ms != null) timing.push(`<span class="insp-ms" title="Measured in your browser from request to response">${fmtMs(ns.ms)} round trip</span>`);
  let html = `<div class="insp-head"><span class="insp-icon s-${ns.state}">${icon ? icon.outerHTML : ''}</span>` +
    `<div class="insp-title"><h3 id="inspTitle" tabindex="-1">${escapeHtml(def.label)}</h3><div class="insp-meta"><span class="stage-status s-${ns.state}">${escapeHtml(cap1(STATUS_WORD[ns.state]))}</span>${timing.join('')}</div></div>` +
    `<div class="insp-nav"><button type="button" class="insp-btn" data-insp="prev" aria-label="Previous stage"${idx === 0 ? ' disabled' : ''}>‹</button>` +
    `<button type="button" class="insp-btn" data-insp="next" aria-label="Next stage"${idx === NODE_DEFS.length - 1 ? ' disabled' : ''}>›</button>` +
    `<button type="button" class="insp-btn insp-close" data-insp="close" aria-label="Close details">×</button></div></div>` +
    `<p class="insp-purpose">${escapeHtml(def.purpose)}</p>`;
  const reachedFailure = NODE_DEFS.map(n => n.key).find(k => nodeStates[k] && nodeStates[k].state === 'error');
  if (ns.state === 'error') html += `<div class="insp-callout bad" role="note"><strong>This stage failed.</strong>${ns.note ? `<span>${escapeHtml(ns.note)}</span>` : '<span>The backend did not give a reason.</span>'}</div>`;
  else if (ns.state === 'warn') html += `<div class="insp-callout warn" role="note"><strong>Completed with a warning.</strong>${ns.note ? `<span>${escapeHtml(ns.note)}</span>` : ''}</div>`;
  else if (ns.state === 'na') html += `<div class="insp-callout muted" role="note"><strong>Details unavailable.</strong><span>${escapeHtml(ns.note || 'This stage did not record any details.')}</span></div>`;
  else if (ns.state === 'skipped') html += `<div class="insp-callout muted" role="note"><strong>Skipped.</strong><span>${escapeHtml(ns.note || 'This stage did not run for this query.')}</span></div>`;
  else if (ns.state === 'active') html += '<div class="insp-callout live" role="note"><strong>Running now.</strong><span>Details appear here when the backend responds.</span></div>';
  else if (ns.state === 'pending') {
    const why = reachedFailure && NODE_DEFS.findIndex(n => n.key === reachedFailure) < idx ? `This stage did not run because ${NODE_BY_KEY[reachedFailure].label} failed.` : 'This stage has not run yet in this session.';
    html += `<div class="insp-callout muted" role="note"><strong>Details unavailable.</strong><span>${escapeHtml(why)}</span></div>`;
  }
  const body = NODE_BODY[selectedStage] ? NODE_BODY[selectedStage]() : '';
  if (ns.state !== 'pending' && !body && ns.state !== 'error' && ns.state !== 'na' && ns.state !== 'skipped' && ns.state !== 'active') html += '<p class="insp-muted">The backend recorded no further details for this stage.</p>';
  html += body + rawBlocks(def);
  box.innerHTML = html; box.hidden = false;
  document.body.classList.add('inspector-open');
}

function selectStage(step, opts = {}) {
  const closing = selectedStage === step && !opts.keepOpen;
  selectedStage = closing ? null : step;
  refreshStages({userSelected: true});
  if (selectedStage && opts.focusPanel) { const t = $('inspTitle'); if (t) t.focus(); }
}
function closeInspector(restoreFocus) {
  if (!selectedStage) return;
  const key = selectedStage; selectedStage = null; refreshStages({userSelected: true});
  if (restoreFocus) { const b = document.querySelector(`.pipeline-step-node[data-step="${key}"] .stage-btn`); if (b) b.focus(); }
}

function refreshStages(opts = {}) {
  const states = {};
  NODE_DEFS.forEach(n => { states[n.key] = nodeState(n.key); });
  nodeStates = states;
  document.querySelectorAll('.pipeline-step-node').forEach(li => {
    const key = li.dataset.step, st = states[key], def = NODE_BY_KEY[key];
    const b = li.querySelector('.stage-btn');
    if (!b || !st || !def) return;
    li.classList.remove('done', 'active', 'warn', 'error', 'skipped', 'na');
    if (st.state !== 'pending') li.classList.add(st.state);
    li.classList.toggle('selected', selectedStage === key);
    b.setAttribute('aria-pressed', String(selectedStage === key));
    b.setAttribute('aria-label', `${def.label}: ${STATUS_WORD[st.state]}`);
    const label = li.querySelector('.node-status'); if (label) label.textContent = cap1(STATUS_WORD[st.state]);
  });
  drawEdges();
  renderStageDetail();
  keepActiveInView(!!opts.userSelected);
}

function wireStages() {
  const list = $('pipeList');
  if (!list) return;
  list.addEventListener('click', ev => {
    const d = ev.target.closest('.node-details-btn');
    if (d) { selectStage(d.dataset.detailsFor, {keepOpen: true, focusPanel: true}); return; }
    const b = ev.target.closest('.stage-btn'); if (b) selectStage(b.closest('.pipeline-step-node').dataset.step);
  });
  list.addEventListener('keydown', ev => {
    const btns = Array.from(list.querySelectorAll('.stage-btn'));
    const i = btns.indexOf(document.activeElement);
    if (i < 0) return;
    const go = {ArrowRight: i + 1, ArrowDown: i + 1, ArrowLeft: i - 1, ArrowUp: i - 1, Home: 0, End: btns.length - 1}[ev.key];
    if (go !== undefined && btns[go]) { ev.preventDefault(); btns[go].focus(); }
  });
  $('stageDetail').addEventListener('click', ev => {
    const b = ev.target.closest('[data-insp]'); if (!b || b.disabled) return;
    const idx = NODE_DEFS.findIndex(n => n.key === selectedStage);
    if (b.dataset.insp === 'close') closeInspector(true);
    else if ((b.dataset.insp === 'prev' && idx > 0) || (b.dataset.insp === 'next' && idx < NODE_DEFS.length - 1)) {
      const which = b.dataset.insp;
      selectStage(NODE_DEFS[idx + (which === 'next' ? 1 : -1)].key, {keepOpen: true});
      const nb = document.querySelector(`#stageDetail [data-insp="${which}"]`);
      if (nb && !nb.disabled) nb.focus(); else if ($('inspTitle')) $('inspTitle').focus();
    }
  });
  document.addEventListener('keydown', ev => {
    if (ev.key !== 'Escape' || !selectedStage) return;
    if (!$('settingsModal').hidden || document.querySelector('.menu-pop:not([hidden])')) return;          // the settings dialog owns Escape while it is open
    const inside = $('stageDetail').contains(document.activeElement);
    closeInspector(inside || list.contains(document.activeElement));
  });
  if (window.ResizeObserver) new ResizeObserver(() => drawEdges()).observe($('wfCanvas'));
  window.addEventListener('resize', drawEdges);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(drawEdges);
  refreshStages();
}

function markStep(step, state = 'active') {
  if (state === 'active') stageStart(step);
  else if (state === 'error') stageEnd(step, undefined, 'error', (typeof lastStageError === 'string' && lastStageError) || '');
}
function finishTo() { refreshStages(); }


function setBar(id, val) {
  const el = $(id);
  if (el) el.style.width = clamp(val) + '%';
}

function renderTable(rows) {
  lastRows = rows || [];
  const table = $('resultsTable'), thead = table.querySelector('thead'), tbody = table.querySelector('tbody');
  thead.innerHTML = ''; tbody.innerHTML = '';
  if (!rows || !rows.length) {
    tbody.innerHTML = '<tr><td colspan="99" class="table-empty">The query ran but returned no rows.</td></tr>';
  } else {
    const keys = Object.keys(rows[0]);
    const numeric = keys.map(k => rows.every(r => r[k] === null || r[k] === undefined || typeof r[k] === 'number'));
    thead.innerHTML = '<tr>' + keys.map((k, i) => `<th class="${numeric[i] ? 'num' : ''}">${escapeHtml(k)}</th>`).join('') + '</tr>';
    tbody.innerHTML = rows.slice(0, 200).map(r => '<tr>' + keys.map(k => {
      const v = r[k];
      if (v === null || v === undefined) return '<td class="null">null</td>';
      if (typeof v === 'number') return `<td class="num">${v.toLocaleString()}</td>`;
      return `<td>${escapeHtml(v)}</td>`;
    }).join('') + '</tr>').join('');
    if (rows.length > 200) tbody.innerHTML += `<tr><td colspan="${keys.length}" class="table-empty">Showing the first 200 of ${rows.length.toLocaleString()} rows. Download the CSV for all of them.</td></tr>`;
  }
  $('emptyState').style.display = 'none';
  $('resultsState').style.display = 'block';
  $('resultsState').classList.remove('hidden-element');
}

function highlightSql(sql) {
  const esc = (t) => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const KW = /^(select|from|where|group|order|by|join|left|right|inner|outer|on|as|and|or|not|in|is|null|limit|having|distinct|case|when|then|else|end|desc|asc|union|with|between|like|over|partition)$/i;
  const FN = /^(count|sum|avg|min|max|round|coalesce|strftime|date|cast|lower|upper|length)$/i;
  const re = /(--[^\n]*)|('(?:[^']|'')*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|([(),.*=<>+\-\/;]+)|(\s+)|(.)/g;
  let out = '', m;
  while ((m = re.exec(sql))) {
    if (m[1]) out += `<span class="sql-cm">${esc(m[1])}</span>`;
    else if (m[2]) out += `<span class="sql-str">${esc(m[2])}</span>`;
    else if (m[3]) out += `<span class="sql-num">${m[3]}</span>`;
    else if (m[4]) out += KW.test(m[4]) ? `<span class="sql-kw">${m[4]}</span>` : (FN.test(m[4]) && sql[re.lastIndex] === '(' ? `<span class="sql-fn">${m[4]}</span>` : esc(m[4]));
    else if (m[5]) out += `<span class="sql-pn">${esc(m[5])}</span>`;
    else out += esc(m[0]);
  }
  return out;
}
const CHART_PALETTE = ['#6cb0ff', '#72e4c4', '#3f7fe0', '#a8c7ff', '#3fbf9f', '#8a9bd6'];

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function truncateLabel(s, maxLen = 14) {
  s = String(s);
  return s.length > maxLen ? s.slice(0, maxLen - 1) + '…' : s;
}
function detectDateColumn(cols, sampleRow) {
  const nameHint = /(date|month|year|day|time|created|updated|period)/i;
  for (const c of cols) if (nameHint.test(c)) return c;
  for (const c of cols) {
    const v = sampleRow[c];
    if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}/.test(v)) return c;
  }
  return null;
}

function isNumericColumn(rows, col) {
  let sawNumber = false;
  for (const r of rows) {
    const v = r[col];
    if (v === null || v === undefined) continue;
    if (typeof v === 'number' && Number.isFinite(v)) { sawNumber = true; continue; }
    return false;
  }
  return sawNumber;
}

const ID_LIKE_RE = /(^|_)(id|uuid|guid|pk)$/i;
const METRIC_NAME_HINTS = ['revenue', 'sales', 'total', 'sum', 'amount', 'price', 'cost', 'profit', 'margin', 'count', 'quantity', 'qty'];

function pickMetricColumn(rows, cols) {
  const numCols = cols.filter(c => isNumericColumn(rows, c));
  if (!numCols.length) return null;
  const varying = numCols.filter(c => new Set(rows.map(r => r[c]).filter(v => typeof v === 'number')).size >= 2);
  const candidates = varying.length ? varying : numCols;
  const scored = candidates.map((c, i) => {
    const lower = c.toLowerCase();
    let score = 0;
    if (ID_LIKE_RE.test(lower)) score -= 10;
    // period / rank columns are axes or orderings, not the measure to plot
    if (/(^|_)(year|month|quarter|week|day|rank|rn|row_?num(ber)?|rownum)($|_)/.test(lower)) score -= 8;
    if (METRIC_NAME_HINTS.some(h => lower.includes(h))) score += 1;
    return { c, i, score };
  });
  scored.sort((a, b) => b.score - a.score || a.i - b.i);
  return scored[0].c;
}

function buildBarSVG(data) {
  const W = 400, H = 200, pad = 18;
  const plotTop = 14, plotBottom = H - 30;
  const sorted = data.map((d, i) => ({ label: d.label, value: d.value, _i: i })).sort((a, b) => b.value - a.value);
  const vals = sorted.map(d => d.value);
  const max = Math.max(...vals, 0);
  const min = Math.min(...vals, 0);
  const range = (max - min) || 1;
  const scale = (plotBottom - plotTop) / range;
  const zeroY = plotBottom - (0 - min) * scale;
  const n = sorted.length || 1;
  const bw = (W - pad * 2) / n;

  const grid = buildYAxisGrid(min, max, scale, plotBottom, pad, W);

  let bars = '';
  let labels = '';
  // Category labels only when each bar has room for a readable one; the tooltip always carries the full label.
  const showLabels = bw >= 22;
  const maxChars = Math.max(3, Math.floor(bw / 5.4));
  sorted.forEach((d, i) => {
    const barY = plotBottom - (d.value - min) * scale;
    const y = Math.min(barY, zeroY);
    const h = Math.max(Math.abs(barY - zeroY), 1);
    const x = pad + i * bw + bw * 0.15;
    const w = bw * 0.7;
    const peakClass = i === 0 ? ' bar-peak' : '';
    bars += `<rect class="chart-bar-rect${peakClass}" data-i="${d._i}" tabindex="0" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}" rx="2"></rect>`;
    if (showLabels) labels += `<text class="chart-x-label" x="${(x + w / 2).toFixed(1)}" y="${H - 12}">${escapeHtml(truncateLabel(d.label, maxChars))}</text>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" overflow="visible" role="img" aria-label="Bar chart">${grid}${bars}${labels}</svg>`;
}

function buildYAxisGrid(min, max, scale, plotBottom, pad, W) {
  const candidates = [max, (max + min) / 2, min];
  const seen = new Set();
  let out = '';
  candidates.forEach((v) => {
    const key = v.toFixed(4);
    if (seen.has(key)) return;
    seen.add(key);
    const gy = plotBottom - (v - min) * scale;
    out += `<line class="chart-grid-line" x1="${pad + 6}" y1="${gy.toFixed(1)}" x2="${(W - pad + 10).toFixed(1)}" y2="${gy.toFixed(1)}"></line>`;
    out += `<text class="chart-axis-label" x="-22" y="${(gy + 3).toFixed(1)}">${escapeHtml(numFmt(v))}</text>`;
  });
  return out;
}

function buildLineSVG(data) {
  const W = 400, H = 200, pad = 18;
  const plotTop = 14, plotBottom = H - 30;
  const vals = data.map(d => d.value);
  const max = Math.max(...vals, 1);
  const min = Math.min(...vals, 0);
  const range = (max - min) || 1;
  const scale = (plotBottom - plotTop) / range;
  const n = data.length;
  const stepX = n > 1 ? (W - pad * 2) / (n - 1) : 0;
  const pts = data.map((d, i) => ({
    x: pad + i * stepX,
    y: plotBottom - (d.value - min) * scale,
    d,
  }));
  const linePath = pts.map((p, i) => (i === 0 ? 'M' : 'L') + p.x.toFixed(1) + ' ' + p.y.toFixed(1)).join(' ');
  const areaPath = pts.length ? linePath + ` L${pts[pts.length - 1].x.toFixed(1)} ${plotBottom} L${pts[0].x.toFixed(1)} ${plotBottom} Z` : '';
  const grid = buildYAxisGrid(min, max, scale, plotBottom, pad, W);
  let dots = '';
  pts.forEach((p, i) => {
    dots += `<circle class="chart-line-dot" data-i="${i}" tabindex="0" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}"></circle>`;
  });
  // Label the first, middle and last point so the time axis is readable without crowding.
  let labels = '';
  const marks = n > 2 ? [0, Math.floor((n - 1) / 2), n - 1] : n === 2 ? [0, 1] : [0];
  marks.forEach((i, k) => {
    const anchor = k === 0 && marks.length > 1 ? 'start' : (k === marks.length - 1 && marks.length > 1 ? 'end' : 'middle');
    labels += `<text class="chart-x-label" style="text-anchor:${anchor}" x="${pts[i].x.toFixed(1)}" y="${H - 12}">${escapeHtml(truncateLabel(data[i].label, 12))}</text>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" overflow="visible" role="img" aria-label="Line chart">${grid}<path class="chart-line-area" d="${areaPath}"></path><path class="chart-line-path" d="${linePath}"></path>${dots}${labels}</svg>`;
}

function buildPieSVG(data) {
  const size = 150, r = 62, cx = size / 2, cy = size / 2;
  const total = data.reduce((s, d) => s + Math.max(d.value, 0), 0) || 1;
  let angle = -Math.PI / 2;
  let slices = '';
  data.forEach((d, i) => {
    const frac = Math.max(d.value, 0) / total;
    const start = angle;
    const end = angle + frac * Math.PI * 2;
    const x1 = cx + r * Math.cos(start), y1 = cy + r * Math.sin(start);
    const x2 = cx + r * Math.cos(end), y2 = cy + r * Math.sin(end);
    const large = (end - start) > Math.PI ? 1 : 0;
    const color = CHART_PALETTE[i % CHART_PALETTE.length];
    const path = frac >= 0.9999
      ? `M${cx} ${cy - r} A${r} ${r} 0 1 1 ${(cx - 0.01).toFixed(2)} ${cy - r} Z`
      : `M${cx} ${cy} L${x1.toFixed(2)} ${y1.toFixed(2)} A${r} ${r} 0 ${large} 1 ${x2.toFixed(2)} ${y2.toFixed(2)} Z`;
    slices += `<path class="chart-pie-slice" data-i="${i}" tabindex="0" d="${path}" fill="${color}"></path>`;
    angle = end;
  });
  return `<svg viewBox="0 0 ${size} ${size}" role="img" aria-label="Pie chart" style="width:${size}px;height:${size}px;margin:0 auto;display:block">${slices}</svg>`;
}

function renderLegend(container, data) {
  container.innerHTML = data.map((d, i) => `
    <div class="chart-legend-item">
      <i style="background:${CHART_PALETTE[i % CHART_PALETTE.length]}"></i>
      <span>${escapeHtml(truncateLabel(d.label))} (${d.value.toLocaleString()})</span>
    </div>`).join('');
}

let chartPinnedIndex = null;

function formatChartValue(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return String(v);
  return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function positionTooltip(wrapEl, tooltipEl, targetEl) {
  const wrapRect = wrapEl.getBoundingClientRect();
  const targetRect = targetEl.getBoundingClientRect();
  const left = targetRect.left + targetRect.width / 2 - wrapRect.left;
  const top = targetRect.top - wrapRect.top;
  tooltipEl.style.left = left.toFixed(0) + 'px';
  tooltipEl.style.top = Math.max(top, 4).toFixed(0) + 'px';
}

function showTooltip(wrapEl, tooltipEl, targetEl, label, value, extra) {
  tooltipEl.innerHTML = `<div>${escapeHtml(label)}</div><div class="tt-value">${escapeHtml(value)}${extra ? ' · ' + escapeHtml(extra) : ''}</div>`;
  positionTooltip(wrapEl, tooltipEl, targetEl);
  tooltipEl.classList.add('visible');
}
function hideTooltip(tooltipEl) { tooltipEl.classList.remove('visible'); }

document.addEventListener('click', (e) => {
  if (chartPinnedIndex === null) return;
  if (e.target.closest && e.target.closest('#chartCanvas')) return;
  chartPinnedIndex = null;
  const tt = $('chartTooltip');
  if (tt) hideTooltip(tt);
});

function wireChartTooltips(wrapEl, canvasEl, tooltipEl, data, opts) {
  chartPinnedIndex = null;
  const total = (opts && opts.total) || null;
  canvasEl.querySelectorAll('[data-i]').forEach((el) => {
    const i = parseInt(el.getAttribute('data-i'), 10);
    const d = data[i];
    if (!d) return;
    const extra = total ? ((Math.max(d.value, 0) / total) * 100).toFixed(1) + '%' : null;
    const reveal = () => showTooltip(wrapEl, tooltipEl, el, d.label, formatChartValue(d.value), extra);
    el.addEventListener('mouseenter', reveal);
    el.addEventListener('focus', reveal);
    el.addEventListener('mouseleave', () => { if (chartPinnedIndex !== i) hideTooltip(tooltipEl); });
    el.addEventListener('blur', () => { if (chartPinnedIndex !== i) hideTooltip(tooltipEl); });
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      if (chartPinnedIndex === i) { chartPinnedIndex = null; hideTooltip(tooltipEl); return; }
      chartPinnedIndex = i;
      reveal();
    });
  });
}

function renderChart(rows) {
  const area = $('chartArea');
  if (!rows || rows.length < 2) {
    if (area) area.classList.add('hidden-element');
    return;
  }

  const cols = Object.keys(rows[0]);
  const numCol = pickMetricColumn(rows, cols);
  if (!numCol) {
    if (area) area.classList.add('hidden-element');
    return;
  }

  const otherCols = cols.filter(c => c !== numCol);
  const dateCol = detectDateColumn(otherCols, rows[0]);
  const labelCol = dateCol || otherCols.find(c => !isNumericColumn(rows, c)) || otherCols[0];

  // Rendering a chart point for every returned row is neither useful nor
  // legible past a certain count, so the chart caps how many POINTS it
  // draws -- but this is a display limit, not a data limit: it doesn't
  // touch how many rows the backend returned or that the table/CSV/JSON
  // export show ("top 20" still returns and downloads as 20 rows; the
  // chart plots up to CHART_MAX_POINTS of them). Configurable in one
  // place rather than hardcoded inline like the previous 10/8 caps were.
  const CHART_MAX_POINTS = 30;
  const chartSubset = rows.slice(0, CHART_MAX_POINTS);
  const buildData = (subset) => subset.map((r, i) => ({
    label: labelCol ? String(r[labelCol] ?? `Row ${i + 1}`) : `Row ${i + 1}`,
    value: typeof r[numCol] === 'number' ? r[numCol] : 0,
  }));
  const data = buildData(chartSubset);

  const distinctLabels = new Set(data.map(d => d.label));
  let type = 'bar';
  // Pie slices must be additive shares: not for rates/averages/percentages or negatives.
  const nonAdditive = /(pct|percent|rate|ratio|avg|average|mean|margin|growth|price|score|rating|median|discount)/i.test(numCol)
    || data.some(d => d.value < 0);
  if (dateCol) {
    type = 'line';
    // ISO-like labels sort chronologically; keep the line in time order regardless of row order
    if (data.every(d => /^\d{4}(-\d{2}){0,2}/.test(d.label))) data.sort((a, b) => a.label < b.label ? -1 : a.label > b.label ? 1 : 0);
  }
  // Pie/donut only reads well with a small number of distinct slices;
  // this is a chart-type suitability check, not a truncation of results --
  // datasets with more distinct categories simply render as a bar chart
  // instead, still showing up to CHART_MAX_POINTS of them.
  else if (!nonAdditive && distinctLabels.size <= 6 && distinctLabels.size === data.length) type = 'pie';

  $('chartTypeBadge').textContent = type === 'line' ? 'Line chart' : type === 'pie' ? 'Pie chart' : 'Bar chart';

  const dimLabel = labelCol || 'row';
  let reason = '';
  if (type === 'line') reason = `Trend over time, by "${dateCol}"`;
  else if (type === 'pie') reason = `Share of "${numCol}" by "${dimLabel}"`;
  else reason = `"${numCol}" compared across "${dimLabel}"`;

  $('chartReason').textContent = reason;

  const canvas = $('chartCanvas');
  const legend = $('chartLegend');
  const tooltip = $('chartTooltip');
  const wrap = $('chartCanvasWrap');
  legend.innerHTML = '';
  hideTooltip(tooltip);

  if (type === 'bar') {
    canvas.innerHTML = buildBarSVG(data);
    wireChartTooltips(wrap, canvas, tooltip, data);
  } else if (type === 'line') {
    canvas.innerHTML = buildLineSVG(data);
    wireChartTooltips(wrap, canvas, tooltip, data);
  } else {
    canvas.innerHTML = buildPieSVG(data);
    renderLegend(legend, data);
    const total = data.reduce((s, d) => s + Math.max(d.value, 0), 0);
    wireChartTooltips(wrap, canvas, tooltip, data, { total });
  }

  area.classList.remove('hidden-element');
}



function renderReliabilityChecks(rel) {
  const checks = [
    { name: 'Completeness', key: 'completeness' },
    { name: 'Freshness', key: 'freshness' },
    { name: 'Integrity', key: 'integrity' },
    { name: 'Drift', key: 'drift' },
    { name: 'Uniqueness', key: 'uniqueness' },
  ];
  $('relChecks').innerHTML = checks.map(c => {
    const data = rel[c.key] || {};
    const score = data.score ?? '?';
    const status = data.status || '?';
    const tagClass = status === 'PASS' ? 'tag-pass' : 'tag-alert';
    return `<div class="rel-row">
      <span>${c.name}</span>
      <div>
        <span class="text-mono">${escapeHtml(String(score))}/100</span>
        <span class="pill-tag ${tagClass}">${escapeHtml(String(status))}</span>
      </div>
    </div>`;
  }).join('') + renderCorrectnessDimensions(rel);
}

// Honest, separate reporting: passing syntax/security/execution never implies the answer is right.
function renderCorrectnessDimensions(rel) {
  const d = rel && rel.dimensions;
  if (!d) return '';
  const rows = [
    ['Syntax', d.syntax], ['Security', d.security], ['Semantic match', d.semantic_validation],
    ['Execution', d.execution], ['Result correctness', d.result_correctness],
  ].filter(r => r[1]);
  const good = new Set(['pass', 'verified']);
  const bad = new Set(['fail', 'mismatch', 'suspicious', 'needs_attention']);
  return '<div class="rel-dim-title" title="' + escapeHtml(rel.score_meaning || '') + '">What was actually checked</div>' + rows.map(([name, v]) => {
    const st = String(v.status || '?');
    const cls = good.has(st) ? 'tag-pass' : bad.has(st) ? 'tag-alert' : 'tag-neutral';
    return `<div class="rel-row" title="${escapeHtml(v.detail || '')}"><span>${name}</span><div><span class="pill-tag ${cls}">${escapeHtml(st.replace('_', ' '))}</span></div></div>`;
  }).join('');
}

function renderBulletList(id, items, cls, icon) {
  const el = $(id);
  if (!items || !items.length) {
    el.innerHTML = `<div class="bullet-note-node"><span class="bullet-mark">·</span><span class="muted">Nothing to report.</span></div>`;
    return;
  }
  el.innerHTML = items.map(x => `<div class="bullet-note-node"><strong>${icon}</strong><span>${escapeHtml(x)}</span></div>`).join('');
}

function renderValidationTab(v) {
  const issues = v.issues || [];
  const warnings = v.warnings || [];
  let html = `<div class="chip-row">
    <span class="pill-tag ${v.is_valid ? 'tag-pass' : 'tag-alert'}">${v.is_valid ? 'Passed validation' : 'Failed validation'}</span>
    <span class="pill-tag tag-neutral">Score ${escapeHtml(String(v.validation_score ?? '—'))}/100</span>
    <span class="pill-tag tag-neutral">Hallucination risk ${((v.hallucination_score||0)*100).toFixed(0)}%</span>
    <span class="pill-tag tag-neutral">Join fan-out risk ${escapeHtml(String(v.fanout_risk||'Low'))}</span>
  </div>`;

  if (issues.length) {
    html += `<div><div class="check-group-title">Problems found</div>`;
    html += issues.map(i => `<div class="bullet-note-node issue"><strong>!</strong><span>${escapeHtml(i.message)}</span></div>`).join('');
    html += '</div>';
  }
  if (warnings.length) {
    html += `<div><div class="check-group-title">Warnings</div>`;
    html += warnings.map(w => `<div class="bullet-note-node"><strong>▲</strong><span>${escapeHtml(w)}</span></div>`).join('');
    html += '</div>';
  }
  $('valContent').innerHTML = html;
}

function renderRetrievalTab(r) {
  const tables = r.top_tables || [];
  if (!tables.length) { $('retrievalContent').innerHTML = '<p class="muted">No tables matched this question.</p>'; return; }
  $('retrievalContent').innerHTML = tables.map(t => `<div class="table-hit">
    <code>${escapeHtml(t.table)}</code>
    <span class="pill-tag tag-active">${Math.round(t.score*100)}% match</span>
  </div>`).join('');
}

function csvFromRows(rows) {
  if (!rows.length) return '';
  const keys = Object.keys(rows[0]);
  return [keys.join(',')].concat(rows.map(r => keys.map(k => JSON.stringify(r[k] ?? '')).join(','))).join('\n');
}
// Single, non-labeled final helper architecture functions
function download(name, text, type='text/plain') {
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([text], {type})); a.download = name; a.click();
}

// View navigation (rail), answer-sheet index and result chart/table toggle
document.querySelectorAll('.nav-tab[data-view]').forEach(b => { b.onclick = () => switchView(b.dataset.view); });
document.querySelectorAll('.sheet-index button').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('.sheet-index button').forEach(x => x.classList.remove('current'));
    b.classList.add('current');
    openFold(b.dataset.jump, true);
  };
});
document.querySelectorAll('#resultSeg button').forEach(b => { b.onclick = () => setResultView(b.dataset.resultView); });
$('schemaList').addEventListener('click', (e) => { const b = e.target.closest('.ds-table-btn'); if (b) selectTable(b.dataset.table); });
$('dsDetail').addEventListener('click', (e) => { const b = e.target.closest('[data-goto]'); if (b) { selectTable(b.dataset.goto); document.querySelector('.ds-table-btn.selected')?.scrollIntoView({block: 'nearest'}); } });
$('wsEvalRefresh').onclick = () => loadWorkspaceEvaluation(true);

// Chips triggers wire
document.querySelectorAll('.suggest-chip').forEach(c => {
  c.onclick = () => { $('question').value = c.dataset.q; $('question').focus(); };
});

// Clipboard operations mapping
$('copySqlBtn').onclick = () => {
  if (currentSql) navigator.clipboard.writeText(currentSql).then(() => { $('copySqlBtn').textContent = 'Copied'; setTimeout(() => $('copySqlBtn').textContent = 'Copy SQL', 1500); });
};

async function newSession() {
  const res = sessionId
    ? await post('/api/session/flush', {session_id: sessionId})
    : await post('/api/session', {});
  if (res.success) {
    sessionId = res.session_id;
    refreshSettingsView();
    resetWorkspaceUI();
    $('question').value = '';   // a flushed session starts with an empty question box too
  }
}

function formatMoney(v) {
  // FIX: previously always .toFixed(4) ($0.0034), inconsistent with the
  // budget total's 2-decimal format and not how a normal product shows
  // currency. Rounds to cents, but a nonzero cost that rounds to $0.00
  // shows as "<$0.01" instead -- so a real (if tiny) charge never reads as
  // "this was free."
  if (v > 0 && v < 0.005) return '<$0.01';
  return `$${v.toFixed(2)}`;
}
async function refreshUsage() {
  const u = await apiGet('/api/usage');
  if (!u || u.success === false) return;
  $('usageRequests').textContent = u.requests;
  $('usageCost').textContent = `${formatMoney(u.estimated_cost_usd)} / $${u.budget_usd.toFixed(2)}`;
  const pct = u.budget_usd > 0 ? Math.min(100, (u.estimated_cost_usd / u.budget_usd) * 100) : 0;
  setBar('usageBarFill', pct);
}
async function refreshAccountUsage() {
  const u = await apiGet('/api/account/usage');
  if (!u || u.success === false) return;
  accountUsage = u;
  refreshSettingsView();
}

async function refreshBillingState() {
  const b = await apiGet('/api/billing');
  if (!b || b.success === false) return;
  billingState = b;
  refreshSettingsView();
}

async function ensureSession() {
  if (sessionId) return true;
  const res = await post('/api/session', {});
  if (!res.success) { showGlobalError(res.error || 'Could not create a workspace session.', res.request_id); return false; }
  sessionId = res.session_id; refreshSettingsView();
  return true;
}

// The server always ends a job: succeeded, failed, or JOB_INTERRUPTED / JOB_STALE once nothing can finish it (at most about
// 27 minutes). So keep polling past that instead of giving up after 3 minutes while a slow job (an Excel file on a 0.1-CPU
// free instance takes minutes) is still working and would succeed.
const JOB_POLL_LIMIT_MS = 30 * 60 * 1000;

async function pollJob(jobId, label) {
  const started = Date.now();
  while (Date.now() - started < JOB_POLL_LIMIT_MS) {
    const res = await apiGet('/api/jobs/' + encodeURIComponent(jobId));
    if (!res.success) throwApiError(res, label + ' failed.');
    if (res.status === 'succeeded') return res.result || {};
    // The worker's reason ("Excel workbook is not a valid ZIP container.") tells the user what to fix; shown as text.
    if (res.status === 'failed') throw new Error(res.error ? `${label} failed: ${res.error}` : label + ' failed. Please try again.');
    const secs = Math.round((Date.now() - started) / 1000);
    setPipelineState(label + ' · ' + (res.status || 'queued') + ' · ' + secs + 's');
    await sleep(secs < 60 ? 1000 : 3000);   // poll less often once it is clearly a long job (less load on a small instance)
  }
  throw new Error(label + ' timed out.');
}

async function connectSample() {
  if (pipelineIsBusy) return;
  setPipelineBusy(true);
  try { await connectSampleInner(); } finally { setPipelineBusy(false); }
}

async function connectSampleInner(opts = {}) {
  markStep('connect','active'); $('runProgress').hidden = false; setPipelineState('Connecting the sample database…');
  clearGlobalError();
  if (!(await ensureSession())) return;
  const res = await post('/api/connect/sample', {session_id: sessionId});
  if (!res.success) {
    markStep('connect','error');
    setPipelineState('Could not connect');
    showGlobalError(res.error || 'Could not connect the sample dataset.', res.request_id);
    return;
  }
  sessionId = res.session_id; refreshSettingsView();
  if (opts.withinRun) datasetEpoch += 1;   // first connect inside a run: keep that run's question on screen
  else onDatasetActivated();
  dbConnected = true;
  currentDataset = { name: res.db_name || 'Sample Database', tables: res.table_count, rows: res.total_rows, size: res.db_size, sample: true };
  refreshSettingsView();
  setStatus('Connected · ' + (res.db_name || 'Sample Database'), true);
  renderSchemaList(res.tables);
  stageDone('connect', res); setPipelineState('Connected');
}

async function uploadFile(file) {
  // Ask is locked while an upload is in flight, so no answer for the previous dataset can land after the switch.
  if (pipelineIsBusy) return;
  setPipelineBusy(true);
  try { await uploadFileInner(file); } finally { setPipelineBusy(false); }
}

async function uploadFileInner(file) {
  markStep('connect','active'); $('runProgress').hidden = false; setPipelineState('Uploading your file…');
  clearGlobalError();
  const fd = new FormData(); fd.append('file', file); fd.append('session_id', sessionId);
  if (!(await ensureSession())) return;
  fd.set('session_id', sessionId);
  const res = await postForm('/api/connect/upload', fd);
  if (!res.success) {
    markStep('connect','error');
    setPipelineState('Upload failed');
    // Covers both "upload failure" (network/size/auth) and "malformed
    // file" (backend returns a specific 422 message for bad CSV/JSON/
    // Excel/SQLite content) -- both land here with the server's own
    // actionable message, shown the same way as every other pipeline
    // error instead of a blocking native alert().
    showGlobalError(res.error || 'Upload failed.', res.request_id);
    return;
  }
  sessionId = res.session_id; refreshSettingsView();
  setPipelineState('Upload queued…');
  let completed;
  try { completed = await pollJob(res.job_id, 'Dataset upload'); }
  catch (e) {
    // A failed upload never activates anything: the previously connected dataset (if any) stays active and visible.
    markStep('connect','error'); setPipelineState('Upload failed'); showGlobalError(e.message || String(e), e.requestId); return;
  }
  onDatasetActivated();
  dbConnected = true;
  currentDataset = { name: completed.db_name || 'Uploaded dataset', tables: completed.table_count, rows: completed.total_rows, size: completed.db_size, sample: false };
  refreshSettingsView();
  setStatus('Connected · ' + (completed.db_name || 'Uploaded dataset'), true);
  renderSchemaList(completed.tables);
  stageDone('connect', completed); setPipelineState('Connected');
  switchView('datasets');
}

function throwApiError(res, fallbackMessage) {
  const err = new Error(res && res.error ? res.error : (fallbackMessage || 'Request failed.'));
  err.requestId = res && res.request_id;
  // The daily-quota banner is already on screen (see _handleResponse); callers must not replace it with a generic fault.
  err.quotaReached = !!(res && res.code === 'QUERY_LIMIT_REACHED');
  throw err;
}

async function runAll() {
  const q = $('question').value.trim();
  if (!q) { showGlobalError('Type a question first, then press Ask.'); $('question').focus(); return; }
  setPipelineBusy(true);
  switchView('ask');
  clearGlobalError();
  clearSheet();
  $('sheetQuestion').textContent = q;
  $('answerSheet').classList.remove('hidden-element');
  $('runProgress').hidden = false;
  $('emptyState').style.display = 'none';
  $('runProgress').scrollIntoView({behavior: 'smooth', block: 'nearest'});
  runQuestion = {text: q, at: new Date()}; refreshStages();

  try {
    // `dbConnected` is updated in lockstep by connectSample/uploadFile/resetWorkspaceUI, so a flushed session is never
    // mistaken for a connected one.
    if (!dbConnected) {
      await connectSampleInner({ withinRun: true });   // runAll already holds the busy lock
      await sleep(200);
      if (!dbConnected) throw new Error('Connect a dataset first, then ask again.');
    }

    markStep('schema','active'); setPipelineState('Reading the schema…');
    const schema = await post('/api/schema-intelligence', {session_id: sessionId});
    if (!schema.success) throwApiError(schema);
    stageDone('schema', schema); await sleep(150);

    markStep('semantic','active'); setPipelineState('Adding business meaning…');
    const semantic = await post('/api/semantic-layer', {session_id: sessionId});
    if (!semantic.success) throwApiError(semantic);
    stageDone('semantic', semantic); await sleep(150);

    let sqlRes;
    markStep('retrieval','active'); setPipelineState('Finding relevant tables…');
    const retrieval = await post('/api/retrieve-tables', {session_id: sessionId, question: q});
    if (!retrieval.success) throwApiError(retrieval);
    renderRetrievalTab(retrieval);
    { const n = (retrieval.relevant_tables || retrieval.top_tables || []).length; setBadge('badgeExplain', n + ' table' + (n === 1 ? '' : 's'), 'muted'); }
    stageDone('retrieval', retrieval); await sleep(150);

    markStep('sql','active'); setPipelineState('Writing SQL…');
    const genBody = {session_id: sessionId, question: q};
    if (apiKeyOverride) genBody.api_key = apiKeyOverride;
    sqlRes = await post('/api/generate-sql', genBody);
    if (!sqlRes.success && sqlRes.code === 'QUERY_LIMIT_REACHED') throwApiError(sqlRes);
    if (!sqlRes.success) {
      // Most common first-run failure: no LLM provider key. Take the user straight to where it is set.
      if (sqlRes.error_code === 'NO_LLM_PROVIDER' && allowClientApiKey) { openSettings(); }
      const err = new Error(sqlRes.error);
      err.requestId = sqlRes.request_id;
      throw err;
    }
    refreshUsage();
    currentSql = sqlRes.sql || '';
    $('sqlOut').innerHTML = highlightSql(currentSql || '-- No SQL was generated.');

    const expl = sqlRes.explanation || '';
    if (expl) { $('sqlExplanation').textContent = expl; $('sqlExplanation').hidden = false; }
    else { $('sqlExplanation').hidden = true; }

    const conf = sqlRes.confidence || 'Medium';
    $('kpiConfidence').textContent = conf;

    $('sqlMetaTags').innerHTML = [
      `<span class="pill-tag tag-active">${escapeHtml(conf)} confidence</span>`,
      `<span class="pill-tag tag-neutral">${escapeHtml(sqlRes.complexity || 'Moderate')} complexity</span>`,
    ].join('');

    setBadge('badgeSql', conf + ' confidence', 'muted');
    stageDone('sql', sqlRes); await sleep(150);

    markStep('validation','active'); setPipelineState('Validating the SQL…');
    const validation = await post('/api/validate-sql', {session_id: sessionId, sql: currentSql});
    if (!validation.success) throwApiError(validation);
    renderValidationTab(validation);
    { const f = (validation.warnings || []).length + (validation.issues || []).length;
      setBadge('badgeValidation', validation.is_valid ? (f ? 'Passed with findings' : 'Passed · ' + (validation.validation_score ?? 0) + '%') : 'Rejected', validation.is_valid ? (f ? 'warn' : 'ok') : 'bad'); }
    stageDone('validation', validation);
    if (!validation.is_valid) {
      stageEnd('validation', undefined, 'error', 'Rejected: the SQL did not pass validation, so it was not run.');
      openFold('sec-validation', true);
      throw new Error('The generated SQL did not pass validation, so it was not run. See the safety checks below.');
    }
    const valScore = validation.validation_score ?? 0;
    $('kpiReliability').textContent = valScore + '%';
    await sleep(150);

    markStep('execution','active'); setPipelineState('Running the query…');
    const execution = await post('/api/execute-sql', {session_id: sessionId, sql: currentSql, question: q});
    if (!execution.success) throwApiError(execution);
    refreshAccountUsage();
    const rows = execution.rows || [];
    renderTable(rows);
    renderChart(rows);
    const chartShown = !$('chartArea').classList.contains('hidden-element');
    $('resultSeg').dataset.chart = chartShown ? '1' : '0';
    $('resultSeg').hidden = !chartShown;
    setResultView(chartShown ? 'chart' : 'table');

    const rowCount = execution.row_count ?? rows.length;
    const execMs = execution.execution_metadata?.execution_time_ms ?? 0;
    $('kpiRows').textContent = numFmt(rowCount);
    $('kpiExec').textContent = execMs + ' ms';
    renderCorrectness(execution);
    stageDone('execution', execution);
    // Bring the answer into view as soon as its rows are on screen, not after the report job: that finishes seconds later,
    // when the user may already be clicking Download CSV / JSON, and a smooth scroll then moves the buttons under the pointer.
    $('sec-answer').scrollIntoView({behavior: 'smooth', block: 'start'});
    await sleep(150);

    markStep('reliability','active'); setPipelineState('Checking data reliability…');
    const reliability = await post('/api/analyze-reliability', {session_id: sessionId});
    if (!reliability.success) throwApiError(reliability);
    const relScore = reliability.reliability_score ?? 0;
    const relLevel = reliability.reliability_level || (relScore >= 90 ? 'High' : relScore >= 75 ? 'Medium' : 'Low');

    $('relScore').textContent = relScore;
    $('relLevel').textContent = relLevel + ' reliability';
    $('relStatus').textContent = (reliability.status || 'Assessment complete') + (reliability.capped_reason ? ' · ' + reliability.capped_reason : (reliability.answer_verified ? ' · answer independently verified' : ''));

    const circumference = 175.9;
    $('relArc').style.strokeDashoffset = circumference - (relScore / 100 * circumference);
    renderReliabilityChecks(reliability);
    setBadge('badgeIntegrity', relScore + '/100 · ' + relLevel, relLevel === 'High' ? 'ok' : relLevel === 'Medium' ? 'warn' : 'bad');
    stageDone('reliability', reliability); await sleep(150);

    markStep('insights','active'); setPipelineState('Summarizing the answer…');
    const insights = await post('/api/generate-insights', {session_id: sessionId});
    if (!insights.success) throwApiError(insights);
    $('execNote').textContent = insights.executive_note || insights.summary || 'The query ran. See the result below.';
    renderSpreadInsights(insights);
    await loadTrustTrace();
    stageDone('insights', insights); await sleep(150);

    // The answer is already on screen at this point. A report failure (worker down, queue backlog) is reported on
    // the Answer node as a warning; it must not turn a finished answer into a "Stopped" run with a global error.
    markStep('report','active'); setPipelineState('Preparing the report…');
    try {
      const reportJob = await post('/api/generate-report', {session_id: sessionId});
      if (!reportJob.success) throwApiError(reportJob);
      const report = await pollJob(reportJob.job_id, 'Report generation');
      lastReport = report;
      stageDone('report', report); setPipelineState('Done');
    } catch (reportErr) {
      stageEnd('report', undefined, 'error', String((reportErr && reportErr.message) || reportErr).slice(0, 300));
      setPipelineState('Done · report unavailable');
    }

  } catch (e) {
    Object.keys(stageInfo).filter(k => stageInfo[k].status === 'running').forEach(k => stageEnd(k, undefined, 'error', String(e.message || e).slice(0, 300)));
    setPipelineState(e.quotaReached ? 'Daily limit reached' : 'Stopped');
    $('rawOut').textContent = 'Run stopped: ' + (e.message || e);
    if (!e.quotaReached) showGlobalError(e.message || String(e), e.requestId);
  } finally {
    setPipelineBusy(false);
  }
}

function renderTrustTrace(trace) {
  const root = $('trustTraceContent');
  if (!root) return;
  if (!trace || !trace.available) {
    root.innerHTML = '<div class="trust-trace-empty">Answer provenance is not available for this result.</div>';
    return;
  }

  const has = (key) => trace[key] !== undefined && trace[key] !== null;
  const grounding = trace.grounding || {};
  const validation = trace.validation || {};
  const execution = trace.execution || {};
  const reliability = trace.reliability || {};
  const answer = trace.answer;
  const tableNames = (grounding.tables || []).map(x => x.table).filter(Boolean);
  const validationText = validation.is_valid === true ? 'Validated' : validation.is_valid === false ? 'Validation failed' : 'Not available';
  const executionText = execution.success === true ? `${execution.rows_returned ?? 0} rows` : execution.status || 'Not available';
  const reliabilityText = reliability.reliability_level || (has('reliability_score') ? `${reliability.reliability_score}/100` : 'Available');
  const mono = (text) => `<div class="trust-trace-mono">${escapeHtml(text)}</div>`;
  const pill = (text, cls) => `<span class="pill-tag ${cls || 'tag-neutral'}">${escapeHtml(String(text))}</span>`;

  // state: ok = recorded and healthy, bad = recorded and failed, na = nothing recorded
  const steps = [];
  steps.push({ title: 'Question', state: has('question') ? 'ok' : 'na', summary: trace.question || 'Available', detail: '' });

  let groundDetail = '';
  const tableLines = (grounding.tables || []).map(x => `${x.table}${x.business_name ? ` — ${x.business_name}` : ''}${x.table_type ? ` · ${x.table_type}` : ''}`);
  const colLines = (grounding.columns || []).map(x => `${x.table}.${x.column}${x.role ? ` · ${x.role}` : ''}`);
  if (tableLines.length) groundDetail += `<div><div class="check-group-title">Retrieved tables</div>${mono(tableLines.join('\n'))}</div>`;
  if (colLines.length) groundDetail += `<div><div class="check-group-title">Relevant columns</div>${mono(colLines.join('\n'))}</div>`;
  if ((grounding.join_hints || []).length) groundDetail += `<div><div class="check-group-title">Join context</div>${mono(grounding.join_hints.join('\n'))}</div>`;
  if ((trace.semantic_context || []).length) {
    const defs = trace.semantic_context.map(t => `${t.table}${t.business_name ? ` — ${t.business_name}` : ''}\n${(t.columns || []).map(c => `  ${c.column}: ${c.meaning || 'definition unavailable'} · ${c.role || 'role unavailable'}`).join('\n')}`).join('\n\n');
    groundDetail += `<div><div class="check-group-title">Semantic definitions used</div>${mono(defs)}</div>`;
  }
  steps.push({ title: 'Grounding', state: tableNames.length || groundDetail ? 'ok' : 'na', summary: tableNames.length ? tableNames.join(', ') : 'Schema context', detail: groundDetail });

  steps.push({ title: 'SQL generated', state: has('sql') ? 'ok' : 'na', summary: has('sql') ? 'Generated from the grounded context' : 'Not available', detail: has('sql') ? mono(trace.sql) : '' });

  let valDetail = '';
  if (has('validation')) {
    valDetail = `<div class="trust-trace-meta">
      ${validation.validation_score != null ? pill(`Score ${validation.validation_score}/100`) : ''}
      ${validation.join_valid != null ? pill(`Joins ${validation.join_valid ? 'valid' : 'flagged'}`, validation.join_valid ? 'tag-pass' : 'tag-alert') : ''}
      ${validation.aggregation_valid != null ? pill(`Aggregation ${validation.aggregation_valid ? 'valid' : 'flagged'}`, validation.aggregation_valid ? 'tag-pass' : 'tag-alert') : ''}
    </div>`;
  }
  steps.push({ title: 'Validation', state: validation.is_valid === true ? 'ok' : validation.is_valid === false ? 'bad' : 'na', summary: validationText, detail: valDetail });

  let execDetail = '';
  if (has('execution')) {
    execDetail = `<div class="trust-trace-meta">
      ${execution.execution_time_ms != null ? pill(`${execution.execution_time_ms} ms`) : ''}
      ${execution.rows_returned != null ? pill(`${execution.rows_returned} rows returned`) : ''}
      ${execution.success != null ? pill(execution.success ? 'Executed' : 'Not executed', execution.success ? 'tag-pass' : 'tag-alert') : ''}
    </div>`;
  }
  steps.push({ title: 'Execution', state: execution.success === true ? 'ok' : execution.success === false ? 'bad' : 'na', summary: executionText, detail: execDetail });

  let relDetail = '';
  if (has('reliability')) {
    relDetail = `<div class="trust-trace-meta">${Object.entries(reliability).map(([k, v]) => pill(`${k.replaceAll('_', ' ')}: ${v}`)).join('')}</div>`;
  }
  steps.push({ title: 'Reliability', state: has('reliability') ? 'ok' : 'na', summary: String(reliabilityText), detail: relDetail });

  steps.push({ title: 'Answer', state: answer ? 'ok' : 'na', summary: answer ? 'Produced' : 'Available in the results', detail: answer ? `<div class="trace-answer">${escapeHtml(String(answer))}</div>` : '' });

  const items = steps.map((s, i) => `<li class="trace-step ${s.state}">
      <div class="trace-marker">${i + 1}</div>
      <div class="trace-main">
        <div class="trace-line"><strong>${escapeHtml(s.title)}</strong><span class="trace-summary">${escapeHtml(String(s.summary))}</span></div>
        ${s.detail ? `<details><summary>Show details</summary><div class="trace-detail">${s.detail}</div></details>` : ''}
      </div>
    </li>`).join('');
  root.innerHTML = `<ol class="trace-ledger">${items}</ol>
    <div class="trust-trace-disclaimer">This trace shows the evidence and processing steps QuerySure recorded for this answer. Validation and execution are safeguards; they do not guarantee that an answer is correct.</div>`;
}

async function loadTrustTrace() {
  if (!sessionId) return;
  try {
    const res = await fetch(`/api/answer-trace?session_id=${encodeURIComponent(sessionId)}`, { headers: authHeaders() });
    if (!res.ok) return;
    const data = await res.json();
    if (data.success) {
      lastTrace = data.trace || null; renderTrustTrace(data.trace);
      const ok = !!(lastTrace && lastTrace.available); setBadge('badgeTrace', ok ? 'Recorded' : 'Not available', ok ? 'ok' : 'muted'); refreshStages();
    }
  } catch (_) {
    // Provenance is optional presentation; never break the core query result.
  }
}

function setBadge(id, text, tone) {
  const el = $(id); if (!el) return;
  el.textContent = text || '';
  el.className = 'fold-badge' + (tone ? ' tone-' + tone : '');
}
function openFold(id, scroll) {
  const el = $(id); if (!el) return;
  if (el.tagName === 'DETAILS') el.open = true;
  if (scroll) el.scrollIntoView({behavior: 'smooth', block: 'start'});
}
function resetDetailFolds() {
  document.querySelectorAll('.detail-fold').forEach(d => { d.open = false; });
  ['badgeSql', 'badgeExplain', 'badgeValidation', 'badgeCorrect', 'badgeIntegrity', 'badgeAnomalies', 'badgeTrace'].forEach(id => setBadge(id, ''));
  if ($('correctnessContent')) $('correctnessContent').innerHTML = '<p class="muted">Appears after the query has run.</p>';
}
function renderCorrectness(ex) {
  const root = $('correctnessContent'); if (!root) return;
  const dims = ex && ex.correctness, ver = ex && ex.verification;
  if (!dims && !ver) { root.innerHTML = '<p class="muted">The backend did not return correctness information for this run.</p>'; setBadge('badgeCorrect', 'Not available', 'muted'); return; }
  const label = {syntax: 'Syntax and schema', security: 'Read-only safety', semantic_validation: 'Matches the question plan', execution: 'Execution', result_correctness: 'Result correctness'};
  const word = {pass: 'Pass', fail: 'Fail', needs_attention: 'Needs attention', not_run: 'Not run', verified: 'Verified', mismatch: 'Mismatch', suspicious: 'Suspicious', unconfirmed: 'Unconfirmed', not_verified: 'Not verified', unknown: 'Unknown'};
  const tone = st => (st === 'pass' || st === 'verified' ? 'tag-pass' : st === 'fail' || st === 'mismatch' ? 'tag-alert' : st === 'not_run' || st === 'unknown' ? 'tag-neutral' : 'tag-warn');
  let html = '';
  if (dims) html += '<ul class="check-rows">' + Object.entries(dims).map(([k, v]) => `<li><span class="pill-tag ${tone(v.status)}">${escapeHtml(word[v.status] || String(v.status))}</span><div><strong>${escapeHtml(label[k] || k)}</strong><p>${escapeHtml(String(v.detail || ''))}</p></div></li>`).join('') + '</ul>';
  if (ver) {
    html += '<div class="panel-title">Independent re-computation</div>' +
      `<p class="verify-line">${escapeHtml(VERIFY_WORD[ver.status] || String(ver.status))}${ver.diagnosis ? ' · ' + escapeHtml(String(ver.diagnosis)) : ''}</p>` +
      (ver.checks || []).map(c => `<p class="muted">${escapeHtml(String(c.check))}: ${escapeHtml(String(c.result))}${c.detail ? ' — ' + escapeHtml(String(c.detail)) : ''}</p>`).join('');
    if (ver.reference_sql) html += `<div class="code-card"><pre>${highlightSql(String(ver.reference_sql))}</pre></div>`;
  }
  html += '<p class="trust-trace-disclaimer">Passing these checks does not guarantee the answer is correct.</p>';
  root.innerHTML = html;
  const vs = ver && ver.status;
  setBadge('badgeCorrect', VERIFY_WORD[vs] || (vs ? String(vs) : 'Not independently verified'), vs === 'verified_match' ? 'ok' : (vs === 'mismatch' || vs === 'error') ? 'bad' : 'warn');
}

function renderSpreadInsights(insights) {
  { const n = (insights.anomalies || []).length; setBadge('badgeAnomalies', n ? n + ' flagged' : 'None flagged', n ? 'warn' : 'ok'); }
  renderBulletList('findingsList', insights.key_findings || [], 'finding', '•');
  renderBulletList('anomaliesList', insights.anomalies || [], 'anomaly', '!');
  renderBulletList('recoList', insights.recommendations || [], 'reco', '→');
}

// System Trigger Hooks Bindings Mapping Matrix
$('sampleBtn').onclick = connectSample;
$('emptySampleBtn').onclick = connectSample;
$('dsSampleBtn').onclick = connectSample;
$('fileInput').onchange = e => e.target.files[0] && uploadFile(e.target.files[0]);
$('newSessionBtn').onclick = newSession;
$('analyzeBtn').onclick = () => runAll();
$('disconnectBtn').onclick = () => location.reload();
$('downloadCsvBtn').onclick = () => download('querysure-result.csv', csvFromRows(lastRows), 'text/csv');
$('downloadJsonBtn').onclick = () => download('querysure-result.json', JSON.stringify(lastRows, null, 2), 'application/json');
// Business contact is intentionally configuration-driven; no personal address is invented in the client.
(async function initProductContact() {
  const businessEl = $('businessContactText');
  const footerEl = $('footerContactLink');
  if (!businessEl && !footerEl) return;
  try {
    const res = await fetch('/api/product-info');
    if (!res.ok) return;
    const data = await res.json();
    applyBillingAvailability(data);
    if (data && data.contact_email) {
      const email = escapeHtml(data.contact_email);
      if (businessEl) businessEl.innerHTML = `\u003ca href=\"mailto:${email}\">${email}\u003c/a>`;
      if (footerEl) {
        footerEl.outerHTML = `\u003ca id=\"footerContactLink\" href=\"mailto:${email}\">Contact\u003c/a>`;
      }
    } else if (businessEl) {
      businessEl.textContent = 'Contact email not configured (PRODUCT_CONTACT_EMAIL).';
    }
  } catch (_) {}
})();

(function wireAccountControls() {
  const on = (id, fn) => { const el = $(id); if (el) el.onclick = fn; };
  on('settingsUpgradeBtn', startCheckout);
  on('settingsExportBtn', exportAccountData);
  on('settingsSignOutAllBtn', signOutEverywhere);
  on('settingsDeleteBtn', showDeleteForm);
  on('deleteAccountConfirmBtn', deleteAccount);
  on('deleteAccountCancelBtn', () => { $('deleteAccountForm').hidden = true; setDeleteFieldsEligible(false); setAccountMessage(''); });
})();

// Platform Environment Lifecycle Ignition Boot sequence.
// FIX: this only ran once at initial script parse and, on a saved/expired
// token, silently did nothing -- leaving the landing screen showing with no
// feedback while a stale token sat in localStorage. It now shares the same
// path as post-login boot (bootApp) once the token is confirmed valid, and
// explicitly falls back to the landing screen (not a stuck blank state) if
// the token is missing or invalid.
(async function boot() {
  if (!authToken) { bootSiteRoute(); return; }
  const me = await apiGet('/api/auth/me');
  if (!me || me.success === false || !me.email) { switchScreen('screen-landing'); return; }
  setCurrentUser(me, await apiGet('/api/auth/methods'));
  refreshSettingsView();
  resetWorkspaceUI();
  hideAuthGate();
  await newSession();
  refreshUsage();
  refreshAccountUsage();
  refreshBillingState();
  refreshProviderStatus();
  // A signed-in reload at "/" must stay in the workspace; only an explicit site page (/pricing, /security, ...) replaces it.
  const route = location.pathname.replace(/\/+$/, '') || '/';
  if (SITE_ROUTES.has(route)) bootSiteRoute();
  handleCheckoutReturn();
})();
// Product evaluation summary: presentation-only, never computes or invents metrics.
(function initEvaluationSection() {
  const $e = (id) => document.getElementById(id);
  const pct = (v) => typeof v === 'number' ? `${(v * 100).toFixed(1)}%` : '—';
  const setText = (id, value) => { const el = $e(id); if (el) el.textContent = value; };

  async function loadEvaluationSummary() {
    try {
      const data = await fetchEvaluationSummary();
      if (!data) return;
      const b = data.benchmark || {};
      setText('evalCoverage', b.available ? `${b.coverage ?? b.cases} cases` : '—');
      setText('evalCategories', b.categories?.length ? b.categories.join(' · ') : 'Category coverage available');

      const hasMetrics = data.evaluated_cases > 0 && Object.values(data.metrics || {}).some(v => typeof v === 'number');
      setText('evalDeterministicStatus', hasMetrics ? `${data.evaluated_cases} cases evaluated` : 'Results not run');
      setText('evalDeterministicNote', hasMetrics ? 'Real benchmark measurements' : 'Coverage and methodology available');

      const de = data.deepeval || {};
      setText('evalDeepEvalStatus', de.status === 'available' ? `${de.cases} cases evaluated` : 'Not run');
      setText('evalDeepEvalNote', de.status === 'available' ? 'Supplementary judge metrics available' : 'Supplementary relevance / faithfulness');
      const syn = data.synthetic || {};
      setText('evalSyntheticStatus', syn.status === 'available' ? `${syn.cases} cases` : 'Not run');
      setText('evalSyntheticNote', syn.status === 'available' ? 'Generated evaluation coverage' : 'Additional coverage when generated');

      const metrics = $e('evaluationMetrics');
      if (metrics && hasMetrics) {
        const labels = [
          ['Result Accuracy', data.metrics.result_accuracy],
          ['SQL Execution Success', data.metrics.sql_execution_success],
          ['Schema Grounding', data.metrics.schema_grounding],
          ['Hallucinated Identifiers', data.metrics.hallucinated_identifiers],
          ['Correct Rejection', data.metrics.correct_rejection]
        ];
        metrics.innerHTML = labels.filter(([,v]) => typeof v === 'number').map(([label,v]) => `<div class="evaluation-metric"><span>${label}</span><strong>${pct(v)}</strong></div>`).join('');
        metrics.hidden = !metrics.innerHTML;
      }
    } catch (_) {
      // Product landing remains useful when the evaluation endpoint is unavailable.
    }
  }

  const modal = $e('evaluationMethodModal');
  const open = () => { if (modal) modal.hidden = false; };
  const close = () => { if (modal) modal.hidden = true; };
  $e('evaluationMethodBtn')?.addEventListener('click', open);
  $e('evaluationModalClose')?.addEventListener('click', close);
  $e('evaluationModalBackdrop')?.addEventListener('click', close);
  loadEvaluationSummary();
})();

handleAuthLink();

// ---- Query history ----------------------------------------------------------------------------------------------
let historyState = { q: '', offset: 0, total: 0, selected: null, items: [], timer: null };

function fmtWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'});
}

async function loadHistory(append) {
  const list = $('historyList');
  if (!list) return;
  if (!append) { historyState.offset = 0; historyState.items = []; }
  const qs = new URLSearchParams({limit: '30', offset: String(historyState.offset)});
  if (historyState.q) qs.set('q', historyState.q);
  const res = await apiGet('/api/history?' + qs.toString());
  if (!res || !res.success) { list.innerHTML = '<li class="muted">Could not load history.</li>'; return; }
  historyState.total = res.total || 0;
  historyState.items = historyState.items.concat(res.items || []);
  historyState.offset = historyState.items.length;
  renderHistoryList();
}

function renderHistoryList() {
  const list = $('historyList');
  const items = historyState.items;
  if (!items.length) {
    list.innerHTML = '<li class="muted">' + (historyState.q ? 'No history matches your search.' : 'No history yet. Ask a question and it will appear here.') + '</li>';
  } else {
    list.innerHTML = items.map(it => {
      const ok = it.status === 'success';
      const tag = ok ? 'tag-pass' : (it.status === 'empty_result' ? 'tag-neutral' : 'tag-alert');
      return `<li><button type="button" class="history-item" data-id="${escapeHtml(it.id)}" aria-current="${historyState.selected === it.id}">
        <span class="hq">${escapeHtml(it.question || '(SQL run without a recorded question)')}</span>
        <span class="hm"><span class="pill-tag ${tag}">${escapeHtml(it.status.replace('_', ' '))}</span><span>${escapeHtml(fmtWhen(it.created_at))}</span><span>${it.row_count ?? 0} rows</span></span></button></li>`;
    }).join('');
    list.querySelectorAll('.history-item').forEach(b => { b.onclick = () => openHistoryEntry(b.dataset.id); });
  }
  $('historyMore').hidden = historyState.items.length >= historyState.total;
}

async function openHistoryEntry(id) {
  historyState.selected = id;
  renderHistoryList();
  const pane = $('historyDetail');
  pane.innerHTML = '<p class="muted">Loading…</p>';
  const res = await apiGet('/api/history/' + encodeURIComponent(id));
  if (!res || !res.success) { pane.innerHTML = '<p class="muted">This entry is no longer available.</p>'; return; }
  const r = res.result;
  let table = '<p class="muted" id="histNoResult">Result not saved</p>';
  if (r && r.columns && r.columns.length) {
    const head = r.columns.map(c => `<th>${escapeHtml(c)}</th>`).join('');
    const body = (r.rows || []).map(row => '<tr>' + r.columns.map(c => `<td>${escapeHtml(row[c] === null || row[c] === undefined ? '' : String(row[c]))}</td>`).join('') + '</tr>').join('');
    table = `<div class="table-wrapper"><table class="data-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>` +
      (r.truncated ? `<p class="muted">Showing the first ${(r.rows || []).length} of ${r.total_rows} saved rows.</p>` : '');
  }
  const meta = res.answer_meta || {};
  const verif = meta.verification_status ? `<span class="pill-tag ${meta.verified ? 'tag-pass' : 'tag-neutral'}">${escapeHtml(String(meta.verification_status).replace(/_/g, ' '))}</span>` : '';
  pane.innerHTML = `
    <div class="sheet-label">Question</div>
    <h3 id="histQ"></h3>
    <div class="history-banner">Saved result from ${escapeHtml(fmtWhen(res.created_at))} on ${escapeHtml((res.dataset && res.dataset.label) || 'an earlier dataset')}. This is what was returned then; it has <strong>not</strong> been re-run. ${res.sql_edited ? 'The SQL was edited before it ran.' : ''}</div>
    <div class="history-actions">
      <button class="btn-quiet" id="histDelete" type="button">Delete</button>
      <button class="btn-primary" id="histDeleteConfirm" type="button" hidden>Confirm delete</button>
      <button class="btn-secondary" id="histDeleteCancel" type="button" hidden>Cancel</button>
    </div>
    <div class="sheet-label">SQL</div><pre id="histSql"></pre>
    <div class="sheet-label">Saved result ${verif}</div>${table}`;
  $('histQ').textContent = res.question || '(no question recorded)';
  $('histSql').textContent = res.sql || '';
  // Deleting is two-step: Delete asks, Confirm delete removes. Nothing is removed until the server confirms it.
  const askDelete = (on) => {
    $('histDelete').hidden = on; $('histDeleteConfirm').hidden = !on; $('histDeleteCancel').hidden = !on;
    if (on) $('histDeleteConfirm').focus();
  };
  $('histDelete').onclick = () => askDelete(true);
  $('histDeleteCancel').onclick = () => askDelete(false);
  $('histDeleteConfirm').onclick = async () => {
    $('histDeleteConfirm').disabled = true;
    const d = await _request('/api/history/' + encodeURIComponent(id), {method: 'DELETE', headers: authHeaders()});
    if (!d || d.success === false) {
      $('histDeleteConfirm').disabled = false; askDelete(false);
      showGlobalError((d && d.error) || 'Could not delete this entry. Please try again.', d && d.request_id);
      return;
    }
    historyState.selected = null;
    pane.innerHTML = '<p class="muted">Entry deleted.</p>';
    loadHistory(false);
  };
}

(function wireHistory() {
  const s = $('historySearch');
  if (s) s.addEventListener('input', () => {
    clearTimeout(historyState.timer);
    historyState.timer = setTimeout(() => { historyState.q = s.value.trim(); loadHistory(false); }, 250);
  });
  if ($('historyRefresh')) $('historyRefresh').onclick = () => loadHistory(false);
  if ($('historyMore')) $('historyMore').onclick = () => loadHistory(true);
})();

wireStages();
syncAuthFieldEligibility();
setDeleteFieldsEligible(false);
