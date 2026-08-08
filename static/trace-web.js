"use strict";
// ═══════════════════════════════════════════════════════════════════════════
// EDGE TABLE — grounded in the real Ledger source (actions.py / derivations.py /
// ledger_mcp.py / agent_read_tools.py / simplefin_sync.py). See docs/CORE-DESIGN.md
// for the invariants this map visualizes: one write path per verb, nothing
// derived is stored, audit_log/api_tokens/pending_actions are write-only sinks.
// Types: call | write | phase | cascade | audit | read | serve
// ───────────────────────────────────────────────────────────────────────────
const OBJ = [
  { g: 'People & shares',    items: ['members', 'splits'] },
  { g: 'Money movement',     items: ['transactions', 'links', 'income_rules', 'budgets'] },
  { g: 'Commitments',        items: ['bills', 'bill_payments', 'goals', 'goal_contributions'] },
  { g: 'Household',          items: ['items'] },
  { g: 'System of record',   items: ['audit_log', 'api_tokens', 'pending_actions'] }
];

// verb → tables it mutates (INSERT/UPDATE/DELETE), excluding audit_log (drawn
// as its own 'audit' grammar). Transitive through helpers (delete_transaction_
// graph, _record_goal_flow, _write_matches); FK cascades made explicit
// (delete_goal → goal_contributions). Verified against actions.py bodies.
const WRITES = {
  record_transaction:  ['transactions', 'splits'],                       // :234
  edit_transaction:    ['transactions', 'splits', 'links'],              // :319 (severs a settles link)
  delete_transaction:  ['transactions', 'splits', 'links'],              // :379 (delete_transaction_graph)
  settle_up:           ['transactions', 'splits', 'links'],              // :399 (writes 'settles' links)
  classify_inflow:     ['transactions'],                                 // :775 (sets income_type)
  create_bill:         ['bills'],                                        // :567
  update_bill:         ['bills'],                                        // :604
  delete_bill:         ['bills'],                                        // :645 (soft delete; payments untouched)
  mark_bill_paid:      ['transactions', 'splits', 'bill_payments'],      // :482
  unmark_bill_paid:    ['transactions', 'splits', 'links', 'bill_payments'], // :541 (graph + payment)
  create_goal:         ['goals'],                                        // :668
  delete_goal:         ['goals', 'goal_contributions'],                  // :703 (contributions cascade)
  contribute_to_goal:  ['goal_contributions'],                           // :752 (no transaction row)
  withdraw_from_goal:  ['goal_contributions'],                           // :758
  create_income_rule:  ['income_rules'],                                 // :899
  set_rule_enabled:    ['income_rules'],                                 // :925
  apply_rules:         ['transactions', 'income_rules'],                 // :1046 (classifies + hit_count)
  propose_action:      ['pending_actions'],                              // :1122 (no audit — nothing executed)
  confirm_action:      ['pending_actions'],                              // :1177 (claims the row; dispatches)
  add_item:            ['items'],                                        // :1324
  set_item_status:     ['items'],                                        // :1359
  archive_item:        ['items'],                                        // :1388
  set_item_match:      ['items'],                                        // sets restock_match
  set_item_interval:   ['items'],                                        // user-set restock cadence (#011)
  set_budget:          ['budgets'],
  remove_budget:       ['budgets'],
  create_api_token:    ['api_tokens'],
  revoke_api_token:    ['api_tokens'],
  // CLI-only fresh-start wipe: DELETEs the money-movement rows + zeroes
  // income_rules.hit_count. No route — the one verb no door can reach.
  reset_money:         ['transactions', 'splits', 'links', 'bill_payments', 'goal_contributions', 'pending_actions', 'income_rules']
};
// the two verbs whose object-writes are the two-phase choreography
const PHASE = { propose_action: ['pending_actions'], confirm_action: ['pending_actions'] };
// propose_action is the one verb that writes no audit row (nothing executed yet)
const NO_AUDIT = new Set(['propose_action']);

