"use strict";
// ═══════════════════════════════════════════════════════════════════════════
// DATA-DRIVEN: every FACT on this map — verbs, objects, derivations, callers,
// doors, write/read/cascade edges — is fetched from /api/ontology at load
// (ontology.manifest(), computed from the source on every call), so the map
// cannot drift from the code. What remains below are PRESENTATION HINTS only:
// visual grouping, preferred ordering, display labels, and line styling. A
// name the hints don't know still renders (fallback group / appended order) —
// hints affect where a node sits, never whether it exists.
// Types: call | write | phase | cascade | audit | read | serve
// ───────────────────────────────────────────────────────────────────────────

// Visual grouping of the object column. A governed table missing here lands in
// the 'Other' fallback group — grouping is presentation, membership is data.
const OBJ_GROUPS = [
  { g: 'People & shares',  items: ['members', 'splits'] },
  { g: 'Money movement',   items: ['transactions', 'links', 'income_rules', 'budgets'] },
  { g: 'Commitments',      items: ['bills', 'bill_payments', 'goals', 'goal_contributions'] },
  { g: 'Household',        items: ['items'] },
  { g: 'System of record', items: ['audit_log', 'api_tokens', 'pending_actions'] }
];

// Display labels + column order for the manifest's caller / door ids. An id
// the maps don't know renders under its raw id, appended after the known ones.
const CALLER_META = { ui: 'UI route', sync: 'SimpleFIN sync', mcp: 'MCP write tier',
                      ask: 'Ask · chat', cli: 'CLI · maintenance' };
const CALLER_ORDER = ['ui', 'sync', 'mcp', 'ask', 'cli'];
const DOOR_META = { api: 'Flask JSON API', mcp: 'MCP read tier', ask: 'Ask · chat' };
const DOOR_ORDER = ['api', 'mcp', 'ask'];

// The two-phase choreography verbs: their pending_actions writes draw with the
// double-line 'phase' grammar (styling; other verbs touching pending_actions —
// reset_money's wipe — stay plain writes).
const PHASE_VERBS = new Set(['propose_action', 'confirm_action']);

// The one edge SQLite enforces rather than code: goals' ON DELETE CASCADE
// wipes goal_contributions. Invisible to the manifest's source scan, so it is
// the single hand-declared edge on the map.
const FK_CASCADE = { delete_goal: ['goal_contributions'] };

// Write-only sinks get the accent border (styling; the tables themselves come
// from the manifest like everything else).
const SINKS = new Set(['o:audit_log', 'o:api_tokens', 'o:pending_actions']);

// Preferred display order (semantic grouping reads better than alphabetical).
// Names missing from a hint append alphabetically — a new verb/derivation
// appears without touching this file.
const VERB_ORDER = [
  'record_transaction', 'edit_transaction', 'delete_transaction', 'settle_up',
  'classify_inflow', 'create_bill', 'update_bill', 'delete_bill',
  'mark_bill_paid', 'unmark_bill_paid', 'create_goal', 'delete_goal',
  'contribute_to_goal', 'withdraw_from_goal', 'create_income_rule',
  'set_rule_enabled', 'apply_rules', 'propose_action', 'confirm_action',
  'add_item', 'set_item_status', 'archive_item', 'set_item_match',
  'set_item_interval', 'set_budget', 'remove_budget', 'create_api_token',
  'revoke_api_token', 'reset_money'
];
const DERIV_ORDER = [
  'compute_balance', 'spending_summary', 'top_merchants', 'category_trend',
  'income_summary', 'income_trend', 'savings_rate_trend', 'member_breakdown',
  'bill_variance', 'budget_status', 'recurring_charges', 'cash_flow_forecast',
  'anomaly_flags', 'goal_pace', 'last_shopping_trip', 'shopping_list',
  'low_stock', 'restock_suggestions', 'restock_forecast', 'staple_spend',
  'unmatched_staples', 'stale_shopping_items', 'new_staple_suggestions'
];

