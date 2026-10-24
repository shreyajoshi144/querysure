'use strict';
// Read-only ops dashboard. All values are written with textContent (never an HTML-injection API), and the server only ever sends
// aggregate numbers, component names and allow-listed incident fields.
const grid = document.getElementById('grid');
const statusEl = document.getElementById('status');
const errEl = document.getElementById('error');
const token = localStorage.getItem('qs_token') || '';

function el(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined && text !== null) e.textContent = String(text); if (cls) e.className = cls; return e; }
function fmt(v, unit) { return v === null || v === undefined ? '-' : (unit ? v + unit : String(v)); }
function table(headers, rows) {
  const t = el('table'); const hr = el('tr');
  headers.forEach((h, i) => hr.appendChild(el('th', h, i ? 'n' : '')));
  t.appendChild(hr);
  rows.forEach(r => { const tr = el('tr'); r.forEach((c, i) => { const td = el('td', c && c.text !== undefined ? c.text : c, (i ? 'n ' : '') + (c && c.cls ? c.cls : '')); tr.appendChild(td); }); t.appendChild(tr); });
  return t;
}
function card(title, body) { const s = el('section'); s.appendChild(el('h2', title)); s.appendChild(body); return s; }
function kv(obj) { return table(['metric', 'value'], Object.entries(obj).map(([k, v]) => [k, fmt(v)])); }
function lat(l) { return `${fmt(l.p50_ms)} / ${fmt(l.p95_ms)} / ${fmt(l.p99_ms)}`; }

function render(d) {
  grid.replaceChildren();
  statusEl.textContent = d.status; statusEl.className = 'pill ' + (d.status === 'ok' ? 'ok' : 'degraded');
  document.getElementById('updated').textContent = 'updated ' + new Date().toLocaleTimeString() + (d.degraded.length ? ' | degraded: ' + d.degraded.join(', ') : '');
  const t = d.traffic;
  grid.appendChild(card('Traffic', kv({ requests: t.requests, 'req/s': t.rps, '5xx rate %': t.error_rate_5xx_pct, 'p50/p95/p99 ms': lat(t.latency), 'rate limited': t.rate_limited, 'db busy (503)': t.db_busy })));
  grid.appendChild(card('SLOs', table(['objective', 'sli', 'target', 'status'], d.slo.objectives.map(o => [o.name, fmt(o.sli, o.unit), fmt(o.target, o.unit), { text: o.status, cls: o.status }]))));
  grid.appendChild(card('Routes (top by volume)', table(['route', 'reqs', '5xx', 'p50/p95/p99'], t.routes.map(r => [r.route, r.requests, r.errors_5xx, lat(r)]))));
  const a = d.ai;
  grid.appendChild(card('AI', kv({ requests: a.requests, 'success %': a.success_rate_pct, 'p50/p95/p99 ms': lat(a.latency), fallbacks: a.fallbacks, 'deadline exceeded': a.deadline_exceeded, 'tokens in': a.tokens.input, 'tokens out': a.tokens.output, 'cost USD': a.cost_usd, 'kill switch': a.kill_switch, 'global budget used %': (a.global_budget || {}).spent_pct })));
  grid.appendChild(card('AI providers', table(['provider', 'ok', 'fail', 'p95 ms', 'cost $', 'circuit'], Object.entries(a.providers).map(([p, v]) => [p, v.success, v.failure, fmt(v.latency.p95_ms), v.cost_usd, a.circuit_breakers[p] || '-']))));
  const q = d.jobs.queue;
  grid.appendChild(card('Queue & workers', kv({ available: q.available, depth: q.depth, 'oldest wait s': q.oldest_age_s, 'workers alive': q.workers_alive, 'workers busy': q.workers_busy, failed: q.failed, 'wait p50/p95/p99 ms': lat(d.jobs.queue_wait), 'duration p50/p95/p99 ms': lat(d.jobs.duration) })));
  const i = d.infrastructure;
  grid.appendChild(card('Database', kv({ ok: i.database.ok, 'latency ms': i.database.latency_ms, 'pool max util %': i.database.pool.max_utilization_pct, 'pool checked out': i.database.pool.checked_out_total, 'pool capacity': i.database.pool.capacity_total })));
  grid.appendChild(card('Redis', kv({ reachable: i.redis.reachable, 'ping ms': i.redis.ping_ms, clients: i.redis.connected_clients, 'memory MB': i.redis.used_memory_mb, 'ops/s': i.redis.ops_per_sec, evicted: i.redis.evicted_keys, rejected: i.redis.rejected_connections })));
  grid.appendChild(card('Open incidents', table(['alert', 'severity', 'opened', 'seen x'], d.open_incidents.map(x => [x.alert, x.severity, x.opened_at, x.occurrences]))));
  grid.appendChild(card('Top error fingerprints', table(['type', 'location', 'count'], d.errors.map(x => [x.type, x.location, x.count]))));
}

function renderProviders(p) {
  grid.appendChild(card('LLM fallback chain', table(['#', 'provider', 'model', 'circuit'], p.chain.map(c => [c.position, c.provider, c.model, p.circuit_breakers[c.provider] || '-']))));
  grid.appendChild(card('LLM failure reasons', table(['provider', 'reason', 'count'], Object.entries(p.metrics).flatMap(([n, v]) => Object.entries(v.failure_reasons).map(([r, c]) => [n, r, c])))));
  grid.appendChild(card('Fallback history', table(['when', 'event', 'used', 'first error', 'failed calls'], p.fallback_history.map(e => [e.created_at || e.at, e.kind, (e.detail || {}).provider || '-', (e.detail || {}).reason || '-', (e.detail || {}).count]))));
  grid.appendChild(card('LLM settings', kv(p.settings)));
}

async function load() {
  errEl.hidden = true;
  if (!token) { errEl.textContent = 'Sign in to QuerySure first, then reload this page.'; errEl.hidden = false; return; }
  try {
    const w = document.getElementById('window').value;
    const r = await fetch('/api/admin/ops/summary?window_minutes=' + encodeURIComponent(w), { headers: { Authorization: 'Bearer ' + token } });
    if (r.status === 403) throw new Error('This account is not an ops administrator.');
    if (!r.ok) throw new Error('Request failed (' + r.status + ').');
    render(await r.json());
    const pr = await fetch('/api/admin/ops/providers?window_minutes=' + encodeURIComponent(w), { headers: { Authorization: 'Bearer ' + token } });
    if (pr.ok) renderProviders(await pr.json());
  } catch (e) { errEl.textContent = e.message; errEl.hidden = false; statusEl.textContent = 'error'; statusEl.className = 'pill bad'; }
}
document.getElementById('window').addEventListener('change', load);
load(); setInterval(load, 10000);