// table → derivations that read it (SELECT / JOIN), transitive through helper
// derivations. From derivations.py; note links & income_rules are consumed at
// WRITE time (by verbs), never by a derivation — so neither appears here.
const READS = {
  transactions:       ['compute_balance', 'spending_summary', 'top_merchants', 'category_trend', 'income_summary', 'income_trend', 'savings_rate_trend', 'member_breakdown', 'bill_variance', 'budget_status', 'recurring_charges', 'cash_flow_forecast', 'anomaly_flags', 'last_shopping_trip', 'restock_suggestions', 'restock_forecast', 'staple_spend', 'unmatched_staples', 'stale_shopping_items', 'new_staple_suggestions'],
  splits:             ['compute_balance', 'member_breakdown'],
  members:            ['compute_balance', 'member_breakdown'],
  budgets:            ['budget_status'],
  bills:              ['bill_variance', 'cash_flow_forecast'],
  bill_payments:      ['bill_variance', 'cash_flow_forecast'],
  goals:              ['goal_pace'],
  goal_contributions: ['goal_pace'],
  items:              ['shopping_list', 'low_stock', 'restock_suggestions', 'restock_forecast', 'staple_spend', 'unmatched_staples', 'stale_shopping_items', 'new_staple_suggestions']
};

// 23 public read-time derivations in derivations.py
const DERIVS = ['compute_balance', 'spending_summary', 'top_merchants', 'category_trend', 'income_summary', 'income_trend', 'savings_rate_trend', 'member_breakdown', 'bill_variance', 'budget_status', 'recurring_charges', 'cash_flow_forecast', 'anomaly_flags', 'goal_pace', 'last_shopping_trip', 'shopping_list', 'low_stock', 'restock_suggestions', 'restock_forecast', 'staple_spend', 'unmatched_staples', 'stale_shopping_items', 'new_staple_suggestions'];

// door → derivations it exposes. All three doors surface the full read layer:
// MCP (20 read tools) and Ask both bottom out in the same Flask read endpoints,
// so coverage is identical — "one shared read layer, three doors."
const DOORS = [
  { id: 'Flask JSON API', serves: DERIVS.slice() },
  { id: 'MCP read tier', serves: DERIVS.slice() },
  { id: 'Ask · chat', serves: DERIVS.slice() }
];

const ALL_VERBS = Object.keys(WRITES);

// caller → verbs it can invoke (grounded in app.py routes / simplefin_sync.py /
// ledger_mcp.py write tools / agent_write_tools.py)
const CALLERS = [
  { id: 'UI route', calls: ALL_VERBS.filter(v => v !== 'reset_money') },                // every verb EXCEPT reset_money has a Flask route
  { id: 'SimpleFIN sync', calls: ['record_transaction'] },                             // rules match inside the verb
  { id: 'MCP write tier', calls: ['classify_inflow', 'set_rule_enabled', 'propose_action', 'confirm_action'] },
  { id: 'Ask · chat', calls: ['classify_inflow', 'add_item', 'set_item_status', 'archive_item', 'set_item_match', 'set_item_interval'] },
  { id: 'CLI · maintenance', calls: ['reset_money'] }                                  // the one write path that bypasses every door (deploy/reset-money.md)
];

