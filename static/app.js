/* Ledger frontend — plain JS, no build step. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

// Pure presentation helpers live in render.js (loaded before this file) so
// they can be unit-tested in plain node; app.js pulls them off the global.
const { fmt, esc, ord, monthName, nudgeText, ruleSuggestionText, transferRuleText, catEmoji,
        vsLastMonth, incomeCardHTML, incomeTrendChartHTML, spendingCompositionHTML,
        memberBreakdownHTML, billVarianceHTML, budgetStatusHTML, savingsRateTrendHTML,
        categoryTrendHTML, cashFlowForecastHTML, anomaliesHTML,
        recurringChargesHTML, goalPaceHTML,
        goalWhatIfText,
        askThreadHTML, inventoryHTML, agentsHTML, opsPanelHTML,
        moreSheetHTML, helpSheetHTML, calendarLinkHTML, recatSheetHTML, settleBreakdownHTML,
        beamHTML, txnRow, billRowHTML, contribLogHTML, goalCardHTML } = window.Render;

// One local-time source for "today" / "this month" — the user's calendar, not
// UTC. Both the initial selected month and the Bills header read it, so the app
// no longer mixes a UTC month with local dates (CODE-REVIEW-2026-08-07 #6).
const todayISO = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
const thisMonthISO = () => todayISO().slice(0, 7);

const state = {
  meId: null,
  users: [],          // [{id, display_name}]
  tab: "dashboard",
  month: thisMonthISO(),
  activityFilter: "all",   // all | spending | income

  editingTxn: null,
  editingBill: null,
  payingBill: null,
  contribGoal: null,
  openLogs: new Set(),

  ask: { messages: [], pending: false, prefill: "" },   // Ask tab: client-held chat history; prefill = A4 seed
};

// userById / userColor moved into render.js (pure, users injected) alongside
// txnRow — its only callers. app.js reaches them as window.Render.userById(
// state.users, id) at the few remaining sites, so nothing here reads them off
// a global.

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) {
    showAuth();
    throw new Error("authentication required");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
  return data;
}

/* ================= auth ================= */

function showAuth(setupRequired = false) {
  $("#view-app").classList.add("hidden");
  $("#view-auth").classList.remove("hidden");
  $("#form-setup").classList.toggle("hidden", !setupRequired);
  $("#form-login").classList.toggle("hidden", setupRequired);
  $("#auth-sub").textContent = setupRequired
    ? "First run — set up your two accounts."
    : "Household finance for two.";
}

async function showApp() {
  const me = await api("/api/me");
  state.meId = me.user_id;
  state.users = me.users;
  $("#view-auth").classList.add("hidden");
  $("#view-app").classList.remove("hidden");
  buildNav();
  renderHeader();
  await loadCategories();
  setTab(state.tab);
}

// The Garden greeting: a time-of-day hello + the household's avatars.
// Called once at login, then kept fresh (see the clock wiring near boot())
// so a tab left open doesn't say "Good afternoon" at 10pm.
function renderHeader() {
  if (!state.users) return; // not logged in yet — nothing to render
  const h = new Date().getHours();
  const part = h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  // The Garden's one ambient pantry line (Pantry v2 inc 6): the list count
  // from /api/inventory/badge (fetched with the dashboard; /api/dashboard
  // itself is frozen byte-identical to v1). Nothing shows when it's empty.
  const n = window._pantryBadge ? window._pantryBadge.list_count : 0;
  const pantry = n ? ` · ${n} thing${n === 1 ? "" : "s"} on the list` : "";
  $("#greet-kicker").textContent = `${part} 🌿${pantry}`;
  $("#avatars").innerHTML = state.users.map((u, i) => {
    const color = i === 0 ? "var(--p1)" : "var(--p2)";
    const initial = (u.display_name || "?").trim().charAt(0).toUpperCase();
    return `<span class="av" style="background:${color}">${esc(initial)}</span>`;
  }).join("");
}

// Light/dark: follow the phone by default, or a saved manual choice. The
// data-theme attribute overrides the prefers-color-scheme media query (both
// wired in style.css); the toggle flips relative to whatever's showing now.
function systemDark() { return matchMedia("(prefers-color-scheme: dark)").matches; }

function applyTheme(t) {
  const root = document.documentElement;
  if (t === "light" || t === "dark") root.setAttribute("data-theme", t);
  else root.removeAttribute("data-theme");
  const showingDark = t === "dark" || (t !== "light" && systemDark());
  const btn = $("#theme-toggle");
  if (btn) btn.textContent = showingDark ? "☀︎" : "☾";  // the mode you'd switch TO
}

function initTheme() {
  applyTheme(localStorage.getItem("ledger-theme"));
  $("#theme-toggle")?.addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const showingDark = cur === "dark" || (cur !== "light" && systemDark());
    const next = showingDark ? "light" : "dark";
    localStorage.setItem("ledger-theme", next);
    // Crossfade the whole page between themes where supported.
    if (document.startViewTransition) document.startViewTransition(() => applyTheme(next));
    else applyTheme(next);
  });
}

async function boot() {
  initTheme();  // apply the saved (or system) theme + wire the toggle
  try {
    const s = await api("/api/status");
    if (s.setup_required) return showAuth(true);
    if (!s.logged_in) return showAuth(false);
    await showApp();
  } catch (e) {
    showAuth(false);
  }
}

$("#form-login").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  $("#login-error").textContent = "";
  try {
    await api("/api/login", {
      method: "POST",
      body: { username: f.username.value, password: f.password.value },
    });
    f.reset();
    await showApp();
  } catch (e) {
    $("#login-error").textContent = e.message;
  }
});

$("#form-setup").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  $("#setup-error").textContent = "";
  try {
    await api("/api/setup", {
      method: "POST",
      body: {
        users: [
          { display_name: f.d0.value, username: f.u0.value, password: f.p0.value },
          { display_name: f.d1.value, username: f.u1.value, password: f.p1.value },
        ],
      },
    });
    showAuth(false);
    $("#auth-sub").textContent = "Accounts created — sign in.";
  } catch (e) {
    $("#setup-error").textContent = e.message;
  }
});

$("#btn-logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  showAuth(false);
});

// Change password: a session-only write (POST /api/me/password → the
// change_password verb). The dialog checks the two new-password fields agree
// before spending a request; the verb owns every other rule (current password
// verifies, >= 8 chars, differs). Errors show inline; success closes it.
const dlgPassword = $("#dlg-password");
$("#btn-password")?.addEventListener("click", () => {
  $("#form-password").reset();
  $("#password-error").textContent = "";
  dlgPassword.showModal();
});
$("#form-password")?.addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;   // Cancel closes natively
  e.preventDefault();
  const f = e.target;
  const err = $("#password-error");
  err.textContent = "";
  if (f.new_password.value !== f.confirm_password.value) {
    err.textContent = "The new passwords don't match.";
    return;
  }
  try {
    await api("/api/me/password", { method: "POST", body: {
      current_password: f.current_password.value,
      new_password: f.new_password.value } });
    dlgPassword.close();
    f.reset();
  } catch (ex) {
    err.textContent = ex.message;
  }
});
dlgPassword?.addEventListener("click", (e) => { if (e.target === dlgPassword) dlgPassword.close(); });

/* ================= navigation ================= */

const TABS = [
  ["dashboard", "Home"],
  ["activity", "Activity"],
  ["bills", "Bills"],
  ["goals", "Goals"],
  ["analytics", "Analytics"],
  ["inventory", "Pantry"],
  ["ask", "Ask"],
  ["agents", "Agents"],
];