// ── edge styles (line grammar) ──
const INK = '#201e1d', ACC = '#ec3013', BG = '#f3f2f2';
const STYLE = {
  call:    { stroke: INK, w: 1.2, dash: '1 4',     cap: 'round', marker: 'url(#mInk)',  z: 1 },
  audit:   { stroke: INK, w: 0.7, dash: '1 3',     cap: 'butt',  marker: 'none',        z: 0 },
  serve:   { stroke: INK, w: 1.4, dash: '9 3 2 3', cap: 'butt',  marker: 'url(#mBar)',  z: 2 },
  read:    { stroke: INK, w: 1.4, dash: '7 4',     cap: 'butt',  marker: 'url(#mInk)',  z: 3 },
  cascade: { stroke: ACC, w: 1.2, dash: '4 4',     cap: 'butt',  marker: 'url(#mAccO)', z: 4 },
  write:   { stroke: ACC, w: 1.7, dash: 'none',    cap: 'butt',  marker: 'url(#mAcc)',  z: 5 },
  phase:   { stroke: ACC, w: 4.2, dash: 'none',    cap: 'butt',  marker: 'url(#mAcc)',  z: 6 },
  phaseIn: { stroke: BG,  w: 1.6, dash: 'none',    cap: 'butt',  marker: 'none',        z: 7 }
};

// ═══════════════════════════════════════════════════════════════════════════
// Model — built from the fetched manifest
// ═══════════════════════════════════════════════════════════════════════════
let MODEL = null;      // set by boot(); render() no-ops until it exists
let EDGES = [];
let FWD = {}, BACK = {};
let NODE_COUNT = 0;

// hint order first (filtered to what exists), then anything new, alphabetical
function hintOrder(hint, names) {
  const have = new Set(names);
  const known = hint.filter(n => have.has(n));
  const rest = names.filter(n => !hint.includes(n)).sort();
  return known.concat(rest);
}

function buildModel(m) {
  const verbNames = m.actions.map(a => a.name);
  const byName = {};
  m.actions.forEach(a => { byName[a.name] = a; });
  const readsBy = {};
  m.functions.forEach(f => { readsBy[f.name] = f.reads || []; });

  const tables = m.objects.map(o => o.name);
  const known = new Set(OBJ_GROUPS.flatMap(g => g.items));
  const groups = OBJ_GROUPS
    .map(g => ({ g: g.g, items: g.items.filter(t => tables.includes(t)) }))
    .concat([{ g: 'Other', items: tables.filter(t => !known.has(t)).sort() }])
    .filter(g => g.items.length);

  const callerIds = hintOrder(CALLER_ORDER, Object.keys(m.callers));
  const callers = callerIds.map(k => ({
    key: k, id: CALLER_META[k] || k, calls: m.callers[k] || [] }));

  const derivs = hintOrder(DERIV_ORDER, m.functions.map(f => f.name));
  const doors = hintOrder(DOOR_ORDER, m.doors).map(k => ({
    key: k, id: DOOR_META[k] || k, serves: derivs.slice() }));

  return {
    verbs: hintOrder(VERB_ORDER, verbNames),
    byName, readsBy, groups, callers, derivs, doors,
    tables, schema: m.schema_version,
  };
}

function buildEdges() {
  const E = [];
  MODEL.callers.forEach(c => c.calls.forEach(v =>
    E.push({ t: 'call', a: 'c:' + c.id, b: 'v:' + v })));
  MODEL.verbs.forEach(v => {
    const a = MODEL.byName[v];
    a.writes_direct.forEach(o => {
      if (o === 'audit_log') return;   // drawn as its own 'audit' grammar below
      const isPhase = PHASE_VERBS.has(v) && o === 'pending_actions';
      E.push({ t: isPhase ? 'phase' : 'write', a: 'v:' + v, b: 'o:' + o });
    });
    (FK_CASCADE[v] || []).forEach(o => {
      if (!a.writes_direct.includes(o)) E.push({ t: 'write', a: 'v:' + v, b: 'o:' + o });
    });
    if (a.writes_direct.includes('audit_log'))
      E.push({ t: 'audit', a: 'v:' + v, b: 'o:audit_log' });
    (a.cascades || []).forEach(v2 =>
      E.push({ t: 'cascade', a: 'v:' + v, b: 'v:' + v2, loop: true }));
  });
  MODEL.derivs.forEach(d => (MODEL.readsBy[d] || []).forEach(o =>
    E.push({ t: 'read', a: 'o:' + o, b: 'd:' + d })));
  MODEL.doors.forEach(dr => dr.serves.forEach(d =>
    E.push({ t: 'serve', a: 'd:' + d, b: 's:' + dr.id })));
  return E;
}