// verb → verb internal dispatch. confirm_action (the two-phase executor) is the
// ONLY verb that calls other verbs; the money verbs inline their own SQL.
const CASCADE = [
  ['confirm_action', 'create_income_rule'],   // actions.py:1221
  ['confirm_action', 'apply_rules']           // actions.py:1226
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

// ── build the edge list ──
function buildEdges() {
  const E = [];
  CALLERS.forEach(c => c.calls.forEach(v => E.push({ t: 'call', a: 'c:' + c.id, b: 'v:' + v })));
  Object.entries(WRITES).forEach(([v, os]) => os.forEach(o => {
    const isPhase = (PHASE[v] || []).includes(o);
    E.push({ t: isPhase ? 'phase' : 'write', a: 'v:' + v, b: 'o:' + o });
  }));
  ALL_VERBS.forEach(v => { if (!NO_AUDIT.has(v)) E.push({ t: 'audit', a: 'v:' + v, b: 'o:audit_log' }); });
  Object.entries(READS).forEach(([o, ds]) => ds.forEach(d => E.push({ t: 'read', a: 'o:' + o, b: 'd:' + d })));
  DOORS.forEach(dr => dr.serves.forEach(d => E.push({ t: 'serve', a: 'd:' + d, b: 's:' + dr.id })));
  CASCADE.forEach(([a, b]) => E.push({ t: 'cascade', a: 'v:' + a, b: 'v:' + b, loop: true }));
  return E;
}
const EDGES = buildEdges();

// adjacency for reachability
const FWD = {}, BACK = {};
EDGES.forEach((e, i) => { e._i = i; (FWD[e.a] = FWD[e.a] || []).push(e); (BACK[e.b] = BACK[e.b] || []).push(e); });

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
const NODE_COUNT = CALLERS.length + ALL_VERBS.length +
  OBJ.reduce((n, g) => n + g.items.length, 0) + DERIVS.length + DOORS.length;

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
  CALLERS.forEach(c => $('col-callers').appendChild(nodeEl('c:' + c.id, c.id)));
  ALL_VERBS.forEach(v => $('col-verbs').appendChild(nodeEl('v:' + v, v)));
  OBJ.forEach(grp => {
    const h = document.createElement('div');
    h.className = 'grp-head';
    h.textContent = grp.g;
    $('col-objects').appendChild(h);
    grp.items.forEach(o => $('col-objects').appendChild(nodeEl('o:' + o, o)));
  });
  DERIVS.forEach(d => $('col-derivs').appendChild(nodeEl('d:' + d, d)));
  DOORS.forEach(dr => $('col-doors').appendChild(nodeEl('s:' + dr.id, dr.id)));
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
const SINKS = new Set(['o:audit_log', 'o:api_tokens', 'o:pending_actions']);

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
    // computed, so the headline can't drift from the data as verbs are added
    const txnWriters = Object.values(WRITES).filter(t => t.includes('transactions')).length;
    const txnReaders = (READS.transactions || []).length;
    rows = [
      { rel: 'Widest write surface', items: 'transactions — written by ' + txnWriters + ' verbs, read by ' + txnReaders + ' derivations' },
      { rel: 'Longest chain', items: 'MCP write tier → confirm_action → apply_rules → transactions → compute_balance → Flask · MCP · Ask' },
      { rel: 'Write-only sinks', items: 'audit_log   ·   api_tokens   ·   pending_actions   —   plus links & income_rules, read only by verbs' },
      { rel: 'Read-only inputs', items: 'members — the one governed table with no write verb' }
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

// ── provenance line: counts computed from the edge data (never drift), schema
//    version injected server-side into data-schema by the /trace route ──
function fillProvenance() {
  const prov = $('provenance');
  if (!prov) return;
  const tables = OBJ.reduce((n, g) => n + g.items.length, 0);
  prov.textContent = 'Reconciled against actions.py · derivations.py — ' +
    ALL_VERBS.length + ' verbs · ' + tables + ' tables · ' + DERIVS.length +
    ' derivations · schema v' + (prov.dataset.schema || '?');
}

// ── boot ──
buildColumns();
initLegend();
fillProvenance();
$('clearBtn').addEventListener('click', ev => { ev.stopPropagation(); reset(); });
canvas.addEventListener('click', reset);

// fonts can shift metrics; re-measure once they load and on resize
render();
if (document.fonts && document.fonts.ready) document.fonts.ready.then(render);
window.addEventListener('resize', render);
new ResizeObserver(render).observe(canvas);
window.addEventListener('load', render);