// Glyph per tab — one source for both the mobile bar and the More sheet.
const TAB_GLYPH = {
  dashboard: "🏡", activity: "📋", bills: "📅", goals: "🌱",
  analytics: "📊", inventory: "🧺", ask: "💬", agents: "🤖",
};
// The "More" sheet lists every tab (with its glyph), so all 8 are reachable
// from any page. Built from TABS so a new tab shows up here automatically.
const MORE_TABS = TABS.map(([key, label]) => [key, label, TAB_GLYPH[key]]);
const PINNED = new Set(["dashboard", "activity", "ask"]);

// The mobile bottom bar pins Home · Activity · [+] · Ask, plus a More button
// that opens the sheet with the rest. The center + adds a transaction (same as
// the desktop FAB). Desktop keeps the full text nav in the topbar.
const NAV_MOBILE = [
  ["dashboard", "Home", "🏡"],
  ["activity", "Activity", "📋"],
  ["__add__", "Add", "+"],
  ["ask", "Ask", "💬"],
  ["__more__", "More", "☰"],
];

function buildNav() {
  const topnav = $("#topnav");
  topnav.innerHTML = "";
  for (const [key, label] of TABS) {
    const b = document.createElement("button");
    b.textContent = label;
    b.dataset.tab = key;
    b.addEventListener("click", () => setTab(key));
    topnav.appendChild(b);
  }
  const tabbar = $("#tabbar");
  tabbar.innerHTML = "";
  for (const [key, label, glyph] of NAV_MOBILE) {
    const b = document.createElement("button");
    b.type = "button";
    if (key === "__add__") {
      b.className = "nav-add";
      b.textContent = glyph;
      b.setAttribute("aria-label", "Add expense");
      b.addEventListener("click", () => openTxnDialog(null));
    } else if (key === "__more__") {
      b.className = "nav-more";
      b.innerHTML = `<span class="g">${glyph}</span><span class="l">${label}</span>`;
      b.setAttribute("aria-label", "More tabs");
      b.addEventListener("click", openMoreSheet);
    } else {
      b.dataset.tab = key;
      b.innerHTML = `<span class="g">${glyph}</span><span class="l">${label}</span>`;
      b.addEventListener("click", () => setTab(key));
    }
    tabbar.appendChild(b);
  }
}

function setTab(tab) {
  state.tab = tab;
  $$("#topnav button, #tabbar button").forEach((b) =>
    b.classList.toggle("on", b.dataset.tab === tab));
  // The More button lights up when the current tab lives inside the sheet.
  $(".nav-more")?.classList.toggle("on", !PINNED.has(tab) && tab !== "dashboard");
  render();
}

// A4: "ask from anywhere". A contextual affordance around the app opens the Ask
// tab with the box pre-filled by a fixed, plain-language question keyed here (so
// render fns stay pure — they only emit `data-ask="<key>"`). The seed is a
// starting point, not auto-sent: the person can edit or clear it before asking.
const ASK_PREFILLS = {
  month: "How are we doing this month?",
  pantry: "What do we need from the store?",
  balance: "Why do we owe this amount right now?",
};
function askFrom(key) {
  state.ask.prefill = ASK_PREFILLS[key] || "";
  if (dlgSettle && dlgSettle.open) dlgSettle.close();  // when reached from a modal
  setTab("ask");
}

// The "More" bottom sheet: fill it with every tab, open it, and let a tile
// switch tabs (then close). Reachable from any page, so all 8 tabs are too.
const dlgMore = $("#dlg-more");
function openMoreSheet() {
  $("#more-body").innerHTML = moreSheetHTML(MORE_TABS, state.tab);
  $$("#more-body [data-tab]").forEach((b) =>
    b.addEventListener("click", () => { dlgMore.close(); setTab(b.dataset.tab); }));
  dlgMore.showModal();
}
// Tap the backdrop (outside the sheet body) to dismiss.
dlgMore?.addEventListener("click", (e) => { if (e.target === dlgMore) dlgMore.close(); });

// The Help sheet: the in-app "how this works" guide. Opened from the header
// "?" and from the Ask empty state. Reads nothing; every tab row (and the
// "Open Ask" button) navigates and closes the sheet.
const dlgHelp = $("#dlg-help");
function openHelpSheet() {
  $("#help-body").innerHTML = helpSheetHTML(MORE_TABS);
  $$("#help-body [data-tab]").forEach((b) =>
    b.addEventListener("click", () => { dlgHelp.close(); setTab(b.dataset.tab); }));
  // The calendar section fetches the member's own subscribe link on demand
  // (the token is per-member, so it can't be baked into the pure sheet HTML).
  $("#help-cal-btn")?.addEventListener("click", async () => {
    try {
      $("#help-cal").innerHTML = calendarLinkHTML(await api("/api/calendar/link"));
    } catch {
      $("#help-cal").innerHTML =
        '<p class="help-cal-url">Couldn\'t fetch the link just now — try again in a moment.</p>';
    }
  });
  dlgHelp.showModal();
  // A <dialog> keeps its scroll position across close/open, and showModal's
  // autofocus can nudge it; always open at the top so the same tap lands on
  // the same row every time.
  dlgHelp.scrollTop = 0;
}
$("#btn-help")?.addEventListener("click", openHelpSheet);
dlgHelp?.addEventListener("click", (e) => { if (e.target === dlgHelp) dlgHelp.close(); });

// Recategorize sheet: the transactions behind one Home "Spent" row, as a
// checklist you move into another category. The read is /api/activity's new
// category filter (spending only, this month); the write reuses the
// edit-transaction verb per checked id — one audited edit each, splits and
// balance untouched (a category-only edit relabels, nothing more).
const dlgRecat = $("#dlg-recat");
async function openRecatSheet(category) {
  const month = (window._dash && window._dash.month) || state.month;
  const data = await api(
    `/api/activity?filter=spending&month=${encodeURIComponent(month)}` +
    `&category=${encodeURIComponent(category)}`);
  $("#recat-body").innerHTML = recatSheetHTML(
    category, monthName(month), data.transactions);
  const moveBtn = $("#recat-move");
  const input = $("#recat-category");
  const checks = () => $$(".recat-check");
  // Move is enabled only when at least one row is checked AND a target
  // category is typed (and it's not the category you're already in).
  const deleteBtn = $("#recat-delete-cat");
  const syncMove = () => {
    const target = (input?.value || "").trim();
    const validTarget = !!target && target !== category;
    if (moveBtn)
      moveBtn.disabled = !(validTarget && checks().some((c) => c.checked));
    // Delete-category needs only a destination — it moves ALL months, not
    // the checklist (which shows this month only).
    if (deleteBtn) deleteBtn.disabled = !validTarget;
  };
  $("#recat-select-all")?.addEventListener("change", (e) => {
    checks().forEach((c) => { c.checked = e.target.checked; });
    syncMove();
  });
  checks().forEach((c) => c.addEventListener("change", () => {
    // Keep select-all honest: it's checked only when every row is.
    const all = $("#recat-select-all");
    if (all) all.checked = checks().every((x) => x.checked);
    syncMove();
  }));
  input?.addEventListener("input", syncMove);
  $("#recat-cancel")?.addEventListener("click", () => dlgRecat.close());
  // Delete the category: one atomic merge_category call relabels every
  // reference (all months + budget/bills/pantry) into the typed target —
  // orphan-proof by construction (the verb refuses without a destination).
  deleteBtn?.addEventListener("click", async () => {
    const target = input.value.trim();
    if (!confirm(`Move every “${category}” transaction from every month ` +
                 `(plus its budget/bill/pantry references) into “${target}” ` +
                 `and delete “${category}”?`)) return;
    deleteBtn.disabled = true;
    await api("/api/categories/merge", {
      method: "POST",
      body: { from_category: category, into_category: target } });
    dlgRecat.close();
    render();
  });
  moveBtn?.addEventListener("click", async () => {
    const target = input.value.trim();
    const ids = checks().filter((c) => c.checked)
                        .map((c) => +c.dataset.recatId);
    if (!ids.length || !target) return;
    moveBtn.disabled = true;
    // One edit per transaction — the deployed write path. Sequential so a
    // mid-batch failure surfaces without half the UI racing ahead.
    for (const id of ids)
      await api(`/api/transactions/${id}`, {
        method: "PUT", body: { category: target } });
    dlgRecat.close();
    render();
  });
  dlgRecat.showModal();
}
dlgRecat?.addEventListener("click", (e) => { if (e.target === dlgRecat) dlgRecat.close(); });