function reach(start) {
  const nodes = new Set([start]), eIdx = new Set();
  const walk = (id, map, key) => {
    (map[id] || []).forEach(e => {
      if (eIdx.has(e._i)) return;
      eIdx.add(e._i);
      const nxt = e[key];
      if (!nodes.has(nxt)) { nodes.add(nxt); walk(nxt, map, key); }
    });
  };
  walk(start, FWD, 'b');
  walk(start, BACK, 'a');
  return { nodes, eIdx };
}

const KIND = { c: 'Caller', v: 'Write verb', o: 'Object', d: 'Derivation', s: 'Door' };
const label = id => id.slice(2);
const SVGNS = 'http://www.w3.org/2000/svg';

// ═══════════════════════════════════════════════════════════════════════════
// State + DOM
// ═══════════════════════════════════════════════════════════════════════════
let sel = null;
const muted = {};
const nodeEls = {};   // id → element

const $ = id => document.getElementById(id);
const canvas = $('canvas');
const edgesG = $('edges');

// ── build node columns once ──
function nodeEl(id, lbl) {
  const el = document.createElement('div');
  el.className = 'node';
  el.dataset.nid = id;
  el.textContent = lbl;
  el.addEventListener('click', ev => { ev.stopPropagation(); pick(id); });
  nodeEls[id] = el;
  return el;
}

function buildColumns() {
  MODEL.callers.forEach(c => $('col-callers').appendChild(nodeEl('c:' + c.id, c.id)));
  MODEL.verbs.forEach(v => $('col-verbs').appendChild(nodeEl('v:' + v, v)));
  MODEL.groups.forEach(grp => {
    const h = document.createElement('div');
    h.className = 'grp-head';
    h.textContent = grp.g;
    $('col-objects').appendChild(h);
    grp.items.forEach(o => $('col-objects').appendChild(nodeEl('o:' + o, o)));
  });
  MODEL.derivs.forEach(d => $('col-derivs').appendChild(nodeEl('d:' + d, d)));
  MODEL.doors.forEach(dr => $('col-doors').appendChild(nodeEl('s:' + dr.id, dr.id)));
}

// ── node styling per render ──
function styleNode(id, active) {
  const el = nodeEls[id];
  const kind = id[0];
  const on = !active || active.nodes.has(id);
  const isSel = sel === id;
  let bg = 'transparent', col = INK, bd = '1px solid #cfcbc9', fw = 500;
  if (kind === 'c' || kind === 's') { bd = '2px solid ' + INK; fw = 700; }        // caller / door endpoints
  else if (kind === 'v') { bd = '1px solid #f0b3a7'; }                             // verbs
  else if (kind === 'o' && SINKS.has(id)) { bd = '1px solid ' + ACC; col = ACC; } // write-only sinks
  if (isSel) { bg = ACC; col = '#fff'; bd = '2px solid ' + ACC; fw = 700; }
  else if (on && active) { bg = '#fbe4df'; bd = '1px solid ' + ACC; }
  el.style.background = bg;
  el.style.color = col;
  el.style.border = bd;
  el.style.fontWeight = fw;
  el.style.opacity = on ? 1 : 0.22;
}

// ── geometry: measure node rects relative to the canvas ──
function measure() {
  const cb = canvas.getBoundingClientRect();
  const geo = {};
  Object.entries(nodeEls).forEach(([id, el]) => {
    const r = el.getBoundingClientRect();
    geo[id] = { x1: r.left - cb.left, x2: r.right - cb.left, cy: r.top - cb.top + r.height / 2 };
  });
  return geo;
}