// Per-tab row stashes the tap/edit handlers read (findTxn, bill/goal/budget
// edit, the pantry match editor). Cleared at the top of every render so a tab
// only ever sees ITS OWN current rows — otherwise a stale pool (e.g. _txns,
// written by Activity and never cleared) let a tap on another tab resolve to a
// pre-edit row and Save write the stale values back (CODE-REVIEW-2026-08-07 #5).
const ROW_STASHES = ["_dash", "_recent", "_txns", "_bills", "_goals", "_goalPace",
                     "_budgets", "_budgetStatus", "_inv"];

async function render() {
  const main = $("#main");
  ROW_STASHES.forEach((k) => { delete window[k]; });
  try {
    if (state.tab === "dashboard") main.innerHTML = await renderDashboard();
    if (state.tab === "activity") main.innerHTML = await renderActivity();
    if (state.tab === "bills") main.innerHTML = await renderBills();
    if (state.tab === "goals") main.innerHTML = await renderGoals();
    if (state.tab === "analytics") main.innerHTML = await renderAnalytics();
    if (state.tab === "inventory") main.innerHTML = await renderInventory();
    if (state.tab === "ask") main.innerHTML = renderAsk();
    if (state.tab === "agents") main.innerHTML = await renderAgents();
    wireMain();
  } catch (e) {
    if (e.message !== "authentication required")
      main.innerHTML = `<p class="empty">${esc(e.message)}</p>`;
  }
}

/* ================= dashboard ================= */

async function renderDashboard() {
  const d = await api("/api/dashboard");
  // Same month the dashboard resolved to, so the two cards always agree;
  // the /api/dashboard call itself stays unchanged (parity-frozen shape).
  const inc = await api(`/api/income/summary?month=${d.month}`);
  // This month vs last, for the Spent pill — a 2-month trend window ending at
  // the dashboard's month; month_spend is the same net-of-refunds figure.
  const trend = await api(`/api/income/trend?months_back=2&anchor=${d.month}`);
  const spentPill = vsLastMonth(trend.series);
  window._dash = d;
  // The pantry badge rides alongside (not inside) the frozen dashboard payload;
  // a failure here only costs the ambient line, never the Home render.
  api("/api/inventory/badge").then((b) => { window._pantryBadge = b; renderHeader(); })
    .catch(() => {});
  const maxCat = Math.max(1, ...d.by_category.map((c) => c.amount));
  // Each row taps through to the recategorize sheet for that category+month —
  // a button, not a div, so it's keyboard-reachable; the chevron is the
  // affordance that it's actionable.
  const cats = d.by_category.length
    ? d.by_category.map((c) => `
        <button type="button" class="cat-row" data-spent-cat="${esc(c.category)}"
                aria-label="Recategorize spending tagged ${esc(c.category)}">
          <span class="cat-name">${esc(c.category)}</span>
          <span class="cat-bar"><i style="width:${(c.amount / maxCat) * 100}%"></i></span>
          <span class="amt amount">${fmt(c.amount)}</span>
          <span class="cat-go" aria-hidden="true">›</span>
        </button>`).join("")
    : `<p class="empty">Nothing spent yet this month.</p>`;

  const today = new Date().getDate();
  const bills = d.unpaid_bills.length
    ? `<ul class="list">${d.unpaid_bills.map((b) => `
        <li>
          <span class="ic">${catEmoji(b.category || b.name)}</span>
          <div class="grow">
            <div class="title">${esc(b.name)}</div>
            <div class="sub">due the ${ord(b.due_day)}</div>
          </div>
          <span class="badge ${b.due_day < today ? "overdue" : "due"}">
            ${b.due_day < today ? "overdue" : "upcoming"}</span>
          <span class="amt amount">${fmt(b.amount)}</span>
        </li>`).join("")}</ul>`
    : `<p class="empty">All bills paid this month 🎉</p>`;

  const goals = d.goals.length
    ? d.goals.map((g) => {
        const pct = Math.round(g.progress * 100);
        const note = pct >= 60 && pct < 100 ? " — almost there" : "";
        return `
        <div class="goal">
          <div class="goal-head"><h3>🌱 ${esc(g.name)}</h3>
            <span class="amt amount">${fmt(g.saved)} / ${fmt(g.target)}</span></div>
          <div class="goal-bar"><i style="width:${g.progress * 100}%"></i></div>
          <p class="goal-pct">${pct}%${note}</p>
        </div>`;
      }).join("")
    : `<p class="empty">No goals yet — add one in the Goals tab.</p>`;

  // Render the recent list from /api/activity so inflows show income-aware
  // (green, chip) rather than as plain spend rows — /api/dashboard's own
  // `recent` is the parity-frozen txn_to_json shape with no direction. The
  // ids match, so the tap-to-edit fallback (window._dash.recent) still
  // resolves the same rows.
  const recentTxns = (await api("/api/activity?filter=all")).transactions.slice(0, 6);
  window._recent = recentTxns;   // income-aware rows for the tap handler
  const recent = recentTxns.length
    ? `<ul class="list">${recentTxns.map((t) => txnRow(t, state.users)).join("")}</ul>`
    : `<p class="empty">No transactions yet. Tap + to add the first one.</p>`;

  return `
    ${beamHTML(d.balance)}
    <div class="card">
      <div class="spent-head">
        <div>
          <p class="eyebrow" style="margin-bottom:2px">Spent in ${monthName(d.month)}</p>
          <p class="stat-big">${fmt(d.month_total)}</p>
        </div>
        ${spentPill ? `<span class="pill ${spentPill.dir}">${spentPill.text}</span>` : ""}
      </div>
      <div style="margin-top:12px">${cats}</div>
      <button type="button" class="ask-from" data-ask="month">💬 Ask about this month</button>
    </div>
    ${incomeCardHTML(inc, d.month)}
    <div class="card"><p class="eyebrow">Coming up</p>${bills}</div>
    <div class="card"><p class="eyebrow">Growing toward</p>${goals}</div>
    <div class="card"><p class="eyebrow">Recent</p>${recent}</div>`;
}

async function renderAgents() {
  const [data, health, audit] = await Promise.all([
    api("/api/agents"), api("/api/ops/health"), api("/api/ops/audit?limit=30")]);
  return agentsHTML(data) + opsPanelHTML(health, audit);
}

/* ================= activity ================= */

async function renderActivity() {
  const data = await api(
    `/api/activity?month=${state.month}&filter=${state.activityFilter}`);
  const txns = data.transactions;
  window._txns = txns;   // the tap-to-edit path reads this
  const emptyLabel = { all: "No transactions", spending: "No spending",
                       income: "No income" }[state.activityFilter];
  const list = txns.length
    ? `<ul class="list">${txns.map((t) => txnRow(t, state.users)).join("")}</ul>`
    : `<p class="empty">${emptyLabel} in ${monthName(state.month)}.</p>`;
  const seg = (key, label) =>
    `<button data-filter="${key}"${state.activityFilter === key ? ' class="on"' : ""}>${label}</button>`;
  // Global (all-months) tag-me nudge; tapping jumps to the Income filter.
  const badge = data.unclassified_count > 0
    ? `<button class="tagbanner" data-filter="income">${nudgeText(data.unclassified_count)}</button>`
    : "";
  return `
    <div class="monthbar">
      <button id="month-prev" aria-label="Previous month">‹</button>
      <b>${monthName(state.month)}</b>
      <button id="month-next" aria-label="Next month">›</button>
    </div>
    <div class="filterbar">
      ${seg("all", "All")}${seg("spending", "Spending")}${seg("income", "Income")}
    </div>
    ${badge}
    <div class="card">${list}</div>`;
}

/* ================= bills ================= */

async function renderBills() {
  const bills = await api("/api/bills");
  window._bills = bills;
  const rows = bills.length
    ? `<ul class="list">${bills.map(billRowHTML).join("")}</ul>`
    : `<p class="empty">No recurring bills yet.</p>`;
  return `
    <div class="section-head">
      <p class="eyebrow" style="margin:0">Bills — ${monthName(thisMonthISO())}</p>
      <button class="btn small" id="btn-add-bill">Add bill</button>
    </div>
    <div class="card">${rows}</div>`;
}

/* ================= goals ================= */

async function renderGoals() {
  // Pace rides along: the same /api/analytics/goal-pace the analytics card
  // reads (one derivation, every surface), matched to cards by goal_id. The
  // pace entries also carry remaining.cents for the per-goal what-if.
  const [goals, pace] = await Promise.all([
    api("/api/goals"),
    api("/api/analytics/goal-pace"),
  ]);
  window._goals = goals;
  window._goalPace = {};
  (pace.goals || []).forEach((p) => { window._goalPace[p.goal_id] = p; });
  const cards = await Promise.all(goals.map(async (g) => {
    const logOpen = state.openLogs.has(g.id);
    let logHTML = "";
    if (logOpen) {
      const rows = await api(`/api/goals/${g.id}/contributions`);
      logHTML = contribLogHTML(rows);
    }
    return goalCardHTML(g, window._goalPace[g.id], logOpen, logHTML);
  }));
  return `
    <div class="section-head">
      <p class="eyebrow" style="margin:0">Savings goals</p>
      <button class="btn small" id="btn-add-goal">New goal</button>
    </div>
    ${cards.join("") || `<p class="empty">No goals yet — create your first.</p>`}`;
}

/* ================= analytics ================= */

async function renderAnalytics() {
  // A trailing 6-month window ending at the month the user is viewing;
  // month-prev/next (wired in wireMain) shift the whole window. Every card
  // reads a Tier A endpoint — no math here, the server computed it all.
  const m = state.month;
  const [trend, comp, members, bills, savings, forecast, anomalies, recurring, goals,
         budgetStatus, budgetList, categories] =
    await Promise.all([
      api(`/api/income/trend?anchor=${m}&months_back=6`),
      api(`/api/analytics/spending-composition?month=${m}`),
      api(`/api/analytics/member-breakdown?month=${m}`),
      api(`/api/analytics/bill-variance?period=${m}`),
      api(`/api/analytics/savings-rate-trend?anchor=${m}&months_back=6`),
      api(`/api/analytics/cash-flow-forecast?period=${m}`),
      api(`/api/analytics/anomalies?month=${m}`),
      api(`/api/analytics/recurring`),           // not month-scoped
      api(`/api/analytics/goal-pace`),           // projects from today
      api(`/api/analytics/budget-status?period=${m}`),
      api(`/api/budgets`),                       // ids, for the remove handler
      api(`/api/categories`),                    // the add form's picker
    ]);
  // Stashed for the budget action handlers: the shown rows (index-addressed) and
  // the id-carrying list (category→id for remove). Same pattern as window._inv.
  window._budgetStatus = budgetStatus.budgets || [];
  window._budgets = budgetList || [];
  // Drill into the biggest category this month — a trend without a picker.
  let catCard = "";
  const top = (comp.by_category || [])[0];
  if (top) {
    const ct = await api(
      `/api/analytics/category-trend?category=${encodeURIComponent(top.category)}` +
      `&anchor=${m}&months_back=6`);
    catCard = categoryTrendHTML(ct);
  }
  return `
    <div class="monthbar">
      <button id="month-prev" aria-label="Earlier months">‹</button>
      <b>through ${monthName(m)}</b>
      <button id="month-next" aria-label="Later months">›</button>
    </div>
    ${incomeTrendChartHTML(trend.series)}
    ${cashFlowForecastHTML(forecast)}
    ${savingsRateTrendHTML(savings)}
    ${spendingCompositionHTML(comp)}
    ${budgetStatusHTML(budgetStatus, categories)}
    ${anomaliesHTML(anomalies)}
    ${catCard}
    ${recurringChargesHTML(recurring)}
    ${memberBreakdownHTML(members)}
    ${billVarianceHTML(bills)}
    ${goalPaceHTML(goals)}`;
}

// Budgets (Analytics Tier C): set/edit upserts one category's monthly limit;
// remove soft-deletes. The action buttons carry the array index into the shown
// rows; edit/remove read the row out of window._budgetStatus (and map
// category→id via window._budgets for the delete). All re-render on success.
async function setBudget(category, amount) {
  category = (category || "").trim();
  if (!category || amount === "" || amount == null) return;
  await api("/api/budgets", { method: "POST", body: { category, amount } });
  render();
}
async function editBudget(idx) {
  const row = (window._budgetStatus || [])[idx];
  if (!row) return;
  const cur = row.budgeted && row.budgeted.cents != null ? row.budgeted.cents / 100 : "";
  const next = prompt(`Monthly limit for ${row.category}`, cur);
  if (next === null) return;                       // cancelled
  await api("/api/budgets", { method: "POST", body: { category: row.category, amount: next } });
  render();
}
async function removeBudget(idx) {
  const row = (window._budgetStatus || [])[idx];
  if (!row) return;
  const b = (window._budgets || []).find((x) => x.category === row.category);
  if (!b) return;
  if (!confirm(`Remove the ${row.category} budget?`)) return;
  await api(`/api/budgets/${b.id}`, { method: "DELETE" });
  render();
}

/* ================= inventory ("the pantry") ================= */

// The status a tap cycles to: stocked → low → out → stocked. One tap walks the
// staple down as it's used, and back up once restocked.
const NEXT_STATUS = { stocked: "low", low: "out", out: "stocked", ordered: "stocked" };

async function renderInventory() {
  const data = await api("/api/inventory");
  window._inv = data;  // stashed so the match editor can pre-fill the current phrase
  // today (local ISO date) drives the forecast's "due in N days / overdue"
  // framing — the derivation is clock-free, so "now" enters at the view layer.
  const today = new Date().toLocaleDateString("en-CA");  // YYYY-MM-DD, local
  return inventoryHTML(data, today);
}

async function setItemStatus(id, status) {
  await api(`/api/inventory/${id}`, { method: "PUT", body: { status } });
  render();
}

// Set (or clear) a staple's optional purchase-match phrase — the override the
// restock-suggestion derivation matches against instead of the item name.
// Blank clears it (the backend treats "" as clear).
async function setItemMatch(id) {
  const item = (window._inv && window._inv.items || []).find((x) => x.id === id);
  const cur = item ? (item.restock_match || "") : "";
  const next = prompt(
    "Match purchases whose description contains… (leave blank to use the item's name)",
    cur);
  if (next === null) return;               // cancelled
  if (next.trim() === cur) return;         // unchanged
  await api(`/api/inventory/${id}`, { method: "PUT", body: { restock_match: next.trim() } });
  render();
}