// ═══════════════════════════════════════════════════════════════════════════
// Render
// ═══════════════════════════════════════════════════════════════════════════
const CURVE = 0.5, DIM = 0.07, SHOW_AUDIT = true;

function render() {
  if (!MODEL) return;   // resize/font observers can fire before the fetch lands
  const active = sel ? reach(sel) : null;

  // nodes
  Object.keys(nodeEls).forEach(id => styleNode(id, active));

  // edges (measure after node styles are set — sizing is stable)
  const geo = measure();
  const out = [];
  EDGES.forEach((e, i) => {
    const A = geo[e.a], B = geo[e.b];
    if (!A || !B) return;
    if (e.t === 'audit' && !SHOW_AUDIT) return;
    if (muted[e.t]) return;
    let d;
    if (e.loop) {
      const x = Math.min(A.x1, B.x1), b1 = x - 30 - Math.abs(A.cy - B.cy) * 0.12;
      d = 'M' + A.x1 + ',' + A.cy + ' C' + b1 + ',' + A.cy + ' ' + b1 + ',' + B.cy + ' ' + B.x1 + ',' + B.cy;
    } else {
      const dx = Math.max(30, (B.x1 - A.x2) * (0.25 + CURVE * 0.6));
      d = 'M' + A.x2 + ',' + A.cy + ' C' + (A.x2 + dx) + ',' + A.cy + ' ' + (B.x1 - dx) + ',' + B.cy + ' ' + B.x1 + ',' + B.cy;
    }
    const on = !active || active.eIdx.has(i);
    const push = key => {
      const s = STYLE[key];
      out.push({ d, stroke: s.stroke, w: s.w, dash: s.dash, cap: s.cap, marker: s.marker, z: s.z,
        o: on ? (active ? 1 : (e.t === 'audit' ? 0.28 : 0.55)) : DIM });
    };
    push(e.t);
    if (e.t === 'phase') push('phaseIn');
  });
  out.sort((a, b) => a.z - b.z);

  edgesG.textContent = '';
  const frag = document.createDocumentFragment();
  out.forEach(p => {
    const path = document.createElementNS(SVGNS, 'path');
    path.setAttribute('d', p.d);
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', p.stroke);
    path.setAttribute('stroke-width', p.w);
    path.setAttribute('stroke-dasharray', p.dash);
    path.setAttribute('stroke-linecap', p.cap);
    if (p.marker !== 'none') path.setAttribute('marker-end', p.marker);
    path.setAttribute('opacity', p.o);
    frag.appendChild(path);
  });
  edgesG.appendChild(frag);

  // status + counts + detail
  renderReadout(active, out.length);
}