// Look an item up across both pantry lists (staples + shopping, which also
// holds one-offs) so the #014 prompt-setters can pre-fill current values.
function invItem(id) {
  const inv = window._inv || {};
  return [...(inv.items || []), ...(inv.shopping || [])].find((x) => x.id === id);
}

// Set (or clear) where an item is bought — the store the shopping list groups
// by (#014). Blank clears it (ungrouped).
async function setItemStore(id) {
  const cur = (invItem(id) || {}).store || "";
  const next = prompt("Where do you buy this? (leave blank to ungroup)", cur);
  if (next === null || next.trim() === cur) return;
  await api(`/api/inventory/${id}`, { method: "PUT", body: { store: next.trim() } });
  render();
}

// Set (or clear) an item's deadline — sorts the list needed-soonest (#014).
async function setItemNeedBy(id) {
  const cur = (invItem(id) || {}).need_by || "";
  const next = prompt("Needed by (YYYY-MM-DD, blank to clear)", cur);
  if (next === null || next.trim() === cur) return;
  await api(`/api/inventory/${id}`, { method: "PUT", body: { need_by: next.trim() } });
  render();
}

// Snooze an item's nudges until a date, or wake it (#014). The row keeps its
// status; it just stops nagging (and leaves the active list) until then.
async function setItemSnooze(id) {
  const cur = (invItem(id) || {}).snoozed_until || "";
  const next = prompt("Snooze until (YYYY-MM-DD, blank to wake)", cur);
  if (next === null || next.trim() === cur) return;
  await api(`/api/inventory/${id}`, { method: "PUT", body: { snoozed_until: next.trim() } });
  render();
}

// Set (or clear) a staple's manual restock cadence — "remind me every N days".
// When set, the forecast counts N days from the last time it was marked stocked
// instead of inferring the interval from purchase history. Blank clears it (back
// to the inferred cadence); the backend validates 1..365.
async function setItemInterval(id) {
  const item = (window._inv && window._inv.items || []).find((x) => x.id === id);
  const cur = item && item.restock_interval_days ? String(item.restock_interval_days) : "";
  const next = prompt(
    "Remind me to restock this every how many days? (leave blank to clear and " +
    "let the app learn it from your purchases)",
    cur);
  if (next === null) return;               // cancelled
  if (next.trim() === cur) return;         // unchanged
  await api(`/api/inventory/${id}`, {
    method: "PUT",
    body: { restock_interval_days: next.trim() === "" ? null : next.trim() },
  });
  render();
}

async function addItem(name, kind) {
  name = (name || "").trim();
  if (!name) return;
  await api("/api/inventory", { method: "POST", body: { name, kind } });
  render();
}

// "Track" on a new-staple suggestion: start tracking that merchant as a staple,
// seeding its restock-match phrase from the suggestion so future purchases match.
// Reads the row out of window._inv by index (no user content in the attribute).
async function trackSuggestedStaple(idx) {
  const s = (window._inv && window._inv.new_staple_suggestions || [])[idx];
  if (!s) return;
  await api("/api/inventory", {
    method: "POST",
    body: { name: s.merchant, kind: "staple", restock_match: s.suggested_match },
  });
  render();
}

/* ================= ask ================= */

// Renders from client state only (no fetch), so re-rendering mid-chat is cheap.
function renderAsk() {
  const a = state.ask;
  return `
    <div class="ask-wrap">
      <div class="ask-thread" id="ask-thread">${askThreadHTML(a.messages, a.pending)}</div>
      <form class="ask-bar" id="ask-form">
        <input id="ask-input" type="text" autocomplete="off" enterkeyhint="send"
               placeholder="Ask about your money…" value="${esc(a.prefill || "")}"
               ${a.pending ? "disabled" : ""}>
        <button class="btn primary" type="submit" ${a.pending ? "disabled" : ""}>Ask</button>
      </form>
      <p class="ask-note">It can answer questions, tag deposits, and keep your pantry list — other changes still happen in the app.</p>`;
}

async function askSend(text) {
  text = (text || "").trim();
  if (!text || state.ask.pending) return;
  // History is the turns BEFORE this question (client-held, send-and-wait).
  const history = state.ask.messages.map((m) => ({ role: m.role, content: m.content }));
  state.ask.messages.push({ role: "user", content: text });
  state.ask.pending = true;
  render();
  try {
    const res = await api("/api/ask", { method: "POST", body: { message: text, history } });
    // A1: `actions` (one per screen a write touched) drives the tap-through
    // chips. A2: `tools_used` lets the thread suggest topical follow-ups.
    const actions = Array.isArray(res.actions) ? res.actions : [];
    const tools_used = Array.isArray(res.tools_used) ? res.tools_used : [];
    state.ask.messages.push({ role: "assistant", content: res.answer, actions, tools_used });
  } catch (e) {
    state.ask.messages.push({ role: "assistant", content: "Sorry — " + e.message });
  } finally {
    state.ask.pending = false;
    render();
  }
}

/* ================= wiring ================= */

// Resolve a tapped row id against whichever list rendered it — the
// activity feed (_txns), the dashboard recent (_recent), or the
// dashboard payload's own recent (last-resort, no direction).
function findTxn(id) {
  for (const pool of [window._txns, window._recent, window._dash && window._dash.recent]) {
    if (pool) {
      const t = pool.find((x) => x.id === id);
      if (t) return t;
    }
  }
  return null;
}

function wireMain() {
  $("#btn-settle")?.addEventListener("click", openSettle);
  $("#month-prev")?.addEventListener("click", () => shiftMonth(-1));
  $("#month-next")?.addEventListener("click", () => shiftMonth(1));
  $$("[data-filter]").forEach((el) =>
    el.addEventListener("click", () => {
      state.activityFilter = el.dataset.filter;
      render();
    }));
  $("#btn-add-bill")?.addEventListener("click", () => openBillDialog(null));
  $("#btn-add-goal")?.addEventListener("click", openGoalDialog);
  // Home "Spent" rows → the recategorize sheet for that category this month.
  $$("[data-spent-cat]").forEach((el) =>
    el.addEventListener("click", () => openRecatSheet(el.dataset.spentCat)));
  // A4: ask-from-anywhere entry points (Home spend card, Pantry header) →
  // open Ask pre-filled with the keyed question.
  $$("[data-ask]").forEach((el) =>
    el.addEventListener("click", () => askFrom(el.dataset.ask)));
  $$("[data-help]").forEach((el) => el.addEventListener("click", openHelpSheet));
  // Budgets (Analytics Tier C): edit/remove by row index; add via the small form.
  $$("[data-budget-edit]").forEach((el) =>
    el.addEventListener("click", () => editBudget(+el.dataset.budgetEdit)));
  $$("[data-budget-remove]").forEach((el) =>
    el.addEventListener("click", () => removeBudget(+el.dataset.budgetRemove)));
  $("#budget-add")?.addEventListener("submit", (e) => {
    e.preventDefault();
    setBudget($("input[name=category]", e.target).value,
              $("input[name=amount]", e.target).value);
  });
  $$("[data-txn]").forEach((el) =>
    el.addEventListener("click", () => {
      const t = findTxn(+el.dataset.txn);
      if (!t) return;
      // Inflows tag (classify); outflows edit. Inflows never open the
      // spend edit dialog — it has split controls that don't apply to
      // income and could write stray split rows.
      if (t.direction === "in") openClassifyDialog(t);
      else openTxnDialog(t);
    }));
  $$("[data-bill-pay]").forEach((el) =>
    el.addEventListener("click", () => openPayDialog(+el.dataset.billPay)));
  $$("[data-bill-unpay]").forEach((el) =>
    el.addEventListener("click", () => unpayBill(+el.dataset.billUnpay)));
  $$("[data-bill-edit]").forEach((el) =>
    el.addEventListener("click", () =>
      openBillDialog(window._bills.find((b) => b.id === +el.dataset.billEdit))));
  $$("[data-goal-add]").forEach((el) =>
    el.addEventListener("click", () => openContribDialog(+el.dataset.goalAdd)));
  $$("[data-goal-log]").forEach((el) =>
    el.addEventListener("click", () => {
      const id = +el.dataset.goalLog;
      state.openLogs.has(id) ? state.openLogs.delete(id) : state.openLogs.add(id);
      render();
    }));
  // Per-goal what-if: type a $/mo, the months-to-finish readout recomputes in
  // place (a re-render would drop focus mid-typing). Client-side only — the
  // typed rate is never stored anywhere.
  $$("[data-goal-whatif]").forEach((el) =>
    el.addEventListener("input", () => {
      const p = (window._goalPace || {})[+el.dataset.goalWhatif];
      const out = $(`[data-goal-whatif-out="${el.dataset.goalWhatif}"]`);
      if (!p || !out) return;
      const dollars = parseFloat(el.value);
      const cents = el.value !== "" && Number.isFinite(dollars)
        ? Math.round(dollars * 100) : null;
      out.textContent = goalWhatIfText(p.remaining.cents, cents, thisMonthISO());
    }));
  $$("[data-goal-del]").forEach((el) =>
    el.addEventListener("click", async () => {
      if (!confirm("Delete this goal and its contribution log?")) return;
      await api(`/api/goals/${el.dataset.goalDel}`, { method: "DELETE" });
      render();
    }));

  // Inventory: tap a status chip to cycle it; check items off the shopping
  // list ("Got it" = bought = stocked); remove a staple; quick-add either kind.
  $$("[data-item-cycle]").forEach((el) =>
    el.addEventListener("click", () =>
      setItemStatus(+el.dataset.itemCycle, NEXT_STATUS[el.dataset.status] || "stocked")));
  $$("[data-item-got]").forEach((el) =>
    el.addEventListener("click", () => setItemStatus(+el.dataset.itemGot, "stocked")));
  // #015 'ordered': bought online, not here yet. Ordered leaves the list;
  // Arrived = stocked (a one-off then archives as bought); Didn't come = out.
  $$("[data-item-ordered]").forEach((el) =>
    el.addEventListener("click", () => setItemStatus(+el.dataset.itemOrdered, "ordered")));
  $$("[data-item-arrived]").forEach((el) =>
    el.addEventListener("click", () => setItemStatus(+el.dataset.itemArrived, "stocked")));
  $$("[data-item-missed]").forEach((el) =>
    el.addEventListener("click", () => setItemStatus(+el.dataset.itemMissed, "out")));
  // "Got everything": one restock_items call marks the whole list bought
  // (all-or-nothing server-side; per-row "Got it" stays for partial trips).
  $("#inv-got-all")?.addEventListener("click", async (e) => {
    const ids = e.currentTarget.dataset.gotAll.split(",").map(Number);
    if (!confirm(`Mark all ${ids.length} as bought?`)) return;
    await api("/api/inventory/restock", { method: "POST", body: { item_ids: ids } });
    render();
  });
  // Restock nudge: "Yes, restocked" marks the staple stocked (drops it off the
  // shopping list). Same effect as tapping its chip to stocked.
  $$("[data-restock-confirm]").forEach((el) =>
    el.addEventListener("click", () => setItemStatus(+el.dataset.restockConfirm, "stocked")));
  // Trip closure: "Yes, restocked all N" → one restock_items batch for the
  // items that purchase plausibly covered (the same verb as "Got everything").
  $$("[data-restock-all]").forEach((el) =>
    el.addEventListener("click", async () => {
      const ids = el.dataset.restockAll.split(",").map(Number).filter(Boolean);
      if (!ids.length) return;
      el.disabled = true;
      await api("/api/inventory/restock", { method: "POST", body: { item_ids: ids } });
      render();
    }));
  // Forecast "Mark low": an overdue stocked staple is probably low now — one tap
  // flips it to low, dropping it into "Need to buy". Same verb as the chip cycle.
  $$("[data-mark-low]").forEach((el) =>
    el.addEventListener("click", () => setItemStatus(+el.dataset.markLow, "low")));
  $$("[data-item-match]").forEach((el) =>
    el.addEventListener("click", () => setItemMatch(+el.dataset.itemMatch)));
  $$("[data-item-interval]").forEach((el) =>
    el.addEventListener("click", () => setItemInterval(+el.dataset.itemInterval)));
  // #014 metadata setters (store / deadline / snooze) + one-tap wake.
  $$("[data-item-store]").forEach((el) =>
    el.addEventListener("click", () => setItemStore(+el.dataset.itemStore)));
  $$("[data-item-needby]").forEach((el) =>
    el.addEventListener("click", () => setItemNeedBy(+el.dataset.itemNeedby)));
  $$("[data-item-snooze]").forEach((el) =>
    el.addEventListener("click", () => setItemSnooze(+el.dataset.itemSnooze)));
  $$("[data-item-wake]").forEach((el) =>
    el.addEventListener("click", async () => {
      await api(`/api/inventory/${el.dataset.itemWake}`,
                { method: "PUT", body: { snoozed_until: "" } });
      render();
    }));
  // New-staple suggestion: "Track" starts tracking that merchant as a staple.
  $$("[data-track-staple]").forEach((el) =>
    el.addEventListener("click", () => trackSuggestedStaple(+el.dataset.trackStaple)));
  $$("[data-item-remove]").forEach((el) =>
    el.addEventListener("click", async () => {
      if (!confirm("Stop tracking this item? Its history stays in the log.")) return;
      await api(`/api/inventory/${el.dataset.itemRemove}`, { method: "DELETE" });
      render();
    }));
  // (An input named "name" is shadowed by form.name, so read it directly.)
  $("#inv-add-staple")?.addEventListener("submit", (e) => {
    e.preventDefault();
    addItem($("input", e.target).value, "staple");
  });
  $("#inv-add-oneoff")?.addEventListener("submit", (e) => {
    e.preventDefault();
    addItem($("input", e.target).value, "oneoff");
  });

  // Ask tab: submit the question, or send an example; then keep the thread
  // scrolled to the latest and the input focused for the next question.
  $("#ask-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    const inp = $("#ask-input");
    const text = inp.value;
    inp.value = "";
    askSend(text);
  });
  $$("[data-ask-eg]").forEach((el) =>
    el.addEventListener("click", () => askSend(el.dataset.askEg)));
  // A1: a reply's tap-through chip jumps to the screen a write touched, so the
  // person can see or adjust the change (the screen is itself the way back).
  $$("[data-ask-nav]").forEach((el) =>
    el.addEventListener("click", () => setTab(el.dataset.askNav)));
  // A4: a seeded prefill (from an ask-from-anywhere entry point) lands in the
  // box — focus it and put the cursor at the end so it's ready to send or edit,
  // then clear the seed so a later re-render won't clobber what was typed.
  if (state.tab === "ask" && state.ask.prefill) {
    const seeded = $("#ask-input");
    if (seeded) {
      seeded.focus();
      seeded.setSelectionRange(seeded.value.length, seeded.value.length);
    }
    state.ask.prefill = "";
  }
  const thread = $("#ask-thread");
  if (thread) thread.scrollTop = thread.scrollHeight;
  // Auto-focus the Ask input only once a conversation exists (keeps the
  // keyboard up for follow-ups). On the EMPTY Ask tab we deliberately don't —
  // auto-focusing there pops the iOS keyboard and hides the example chips.
  if (state.tab === "ask" && !state.ask.pending && state.ask.messages.length)
    $("#ask-input")?.focus();
}