function renderReadout(active, shown) {
  const join = a => (a.length ? a.join('   ·   ') : '—');
  const cellCls = 'cell';
  let rows, statusLabel, statusDetail;

  if (sel) {
    const up = {}, down = {};
    (BACK[sel] || []).forEach(e => (up[e.t] = up[e.t] || []).push(label(e.a)));
    (FWD[sel] || []).forEach(e => (down[e.t] = down[e.t] || []).push(label(e.b)));
    const flat = o => Object.entries(o).map(([t, v]) => t + ': ' + v.join(', '));
    statusLabel = KIND[sel[0]];
    statusDetail = label(sel) + ' — ' + (active.nodes.size - 1) + ' connected nodes across ' + active.eIdx.size + ' edges';
    rows = [
      { rel: 'Direct upstream', items: join(flat(up)) },
      { rel: 'Direct downstream', items: join(flat(down)) },
      { rel: 'Objects on this path', items: join([...active.nodes].filter(n => n[0] === 'o').map(label)) },
      { rel: 'Doors reached', items: join([...active.nodes].filter(n => n[0] === 's').map(label)) }
    ];
  } else {
    statusLabel = 'No trace selected';
    statusDetail = 'Click any node to isolate its full read/write path.';
    // computed from the fetched model, so the headline can't drift
    const widest = MODEL.tables
      .map(t => ({ t,
        w: MODEL.verbs.filter(v => MODEL.byName[v].writes_direct.includes(t)).length,
        r: MODEL.derivs.filter(d => (MODEL.readsBy[d] || []).includes(t)).length }))
      .sort((a, b) => (b.w + b.r) - (a.w + a.r))[0];
    const readTables = new Set(MODEL.derivs.flatMap(d => MODEL.readsBy[d] || []));
    const sinks = MODEL.tables.filter(t => !readTables.has(t));
    const orphans = MODEL.tables.filter(t =>
      !MODEL.verbs.some(v => MODEL.byName[v].writes_direct.includes(t)));
    rows = [
      { rel: 'Widest write surface', items: widest.t + ' — written by ' + widest.w + ' verbs, read by ' + widest.r + ' derivations' },
      { rel: 'Longest chain', items: 'MCP write tier → confirm_action → apply_rules → transactions → compute_balance → Flask · MCP · Ask' },
      { rel: 'Never read by a derivation', items: join(sinks) },
      { rel: 'No write verb', items: join(orphans.map(t => t + ' — written outside the registry')) }
    ];
  }

  $('statusLabel').textContent = statusLabel;
  $('statusDetail').textContent = statusDetail;
  $('counts').textContent = NODE_COUNT + ' nodes  ·  ' + shown + ' edges drawn';

  const detail = $('detail');
  detail.textContent = '';
  rows.forEach(r => {
    const cell = document.createElement('div');
    cell.className = cellCls;
    const rel = document.createElement('div');
    rel.className = 'rel';
    rel.textContent = r.rel;
    const items = document.createElement('div');
    items.className = 'items';
    items.textContent = r.items;
    cell.appendChild(rel);
    cell.appendChild(items);
    detail.appendChild(cell);
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Interaction
// ═══════════════════════════════════════════════════════════════════════════
function pick(id) { sel = (sel === id ? null : id); render(); }
function reset() { if (sel !== null) { sel = null; render(); } }

function initLegend() {
  document.querySelectorAll('.lg-row').forEach(row => {
    const t = row.dataset.mute;
    row.addEventListener('click', ev => {
      ev.stopPropagation();
      muted[t] = !muted[t];
      row.classList.toggle('muted', !!muted[t]);
      render();
    });
  });
}

// ── provenance: everything from the fetched manifest ──
function fillProvenance() {
  const prov = $('provenance');
  if (!prov) return;
  prov.textContent = 'Live from /api/ontology — ' +
    MODEL.verbs.length + ' verbs · ' + MODEL.tables.length + ' tables · ' +
    MODEL.derivs.length + ' derivations · schema v' + MODEL.schema;
}

// ═══════════════════════════════════════════════════════════════════════════
// Boot: fetch the manifest, build the model, then wire everything up
// ═══════════════════════════════════════════════════════════════════════════
async function boot() {
  try {
    const r = await fetch('/api/ontology');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    MODEL = buildModel(await r.json());
  } catch (e) {
    $('statusLabel').textContent = 'Could not load the model';
    $('statusDetail').textContent =
      '/api/ontology said: ' + e.message + ' — reload the page (a signed-in session is required).';
    return;
  }
  EDGES = buildEdges();
  FWD = {}; BACK = {};
  EDGES.forEach((e, i) => { e._i = i; (FWD[e.a] = FWD[e.a] || []).push(e); (BACK[e.b] = BACK[e.b] || []).push(e); });
  NODE_COUNT = MODEL.callers.length + MODEL.verbs.length +
    MODEL.tables.length + MODEL.derivs.length + MODEL.doors.length;

  buildColumns();
  initLegend();
  fillProvenance();
  render();
  // fonts can shift metrics; re-measure once they load and on resize
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(render);
  window.addEventListener('resize', render);
  new ResizeObserver(render).observe(canvas);
  window.addEventListener('load', render);
}

$('clearBtn').addEventListener('click', ev => { ev.stopPropagation(); reset(); });
canvas.addEventListener('click', reset);
boot();