function shiftMonth(delta) {
  const [y, m] = state.month.split("-").map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  state.month = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  render();
}

async function loadCategories() {
  const cats = await api("/api/categories");
  $("#category-list").innerHTML = cats.map((c) => `<option value="${esc(c)}">`).join("");
}

/* ---------- segmented paid-by control ---------- */
function buildSeg(holder, selectedId, onPick) {
  holder.innerHTML = "";
  state.users.forEach((u, idx) => {
    const b = document.createElement("button");
    b.type = "button";
    b.style.setProperty("--pcolor", idx === 0 ? "var(--p1)" : "var(--p2)");
    b.innerHTML = `<span class="dot"></span>${esc(u.display_name)}`;
    b.classList.toggle("on", u.id === selectedId);
    b.addEventListener("click", () => {
      holder.dataset.value = u.id;
      $$("button", holder).forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      if (onPick) onPick(u.id);
    });
    holder.appendChild(b);
  });
  holder.dataset.value = selectedId;
}

/* ---------- transaction dialog ---------- */
const dlgTxn = $("#dlg-txn");
const formTxn = $("#form-txn");

function updateSplitHint() {
  const pct = +formTxn.payer_share_pct.value;
  const payerId = +$("#txn-paidby").dataset.value;
  const payer = window.Render.userById(state.users, payerId);
  const other = state.users.find((u) => u.id !== payerId) || { display_name: "Partner" };
  $("#split-readout").textContent = `${pct}%`;
  $("#split-hint").textContent =
    `${payer.display_name} covers ${pct}% · ${other.display_name} covers ${100 - pct}%`;
}

function openTxnDialog(txn) {
  state.editingTxn = txn || null;
  $("#txn-title").textContent = txn ? "Edit expense" : "Add expense";
  $("#btn-txn-delete").classList.toggle("hidden", !txn);
  $("#txn-error").textContent = "";
  formTxn.date.value = txn ? txn.date : todayISO();
  formTxn.amount.value = txn ? txn.amount : "";
  formTxn.description.value = txn ? txn.description : "";
  formTxn.category.value = txn ? txn.category : "";
  formTxn.is_shared.checked = txn ? txn.is_shared : true;
  formTxn.payer_share_pct.value = txn ? txn.payer_share_pct : 50;
  buildSeg($("#txn-paidby"), txn ? txn.paid_by : state.meId, updateSplitHint);
  $("#txn-split").classList.toggle("hidden", !formTxn.is_shared.checked);
  updateSplitHint();
  // Transfer toggle: only on an existing row (nothing to flag while adding).
  const xfer = $("#txn-transfer");
  xfer.classList.toggle("hidden", !txn);
  if (txn) {
    xfer.textContent = txn.is_transfer
      ? "✓ Marked as a transfer — tap to undo"
      : "Not spending? Mark as a transfer between your accounts";
    xfer.classList.toggle("on", !!txn.is_transfer);
    xfer.onclick = () => setTransfer(txn.id, !txn.is_transfer);
  }
  dlgTxn.showModal();
  // showModal() auto-focuses the first field (the date input), and iOS pops its
  // date picker on focus — so the calendar appeared the instant you tapped Add.
  // Drop that focus; the user opens the picker by tapping the date field itself.
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
}

formTxn.is_shared.addEventListener("change", () =>
  $("#txn-split").classList.toggle("hidden", !formTxn.is_shared.checked));
formTxn.payer_share_pct.addEventListener("input", updateSplitHint);

formTxn.addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  $("#txn-error").textContent = "";
  const body = {
    date: formTxn.date.value,
    amount: formTxn.amount.value,
    description: formTxn.description.value,
    category: formTxn.category.value || "Other",
    paid_by: +$("#txn-paidby").dataset.value,
    is_shared: formTxn.is_shared.checked,
    payer_share_pct: +formTxn.payer_share_pct.value,
  };
  try {
    if (state.editingTxn) {
      await api(`/api/transactions/${state.editingTxn.id}`, { method: "PUT", body });
    } else {
      await api("/api/transactions", { method: "POST", body });
    }
    dlgTxn.close();
    render();
  } catch (e) {
    $("#txn-error").textContent = e.message;
  }
});

$("#btn-txn-delete").addEventListener("click", async () => {
  if (!state.editingTxn || !confirm("Delete this transaction?")) return;
  await api(`/api/transactions/${state.editingTxn.id}`, { method: "DELETE" });
  dlgTxn.close();
  render();
});

$("#fab").addEventListener("click", () => openTxnDialog(null));

/* ---------- classify (income tagging) dialog ---------- */
const dlgClassify = $("#dlg-classify");
// The six real income types a row can be tagged as ('unclassified' is a
// state, never a target); order matches how often you'd reach for them.
// Real income types only. "Transfer" is deliberately NOT here: a transfer
// between your own accounts is neither income nor spend, so it's set by the
// is_transfer flag (the "Mark as transfer" toggle below), not classified as a
// kind of income. Legacy rows tagged income_type='transfer' still display.
const INCOME_TYPES_UI = [
  ["paycheck", "Paycheck"], ["reimbursement", "Reimbursement"],
  ["refund", "Refund"], ["gift", "Gift"], ["other", "Other"],
];

function openClassifyDialog(txn) {
  $("#classify-error").textContent = "";
  $("#classify-summary").innerHTML =
    `<span class="grow">${esc(txn.description)}</span>` +
    `<span class="amount income-in">+${fmt(txn.amount)}</span>`;
  const holder = $("#classify-types");
  holder.innerHTML = "";
  INCOME_TYPES_UI.forEach(([val, label]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "type-btn" + (txn.income_type === val ? " on" : "");
    b.textContent = label;
    b.addEventListener("click", () => classifyInflow(txn.id, val));
    holder.appendChild(b);
  });
  // The transfer toggle: a transfer isn't income, so it lives apart from the
  // type buttons. Marking removes it from income and the tag queue; unmarking
  // restores it (fully reversible — see set_transfer).
  const xfer = $("#classify-transfer");
  xfer.textContent = txn.is_transfer
    ? "✓ Marked as a transfer — tap to undo"
    : "Not income? Mark as a transfer between your accounts";
  xfer.classList.toggle("on", !!txn.is_transfer);
  xfer.onclick = () => setTransfer(txn.id, !txn.is_transfer);
  dlgClassify.showModal();
}

async function classifyInflow(id, income_type) {
  try {
    const res = await api(`/api/transactions/${id}/classify`,
                          { method: "PUT", body: { income_type } });
    dlgClassify.close();
    render();
    // The backend offers a rule once, on the 2nd inflow of a given type
    // (null otherwise). Chain the offer on top of the re-render.
    if (res && res.rule_suggestion) openRuleDialog(res.rule_suggestion);
  } catch (e) {
    $("#classify-error").textContent = e.message;
  }
}

// Mark (or unmark) a transaction as a transfer between the household's own
// accounts — the fix for a mis-signed "Payment Thank You" (excluded from
// income, spend, and the balance). Closes whichever dialog is open, re-renders.
async function setTransfer(id, isTransfer) {
  try {
    const res = await api(`/api/transactions/${id}/transfer`,
                          { method: "PUT", body: { is_transfer: isTransfer } });
    dlgClassify.close();
    dlgTxn.close();
    render();
    // After the 2nd transfer of a recurring kind, the backend offers a rule
    // ("always treat matching transactions as transfers?") — chain it on top
    // of the re-render, same as classify does.
    if (res && res.rule_suggestion) openRuleDialog(res.rule_suggestion);
  } catch (e) {
    ($("#classify-error") || {}).textContent = e.message;
    ($("#txn-error") || {}).textContent = e.message;
  }
}

/* ---------- "make this a rule?" dialog ---------- */
const dlgRule = $("#dlg-rule");
const formRule = $("#form-rule");

function openRuleDialog(suggestion) {
  state.ruleSetType = suggestion.set_type;
  // A transfer rule marks matches as transfers (is_transfer), not as an income
  // type — carried through to the POST body and reflected in the prompt copy.
  state.ruleSetTransfer = !!suggestion.set_transfer;
  $("#rule-error").textContent = "";
  $("#rule-prompt").textContent = state.ruleSetTransfer
    ? transferRuleText() : ruleSuggestionText(suggestion.set_type);
  $("#rule-hint").textContent =
    "Trim to a stable part of the description — the source's name, not a date " +
    "or amount that changes each time.";
  formRule.match.value = suggestion.match_desc || "";
  dlgRule.showModal();
}

formRule.addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  const match_desc = formRule.match.value.trim();
  if (!match_desc) {
    $("#rule-error").textContent = "Enter some text to match, or tap Not now.";
    return;
  }
  try {
    await api("/api/income/rules",
              { method: "POST", body: {
                set_type: state.ruleSetType, match_desc,
                set_transfer: state.ruleSetTransfer } });
    dlgRule.close();
    render();
  } catch (e) {
    $("#rule-error").textContent = e.message;
  }
});

/* ---------- bill dialogs ---------- */
const dlgBill = $("#dlg-bill");
const formBill = $("#form-bill");

function openBillDialog(bill) {
  state.editingBill = bill || null;
  $("#bill-title").textContent = bill ? "Edit bill" : "Add bill";
  $("#btn-bill-delete").classList.toggle("hidden", !bill);
  $("#bill-error").textContent = "";
  formBill.name.value = bill ? bill.name : "";
  formBill.amount.value = bill ? bill.amount : "";
  formBill.due_day.value = bill ? bill.due_day : "";
  formBill.category.value = bill ? bill.category : "";
  dlgBill.showModal();
}

formBill.addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  const body = {
    name: formBill.name.value,
    amount: formBill.amount.value,
    due_day: +formBill.due_day.value,
    category: formBill.category.value || "Bills",
  };
  try {
    if (state.editingBill) {
      await api(`/api/bills/${state.editingBill.id}`, { method: "PUT", body });
    } else {
      await api("/api/bills", { method: "POST", body });
    }
    dlgBill.close();
    render();
  } catch (e) {
    $("#bill-error").textContent = e.message;
  }
});

$("#btn-bill-delete").addEventListener("click", async () => {
  if (!state.editingBill || !confirm("Remove this bill? Past payments stay in Activity.")) return;
  await api(`/api/bills/${state.editingBill.id}`, { method: "DELETE" });
  dlgBill.close();
  render();
});

const dlgPay = $("#dlg-pay");
const formPay = $("#form-pay");

function openPayDialog(billId) {
  const bill = window._bills.find((b) => b.id === billId);
  state.payingBill = bill;
  $("#pay-error").textContent = "";
  $("#pay-summary").textContent =
    `${bill.name} — ${fmt(bill.amount)} for ${monthName(bill.period)}. This also logs a transaction.`;
  formPay.is_shared.checked = true;
  buildSeg($("#pay-paidby"), state.meId);
  dlgPay.showModal();
}

formPay.addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  try {
    await api(`/api/bills/${state.payingBill.id}/pay`, {
      method: "POST",
      body: {
        paid_by: +$("#pay-paidby").dataset.value,
        is_shared: formPay.is_shared.checked,
        payer_share_pct: 50,
      },
    });
    dlgPay.close();
    render();
  } catch (e) {
    $("#pay-error").textContent = e.message;
  }
});

async function unpayBill(billId) {
  if (!confirm("Undo this payment? The logged transaction is removed too.")) return;
  await api(`/api/bills/${billId}/pay`, { method: "DELETE" });
  render();
}

/* ---------- goal dialogs ---------- */
const dlgGoal = $("#dlg-goal");
const formGoal = $("#form-goal");

function openGoalDialog() {
  $("#goal-error").textContent = "";
  formGoal.reset();
  dlgGoal.showModal();
}

formGoal.addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  try {
    await api("/api/goals", {
      method: "POST",
      body: {
        name: formGoal.name.value,
        target: formGoal.target.value,
        target_date: formGoal.target_date.value || null,
      },
    });
    dlgGoal.close();
    render();
  } catch (e) {
    $("#goal-error").textContent = e.message;
  }
});

const dlgContrib = $("#dlg-contrib");
const formContrib = $("#form-contrib");

function openContribDialog(goalId) {
  state.contribGoal = goalId;
  const g = window._goals.find((x) => x.id === goalId);
  $("#contrib-title").textContent = `Add to “${g.name}”`;
  $("#contrib-error").textContent = "";
  formContrib.reset();
  dlgContrib.showModal();
}

formContrib.addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  try {
    await api(`/api/goals/${state.contribGoal}/contribute`, {
      method: "POST",
      body: { amount: formContrib.amount.value, note: formContrib.note.value },
    });
    dlgContrib.close();
    render();
  } catch (e) {
    $("#contrib-error").textContent = e.message;
  }
});

/* ---------- settle up ---------- */
const dlgSettle = $("#dlg-settle");

async function openSettle() {
  const bal = window._dash.balance;
  if (bal.settled) return;
  $("#settle-summary").textContent =
    `${bal.owes.name} pays ${bal.owed.name} ${fmt(bal.amount)}.`;
  $("#settle-error").textContent = "";
  // The breakdown (why it's this amount) loads async; a failure to fetch it
  // must never block recording the settlement, so it's best-effort.
  $("#settle-breakdown").innerHTML = `<p class="settle-sub">Loading breakdown…</p>`;
  dlgSettle.showModal();
  try {
    const bd = await api("/api/settle/breakdown");
    $("#settle-breakdown").innerHTML = settleBreakdownHTML(bd, state.meId);
  } catch (e) {
    $("#settle-breakdown").innerHTML = "";   // silent — settling still works
  }
}

// A4: from the settle dialog, "Ask about this" hands off to the assistant
// (closing the modal) pre-filled with the balance question — a conversational
// path alongside the static breakdown already shown above.
$("#settle-ask")?.addEventListener("click", () => askFrom("balance"));

$("#form-settle").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  const bal = window._dash.balance;
  try {
    // Paid by the ower with payer share 0% — the full amount credits the other
    // person, which exactly offsets the outstanding balance.
    await api("/api/transactions", {
      method: "POST",
      body: {
        date: todayISO(),
        amount: bal.amount,
        description: `Settlement — ${bal.owes.name} → ${bal.owed.name}`,
        category: "Settlement",
        paid_by: bal.owes.id,
        is_shared: true,
        payer_share_pct: 0,
        source: "settlement",
      },
    });
    dlgSettle.close();
    render();
  } catch (e) {
    $("#settle-error").textContent = e.message;
  }
});

// Keep the greeting's time-of-day bucket current for a tab left open across
// noon/6pm — recheck on tab focus (covers the common case instantly) and on
// an interval as a fallback for tabs that never lose visibility. Wired once,
// unconditionally, at top level (not inside showApp(), which can re-run on
// a logout/login cycle and would otherwise stack duplicate timers).
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) renderHeader();
});
setInterval(renderHeader, 5 * 60 * 1000);

boot();
