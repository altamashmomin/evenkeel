/* Ledger — pure presentation helpers, no DOM, no globals, no build step.
   Split out of app.js so they can be unit-tested in plain node (see
   tests/test_render.js). Loaded before app.js in index.html; app.js pulls
   these off window.Render. Everything here is a pure function of its
   arguments — same input, same string out — which is the whole point. */
"use strict";
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;  // node (tests)
  else root.Render = api;                                                  // browser (app.js)
})(typeof self !== "undefined" ? self : globalThis, function () {

  // US currency, two decimals. A negative renders with the minus BEFORE the
  // symbol (−$353.51), not after ($-353.51) — matching money_display on the
  // server and the − already used in the income card. Reachable since a
  // month's Spent can go negative when a refund exceeds that month's spend.
  const fmt = (n) => {
    const v = Number(n);
    const body = "$" + Math.abs(v).toLocaleString(
      "en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return v < 0 ? "−" + body : body;
  };

  // Escape &, <, > for safe interpolation into HTML text — the exact
  // subset the old document.createElement/textContent trick produced, now
  // a pure string function so it runs in node too.
  function esc(s) {
    return (s == null ? "" : String(s))
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function ord(n) {
    const s = ["th", "st", "nd", "rd"], v = n % 100;
    return n + (s[(v - 20) % 10] || s[v] || s[0]);
  }

  function monthName(ym) {
    const [y, m] = ym.split("-").map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString("en-US", { month: "long", year: "numeric" });
  }

  // The tag-me nudge, subject-verb correct for count. Shared by the
  // dashboard income card and the Activity feed banner so the grammar
  // lives (and is tested) in exactly one place.
  function nudgeText(n) {
    return `${n} inflow${n === 1 ? " still needs" : "s still need"} tagging`;
  }

  // The "make this a rule?" prompt line, shown after the 2nd same-type
  // tag. setType is a real income type ('paycheck', 'gift', …), capitalized
  // for display — all six types are single words that capitalize cleanly.
  // Pure + tested so the one wording lives in exactly one place.
  function ruleSuggestionText(setType) {
    const label = setType.charAt(0).toUpperCase() + setType.slice(1);
    return `You've tagged two as ${label}. Auto-tag future income that matches?`;
  }

  // A small emoji for a spending category or bill/merchant name — the icon in
  // each list row's tile. Keyword-matched on the lowercased text; 💳 as the
  // catch-all. (Income rows use 💵, set at the call site.)
  const CAT_EMOJI = [
    [/grocer|market|whole foods|trader/, "🛒"],
    [/restaur|dining|dinner|lunch|takeout|food|pizza|burger/, "🍽️"],
    [/coffee|cafe|starbucks|espresso|tea/, "☕"],
    [/gas|fuel|uber|lyft|transit|transport|parking|\bcar\b|auto/, "🚗"],
    [/rent|mortgage|\bhome\b|housing|hoa/, "🏠"],
    [/electric|power|utilit|energy/, "⚡"],
    [/internet|wifi|\bphone\b|mobile|cable/, "🌐"],
    [/water/, "💧"],
    [/health|pharmac|doctor|medical|dental|gym|fitness/, "💊"],
    [/entertain|movie|stream|netflix|spotify|music|game/, "🎬"],
    [/travel|flight|hotel|airbnb|\btrip\b/, "✈️"],
    [/shop|amazon|cloth|store|retail/, "🛍️"],
    [/insur/, "🛡️"],
    [/subscrib|membership/, "🔁"],
    // household staples — so pantry rows get a real icon, not the card fallback
    [/paper|towel|napkin|tissue|toilet/, "🧻"],
    [/soap|detergent|cleaner|cleaning|dish/, "🧴"],
    [/trash|garbage|\bbag\b/, "🗑️"],
    [/toothpaste|shampoo|toiletr|razor|deodorant/, "🧼"],
  ];
  function catEmoji(s) {
    const t = (s || "").toLowerCase();
    for (const [re, e] of CAT_EMOJI) if (re.test(t)) return e;
    return "💳";
  }
  // Inventory rows want a homey fallback (a basket), not the money card glyph.
  function itemIcon(it) {
    const e = catEmoji(it.category || it.name);
    return e === "💳" ? "🧺" : e;
  }

  // The "vs last month" pill for the Spent card, from an income-trend series
  // (…, prev, current). Returns {dir, text} or null when there's no usable
  // baseline (fewer than 2 months, or last month had no spend). Spending MORE
  // is "up" (bad → clay); spending LESS is "down" (good → green).
  function vsLastMonth(series) {
    if (!series || series.length < 2) return null;
    const cur = series[series.length - 1];
    const prev = series[series.length - 2];
    if (!prev || prev.month_spend <= 0) return null;
    const pct = Math.round(((cur.month_spend - prev.month_spend) / prev.month_spend) * 100);
    if (pct === 0) return { dir: "flat", text: `— vs ${shortMonth(prev.month)}` };
    const dir = pct > 0 ? "up" : "down";
    return { dir, text: `${pct > 0 ? "▲" : "▼"} ${Math.abs(pct)}% vs ${shortMonth(prev.month)}` };
  }

  function incomeCardHTML(inc, month) {
    // No inflows yet (the state until sync imports income, or an empty
    // month): a muted empty state, not a wall of zeros — same grammar as the
    // bills/goals/recent cards.
    if (inc.gross_inflows === 0) {
      return `
      <div class="card">
        <p class="eyebrow">Income in ${monthName(month)}</p>
        <p class="empty">No income recorded this month.</p>
      </div>`;
    }
    const net = inc.net_cash_flow;
    const netCls = net >= 0 ? "pos" : "neg";
    const netStr = (net >= 0 ? "+" : "−") + fmt(Math.abs(net));
    // savings_rate is a ratio or null (no paycheck income to divide by). The
    // ring fills 0..100% of the rate; a negative rate shows red, empty ring.
    const hasRate = inc.savings_rate != null;
    const ratePct = hasRate ? Math.round(inc.savings_rate * 100) : 0;
    const rate = hasRate ? ratePct + "%" : "—";
    const ringFill = Math.max(0, Math.min(100, ratePct));
    const ringCls = hasRate && ratePct < 0 ? "neg" : "";
    // Total-in row only when gross differs from true income (i.e. there's
    // non-paycheck money in — refunds, gifts — worth distinguishing).
    const grossRow = inc.gross_inflows !== inc.true_income
      ? `<div class="income-sub">
         <span>Total money in</span>
         <span class="amount">${fmt(inc.gross_inflows)}</span>
       </div>`
      : "";
    const n = inc.unclassified_count;
    const nudge = n > 0 ? `<p class="income-nudge">${nudgeText(n)}</p>` : "";
    return `
    <div class="card">
      <p class="eyebrow">Income in ${monthName(month)}</p>
      <div class="income">
        <div class="figs">
          <p class="income-amt">${fmt(inc.true_income)}</p>
          <p class="income-label">earned · net <b class="${netCls}">${netStr}</b></p>
        </div>
        <div class="ring ${ringCls}" style="--p:${ringFill}"><span><b>${rate}</b>saved</span></div>
      </div>
      ${grossRow}
      ${nudge}
    </div>`;
  }

  // Short month label for a chart axis: "2026-07" -> "Jul".
  function shortMonth(ym) {
    const [y, m] = ym.split("-").map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString("en-US", { month: "short" });
  }

  // Window aggregate for the income-vs-spend chart headline. income sums the
  // paycheck income, spend sums NET spend (refund-netted, so it can dip),
  // saved = income - spend; rate is null when there's no income to divide by.
  function trendSummary(series) {
    const income = series.reduce((s, e) => s + Math.max(0, e.true_income), 0);
    const spend = series.reduce((s, e) => s + e.month_spend, 0);
    const saved = income - spend;
    return { income, spend, saved, rate: income > 0 ? saved / income : null };
  }

  // Pure geometry for the income-vs-spend chart. Given the trend series
  // (dollars, from /api/income/trend) and pixel dims, returns the y-domain
  // max and one stacked bar per month in SVG coordinates (y grows downward):
  //   spent = min(income, spend)      — neutral base, the money that went out
  //   saved = max(0, income - spend)  — green cap, the shaded gap you kept
  //   over  = max(0, spend - income)  — red cap above the income line (deficit)
  // The three always tile to max(income, spend), so every sign case (surplus,
  // deficit, and a refund month whose net spend is negative -> all-saved)
  // renders correctly. Kept separate from the SVG string so the scaling math
  // is unit-tested directly.
  function trendBars(series, dims) {
    const { w, h, padL, padR, padT, padB } = dims;
    const plotB = h - padB, plotH = plotB - padT, plotL = padL;
    const plotW = w - padL - padR;
    const n = series.length;
    const max = Math.max(1, ...series.map((e) =>
      Math.max(0, e.true_income, e.month_spend)));
    const band = n ? plotW / n : plotW;
    const barW = band * 0.54;
    const scale = (v) => (Math.max(0, v) / max) * plotH;
    const bars = series.map((e, i) => {
      const income = Math.max(0, e.true_income);
      const spend = Math.max(0, e.month_spend);
      const baseH = scale(Math.min(income, spend));
      const savedH = scale(Math.max(0, income - spend));
      const overH = scale(Math.max(0, spend - income));
      const x = plotL + band * i + (band - barW) / 2;
      return {
        month: e.month, x, w: barW,
        spent: { y: plotB - baseH, h: baseH },
        saved: { y: plotB - baseH - savedH, h: savedH },
        over: { y: plotB - baseH - overH, h: overH },
      };
    });
    return { max, baselineY: plotB, bars };
  }

  function incomeTrendChartHTML(series, dims) {
    const hasData = series.some((e) => e.true_income > 0 || e.month_spend !== 0);
    if (!series.length || !hasData) {
      return `
      <div class="card">
        <p class="eyebrow">Income vs. spending</p>
        <p class="empty">No income or spending in this range yet.</p>
      </div>`;
    }
    const W = 320, H = 176;
    const d = dims || { w: W, h: H, padL: 6, padR: 6, padT: 16, padB: 20 };
    const { max, baselineY, bars } = trendBars(series, d);
    const r = (v) => Math.round(v * 10) / 10;
    const rect = (seg, x, bw, cls) => seg.h > 0.5
      ? `<rect class="${cls}" x="${r(x)}" y="${r(seg.y)}" width="${r(bw)}" height="${r(seg.h)}" rx="1.5"/>`
      : "";
    const barsSvg = bars.map((b) =>
      rect(b.spent, b.x, b.w, "bar-spent") +
      rect(b.saved, b.x, b.w, "bar-saved") +
      rect(b.over, b.x, b.w, "bar-over")).join("");
    const labels = bars.map((b) =>
      `<text class="chart-x" x="${r(b.x + b.w / 2)}" y="${d.h - 6}" text-anchor="middle">${shortMonth(b.month)}</text>`
    ).join("");
    const svg = `
      <svg class="chart-svg" viewBox="0 0 ${d.w} ${d.h}" role="img"
           aria-label="Income versus spending by month">
        <line class="chart-grid" x1="${d.padL}" y1="${r(baselineY)}" x2="${d.w - d.padR}" y2="${r(baselineY)}"/>
        <text class="chart-axis" x="${d.padL}" y="11">${fmt(max)}</text>
        ${barsSvg}
        ${labels}
      </svg>`;
    const sum = trendSummary(series);
    const rate = sum.rate == null ? "—" : Math.round(sum.rate * 100) + "%";
    const rateCls = sum.saved >= 0 ? "pos" : "neg";
    return `
    <div class="card chart-card">
      <p class="eyebrow">Income vs. spending — last ${series.length} months</p>
      <p class="chart-headline"><span class="${rateCls}">${rate}</span> saved</p>
      <p class="chart-sub">kept ${fmt(sum.saved)} of ${fmt(sum.income)} earned</p>
      ${svg}
      <div class="chart-legend">
        <span><i class="sw sw-saved"></i>Saved</span>
        <span><i class="sw sw-spent"></i>Spent</span>
      </div>
    </div>`;
  }

  /* ===== analytics tab (Tier A reads) =====
     Each is a pure function of one endpoint's JSON. The composition, member
     and bill endpoints speak the {cents, display} money shape (`amt` below);
     income/category trends predate it and speak plain dollars (`fmt`). */

  const amt = (m) => fmt((m && m.cents != null ? m.cents : 0) / 100);
  // Signed dollars from cents, with an explicit +/− and the minus before $.
  const signedCents = (c) => (c > 0 ? "+" : c < 0 ? "−" : "") + fmt(Math.abs(c) / 100);

  // Spending composition (#10): category mix (reusing the dashboard's cat-bar)
  // plus the top-merchants list. Net of refunds, outflows only.
  function spendingCompositionHTML(comp) {
    const cats = comp.by_category || [];
    const merchants = comp.top_merchants || [];
    const monthLbl = monthName(comp.month);
    if (!cats.length) {
      return `<div class="card"><p class="eyebrow">Where the money went — ${monthLbl}</p>
        <p class="empty">Nothing spent this month.</p></div>`;
    }
    const maxCat = Math.max(1, ...cats.map((c) => Math.abs(c.amount.cents)));
    const catRows = cats.map((c) => `
      <div class="cat-row">
        <span class="cat-name">${esc(c.category)}</span>
        <span class="cat-bar"><i style="width:${(Math.abs(c.amount.cents) / maxCat) * 100}%"></i></span>
        <span class="amt amount">${amt(c.amount)}</span>
      </div>`).join("");
    const merchCard = merchants.length ? `
      <div class="card">
        <p class="eyebrow">Paid the most — ${monthLbl}</p>
        <ul class="list">${merchants.map((m) => `
          <li>
            <div class="grow"><div class="title">${esc(m.description)}</div>
              <div class="sub">${m.count} charge${m.count === 1 ? "" : "s"}</div></div>
            <span class="amt amount">${amt(m.amount)}</span>
          </li>`).join("")}</ul>
      </div>` : "";
    return `
      <div class="card">
        <p class="eyebrow">Where the money went — ${monthLbl}</p>
        <p class="stat-big">${amt(comp.total)}</p>
        <div style="margin-top:12px">${catRows}</div>
      </div>
      ${merchCard}`;
  }

  // Per-member breakdown (#11): paid (fronted) vs owed (fair share) vs net.
  // Nets sum to zero; net colored (up = green, down = red). Shared outflows.
  function memberBreakdownHTML(mb) {
    const members = mb.members || [];
    const any = members.some((m) => m.paid.cents || m.owed.cents);
    if (!members.length || !any) {
      return `<div class="card"><p class="eyebrow">Who paid what — ${monthName(mb.month)}</p>
        <p class="empty">No shared spending this month.</p></div>`;
    }
    const rows = members.map((m) => {
      const net = m.net.cents;
      const cls = net > 0 ? "pos" : net < 0 ? "neg" : "";
      return `<div class="mb-row">
        <span class="mb-name">${esc(m.name)}</span>
        <span class="mb-fig"><b class="amount">${amt(m.paid)}</b><small>paid</small></span>
        <span class="mb-fig"><b class="amount">${amt(m.owed)}</b><small>owed</small></span>
        <span class="mb-fig ${cls}"><b class="amount">${net === 0 ? fmt(0) : signedCents(net)}</b><small>net</small></span>
      </div>`;
    }).join("");
    return `<div class="card">
      <p class="eyebrow">Who paid what — ${monthName(mb.month)}</p>
      ${rows}
      <p class="chart-sub">Net is how much each person is up (+) or down (−) on shared costs this month.</p>
    </div>`;
  }

  // Bill variance (#12): defined vs actual per bill; over = red badge, under =
  // green, unpaid = neutral. Reuses the existing badge palette.
  function billVarianceHTML(bv) {
    const bills = bv.bills || [];
    if (!bills.length) {
      return `<div class="card"><p class="eyebrow">Bills — planned vs actual</p>
        <p class="empty">No bills set up.</p></div>`;
    }
    const rows = bills.map((b) => {
      if (!b.paid || b.actual == null) {
        return `<li>
          <div class="grow"><div class="title">${esc(b.name)}</div>
            <div class="sub">${amt(b.defined)} planned</div></div>
          <span class="badge due">unpaid</span>
        </li>`;
      }
      const v = b.variance.cents;
      const label = v === 0 ? "on budget"
        : (v > 0 ? "+" : "−") + fmt(Math.abs(v) / 100) + (v > 0 ? " over" : " under");
      return `<li>
        <div class="grow"><div class="title">${esc(b.name)}</div>
          <div class="sub">${amt(b.defined)} planned</div></div>
        <span class="badge ${v > 0 ? "overdue" : "paid"}">${label}</span>
        <span class="amt amount">${amt(b.actual)}</span>
      </li>`;
    }).join("");
    return `<div class="card"><p class="eyebrow">Bills — planned vs actual</p>
      <ul class="list">${rows}</ul></div>`;
  }

  /* ===== "Budgets" — category limits vs actual net spend (Analytics Tier C) =====
     Pure function of /api/analytics/budget-status ({ period, budgets:[{category,
     budgeted, actual, remaining, over, pct}], unbudgeted_spend }) + the category
     list (the add form's datalist). Each budget is a progress bar (green under,
     red over — capped at 100% width) with "actual of limit · pct%" and a
     remaining/over badge; ✎ edits the amount, ✕ removes. An "Unbudgeted spend"
     line so nothing hides, and a small set-a-budget form. budget_status is
     category-keyed (no id); the action buttons carry the ARRAY INDEX and the
     handlers read the row out of window._budgetStatus (and resolve category→id
     for remove via window._budgets), so no user content lands in an attribute. */
  function budgetStatusHTML(data, categories) {
    const budgets = (data && data.budgets) || [];
    const opts = (categories || []).map((c) => `<option value="${esc(c)}">`).join("");
    const rows = budgets.map((b, i) => {
      const pct = b.pct == null ? 0 : b.pct;
      const badge = b.over
        ? `<span class="badge overdue">${fmt(Math.abs(b.remaining.cents) / 100)} over</span>`
        : `<span class="badge paid">${amt(b.remaining)} left</span>`;
      return `<li class="cat-row">
        <div class="grow">
          <div class="title">${esc(b.category)}</div>
          <div class="sub">${amt(b.actual)} of ${amt(b.budgeted)}${b.pct == null ? "" : ` · ${b.pct}%`}</div>
          <span class="cat-bar budget-bar"><i class="${b.over ? "over" : ""}" style="width:${Math.min(pct, 100)}%"></i></span>
        </div>
        ${badge}
        <button class="item-x" data-budget-edit="${i}" aria-label="Edit ${esc(b.category)} budget">✎</button>
        <button class="item-x" data-budget-remove="${i}" aria-label="Remove ${esc(b.category)} budget">✕</button>
      </li>`;
    }).join("");
    const unbudgeted = data && data.unbudgeted_spend && data.unbudgeted_spend.cents
      ? `<p class="sub" style="margin:8px 2px 0">Unbudgeted spend: ${amt(data.unbudgeted_spend)}</p>` : "";
    const body = budgets.length
      ? `<ul class="list">${rows}</ul>${unbudgeted}`
      : `<p class="empty">No budgets yet — set a monthly limit for a category.</p>`;
    return `<div class="card"><p class="eyebrow">Budgets</p>${body}
        <form class="inv-add" id="budget-add" autocomplete="off">
          <input name="category" list="budget-cats" maxlength="60" placeholder="Category">
          <input name="amount" type="number" min="0" step="0.01" placeholder="Monthly $">
          <button class="btn small primary" type="submit">Set</button>
        </form>
        <datalist id="budget-cats">${opts}</datalist>
      </div>`;
  }

  // Savings-rate trend (#9): the trailing 3-month rolling rate per month, as a
  // strip of month cells (ratios, not money), with the latest as the headline.
  function savingsRateTrendHTML(data) {
    const series = data.series || [];
    const rated = series.filter((e) => e.rolling_savings_rate != null);
    if (!rated.length) {
      return `<div class="card"><p class="eyebrow">Savings rate</p>
        <p class="empty">Not enough income yet to show a rate.</p></div>`;
    }
    const latest = rated[rated.length - 1].rolling_savings_rate;
    const cells = series.map((e) => {
      const r = e.rolling_savings_rate;
      const cls = r == null ? "" : r >= 0 ? "pos" : "neg";
      return `<div class="sr-cell">
        <span class="sr-pct ${cls}">${r == null ? "—" : Math.round(r * 100) + "%"}</span>
        <span class="sr-mo">${shortMonth(e.month)}</span>
      </div>`;
    }).join("");
    return `<div class="card">
      <p class="eyebrow">Savings rate — rolling 3-month</p>
      <p class="chart-headline"><span class="${latest >= 0 ? "pos" : "neg"}">${Math.round(latest * 100)}%</span></p>
      <p class="chart-sub">Share of paycheck income kept, smoothed over 3 months.</p>
      <div class="sr-strip">${cells}</div>
    </div>`;
  }

  // Category trend (#8): monthly NET spend for one category (dollars, not the
  // {cents} shape), as horizontal bars, with the latest MoM delta. Returns ""
  // when the category had no activity in the window (caller omits the card).
  function categoryTrendHTML(data) {
    const series = data.series || [];
    if (!series.length || !series.some((e) => e.spend !== 0)) return "";
    const max = Math.max(1, ...series.map((e) => Math.abs(e.spend)));
    const rows = series.map((e) => `
      <div class="cat-row">
        <span class="cat-name">${shortMonth(e.month)}</span>
        <span class="cat-bar"><i style="width:${(Math.abs(e.spend) / max) * 100}%"></i></span>
        <span class="amt amount">${fmt(e.spend)}</span>
      </div>`).join("");
    const last = series[series.length - 1];
    const sub = last.mom_delta == null ? ""
      : `<p class="chart-sub">${last.mom_delta > 0 ? "+" : "−"}${fmt(Math.abs(last.mom_delta))} vs the month before</p>`;
    return `<div class="card">
      <p class="eyebrow">${esc(data.category)} — last ${series.length} months</p>
      ${sub}
      <div style="margin-top:12px">${rows}</div>
    </div>`;
  }

  // Cash-flow forecast (#14): projected month-end NET — a conservative floor
  // (no unlanded paycheck assumed). Headline is projected_net (green/red); the
  // sub shows net-so-far and bills still due; then the remaining-bills list.
  function cashFlowForecastHTML(f) {
    const proj = f.projected_net || { cents: 0 };
    const bills = f.bills_remaining || [];
    const billRows = bills.length
      ? `<ul class="list">${bills.map((b) => `
          <li><div class="grow"><div class="title">${esc(b.name)}</div>
            <div class="sub">due day ${b.due_day}</div></div>
            <span class="amt amount">${amt(b.amount)}</span></li>`).join("")}</ul>`
      : `<p class="empty">No bills left to pay this month.</p>`;
    return `<div class="card">
      <p class="eyebrow">Cash flow — ${monthName(f.period)}</p>
      <p class="chart-headline"><span class="${proj.cents >= 0 ? "pos" : "neg"}">${amt(proj)}</span></p>
      <p class="chart-sub">projected month-end floor · ${amt(f.net_so_far)} net so far, ${amt(f.bills_remaining_total)} in bills still due</p>
      ${billRows}
    </div>`;
  }

  // Anomaly flags (#15): categories spending well over their trailing 3-month
  // average this month. Passive heads-up; empty when nothing's unusual.
  function anomaliesHTML(data) {
    const flags = data.anomalies || [];
    if (!flags.length) {
      return `<div class="card"><p class="eyebrow">Spending spikes — ${monthName(data.month)}</p>
        <p class="empty">Nothing unusual this month 🌿</p></div>`;
    }
    const rows = flags.map((a) => `
      <li><div class="grow"><div class="title">${esc(a.category)}</div>
        <div class="sub">vs ${amt(a.baseline)} avg</div></div>
        <span class="badge overdue">${a.pct_over}% over</span>
        <span class="amt amount">${amt(a.current)}</span></li>`).join("");
    return `<div class="card"><p class="eyebrow">Spending spikes — ${monthName(data.month)}</p>
      <ul class="list">${rows}</ul></div>`;
  }

  // Recurring / subscriptions (#13): detected repeat charges (same merchant +
  // amount on a regular cadence). Sparse until a few months of history.
  function recurringChargesHTML(data) {
    const rec = data.recurring || [];
    if (!rec.length) {
      return `<div class="card"><p class="eyebrow">Recurring & subscriptions</p>
        <p class="empty">None detected yet — needs a few months of history.</p></div>`;
    }
    const rows = rec.map((r) => `
      <li><div class="grow"><div class="title">${esc(r.merchant)}</div>
        <div class="sub">${esc(r.cadence)} · next ~${esc(shortDate(r.predicted_next))}</div></div>
        <span class="amt amount">${amt(r.amount)}</span></li>`).join("");
    return `<div class="card"><p class="eyebrow">Recurring & subscriptions</p>
      <ul class="list">${rows}</ul></div>`;
  }

  // Goal pace (#16): each goal's saved/target, a status chip vs its target
  // date, and the projected finish date. Reuses the badge palette.
  const GOAL_STATUS = {
    complete: ["done", "paid"], on_track: ["on track", "paid"],
    behind: ["behind", "overdue"], projected: ["projected", "due"],
    no_pace: ["no pace yet", "due"],
  };
  function goalPaceHTML(data) {
    const goals = data.goals || [];
    if (!goals.length) {
      return `<div class="card"><p class="eyebrow">Goal pace</p>
        <p class="empty">No goals yet.</p></div>`;
    }
    const rows = goals.map((g) => {
      const [label, cls] = GOAL_STATUS[g.status] || ["", "due"];
      const when = g.projected_date ? ` · ~${esc(shortDate(g.projected_date))}` : "";
      return `<li><div class="grow"><div class="title">${esc(g.name)}</div>
        <div class="sub">${amt(g.saved)} of ${amt(g.target)}${when}</div></div>
        <span class="badge ${cls}">${label}</span></li>`;
    }).join("");
    return `<div class="card"><p class="eyebrow">Goal pace</p>
      <ul class="list">${rows}</ul></div>`;
  }

  // The Ask tab's chat thread. Pure function of the client-held messages
  // ([{role:'user'|'assistant', content}]) plus a pending flag. Content is
  // escaped and rendered as plain text (newlines preserved by CSS white-space);
  // an empty thread shows a friendly starter with example questions.
  const ASK_EXAMPLES = ["How are we doing this month?", "Did my paycheck land?",
                        "What do we need from the store?", "Who owes who right now?"];

  function askThreadHTML(messages, pending) {
    if (!messages.length && !pending) {
      return `
      <div class="ask-empty">
        <p class="ask-empty-title">Ask about your money</p>
        <p class="ask-empty-sub">Plain questions, plain answers. It reads the
          same numbers the app shows — and can tag a deposit or keep your
          pantry list when you tell it.</p>
        <div class="ask-examples">
          ${ASK_EXAMPLES.map((q) =>
            `<button type="button" class="ask-eg" data-ask-eg="${esc(q)}">${esc(q)}</button>`
          ).join("")}
        </div>
      </div>`;
    }
    const bubbles = messages.map((m) => {
      // A subtle chip when the assistant actually changed something (tagged an
      // inflow), so a write reads differently from a plain answer.
      const chip = (m.role === "assistant" && m.tagged)
        ? `<span class="ask-tagged">✓ tagged</span>` : "";
      return `<div class="ask-msg ${m.role === "user" ? "ask-you" : "ask-bot"}">${esc(m.content)}${chip}</div>`;
    }).join("");
    const thinking = pending
      ? `<div class="ask-msg ask-bot ask-thinking"><span></span><span></span><span></span></div>`
      : "";
    return bubbles + thinking;
  }

  // A purchase date (ISO "YYYY-MM-DD") as a friendly "Aug 1". Used by the
  // restock-suggestion evidence line.
  function shortDate(iso) {
    const [y, m, d] = (iso || "").split("-").map(Number);
    if (!y) return iso || "";
    return new Date(y, m - 1, d).toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }

  // Whole days between two ISO dates (b − a), calendar-based (parsed at local
  // midnight so DST can't shift the count). Positive when b is later.
  function daysBetween(aISO, bISO) {
    const a = new Date(aISO + "T00:00:00"), b = new Date(bISO + "T00:00:00");
    return Math.round((b - a) / 86400000);
  }

  /* ===== "Coming up" — cadence-based restock forecast (step 5, 2nd half) =====
     Pure function of restock_forecast[] (each { item_id, name, status,
     interval_days, last_purchase, predicted_date }) and the client's today
     (ISO). The derivation is clock-free on purpose — "how soon / overdue" is a
     view concern, computed here against the real date. We surface only STOCKED
     staples due within the horizon or already overdue: low/out staples already
     appear in "Need to buy", so a forecast for them would just double up.
     Overdue (or due-today) rows carry a one-tap "Mark low" — the prediction
     says you're probably low now, so acting on it drops the staple into "Need
     to buy" (set_item_status → low). Future rows stay a heads-up (date badge,
     no action). Always a suggestion the human confirms, never an auto-flip. */
  function restockForecastHTML(forecasts, todayISO, horizonDays) {
    if (!forecasts || !forecasts.length || !todayISO) return "";
    const horizon = horizonDays == null ? 14 : horizonDays;
    const rows = forecasts
      .filter((f) => f.status === "stocked")
      .map((f) => ({ f, days: daysBetween(todayISO, f.predicted_date) }))
      .filter((x) => x.days <= horizon)   // due soon, or overdue (negative)
      .sort((a, b) => a.days - b.days);
    if (!rows.length) return "";
    const plur = (n) => (Math.abs(n) === 1 ? "" : "s");
    const li = rows.map(({ f, days }) => {
      const when = days < 0 ? `overdue by ${-days} day${plur(days)}`
        : days === 0 ? "likely due today"
        : `likely need in ${days} day${plur(days)}`;
      // days <= 0 → overdue or due today: the prediction says it's probably low
      // now, so offer the action. days > 0 → still in the future: informational
      // date badge only.
      const trailing = days <= 0
        ? `<button class="btn small primary" data-mark-low="${f.item_id}">Mark low</button>`
        : `<span class="badge due">${esc(shortDate(f.predicted_date))}</span>`;
      return `<li>
        <span class="ic">${itemIcon({ name: f.name })}</span>
        <div class="grow">
          <div class="title">${esc(f.name)}</div>
          <div class="sub">${when} · about every ${f.interval_days} days</div>
        </div>
        ${trailing}
      </li>`;
    }).join("");
    return `<div class="card forecast-card">
        <p class="eyebrow">Coming up</p>
        <ul class="list">${li}</ul>
      </div>`;
  }

  /* ===== "Bought a lot — track it?" — new-staple suggestions (step 5 sibling)
     Pure function of new_staple_suggestions[] (each { merchant,
     example_description, purchases_seen, first_purchase, last_purchase,
     total_spent: {cents, display}, suggested_match }). Frequently-bought
     merchants not yet tracked — a one-tap offer to START tracking one as a
     staple (the discovery counterpart to the restock cards, which act on
     staples you already track). The button carries the array INDEX; app.js reads
     the row out of window._inv to POST add_item with the merchant name +
     suggested_match (no user content in an attribute — esc doesn't escape
     quotes). Suggest, never auto-add. */
  function newStapleSuggestionsHTML(suggestions) {
    if (!suggestions || !suggestions.length) return "";
    const li = suggestions.map((s, i) => {
      const spent = s.total_spent ? " · " + esc(s.total_spent.display) : "";
      return `<li>
        <span class="ic">${itemIcon({ name: s.merchant })}</span>
        <div class="grow">
          <div class="title">${esc(s.merchant)}</div>
          <div class="sub">Bought ${s.purchases_seen}×${spent} · last ${esc(shortDate(s.last_purchase))}</div>
        </div>
        <button class="btn small primary" data-track-staple="${i}">Track</button>
      </li>`;
    }).join("");
    return `<div class="card suggest-card">
        <p class="eyebrow">Bought a lot — track it?</p>
        <ul class="list">${li}</ul>
      </div>`;
  }

  /* ===== "Check the match?" — broken-match detector (step 5 sibling) =====
     Pure function of unmatched_staples[] (each { item_id, name, restock_match,
     matched_by, tracked_since }) and the client's today. A staple whose match
     phrase has matched NO purchase is silently invisible to every restock
     inference (restock_suggestions / forecast / predicted-low all need a match),
     so we surface it to fix the phrase — but only once it's been tracked at
     least `minDays` (grace), so a just-added staple isn't nagged before there's
     been time to expect a purchase. The derivation is clock-free; "tracked long
     enough" is decided here against the real date (same split the forecast uses;
     no today → no card). Tapping a row opens the same match editor
     (data-item-match) the staples list uses. A review prompt, never an assertion
     the phrase is wrong: some staples are bought inside grocery runs and can't
     match by product (the step-5 merchant-not-product limit). */
  function unmatchedStaplesHTML(unmatched, todayISO, minDays) {
    if (!unmatched || !unmatched.length || !todayISO) return "";
    const grace = minDays == null ? 21 : minDays;
    const rows = unmatched.filter(
      (u) => u.tracked_since && daysBetween(u.tracked_since, todayISO) >= grace);
    if (!rows.length) return "";
    const li = rows.map((u) => {
      const on = u.matched_by === "phrase"
        ? `nothing matched “${esc(u.restock_match || "")}”`
        : "nothing matched its name";
      return `<li>
        <span class="ic">${itemIcon({ name: u.name })}</span>
        <div class="grow">
          <div class="title">${esc(u.name)}</div>
          <div class="sub">${on} · tracked since ${esc(shortDate(u.tracked_since))}</div>
        </div>
        <button class="btn small" data-item-match="${u.item_id}">Fix match</button>
      </li>`;
    }).join("");
    return `<div class="card suggest-card">
        <p class="eyebrow">Check the match?</p>
        <ul class="list">${li}</ul>
      </div>`;
  }

  /* ===== "Still need these?" — list-rot detector (step 5 sibling) =====
     Pure function of stale_shopping_items[] (each { item_id, name, status,
     low_since }) and the client's today. A staple that's been low/out a long
     time with no matching purchase since is either still needed or was bought
     off-feed — a gentle prompt to prune the list. Only surfaces items low/out
     for at least `minDays` (grace), so a just-flagged item isn't nagged; the
     derivation is clock-free, "a while" decided here (no today → no card). The
     "Not anymore" action reuses data-item-remove (archive, stop tracking) — no
     new app.js wiring. Still-need-it? Leave it; it stays on the shopping list. */
  function staleShoppingHTML(items, todayISO, minDays) {
    if (!items || !items.length || !todayISO) return "";
    const grace = minDays == null ? 14 : minDays;
    const plur = (n) => (n === 1 ? "" : "s");
    const rows = items
      .map((s) => ({ s, days: daysBetween(s.low_since, todayISO) }))
      .filter((x) => x.s.low_since && x.days >= grace);
    if (!rows.length) return "";
    const li = rows.map(({ s, days }) => `<li>
        <span class="ic">${itemIcon({ name: s.name })}</span>
        <div class="grow">
          <div class="title">${esc(s.name)}</div>
          <div class="sub">${esc(s.status)} for ${days} day${plur(days)} · no purchase yet</div>
        </div>
        <button class="btn small" data-item-remove="${s.item_id}">Not anymore</button>
      </li>`).join("");
    return `<div class="card suggest-card">
        <p class="eyebrow">Still need these?</p>
        <ul class="list">${li}</ul>
      </div>`;
  }

  /* ===== "What your staples cost" — money tie-in (step 5's named future) =====
     Pure function of staple_spend[] (each { item_id, name, purchases_seen,
     months_spanned, total:{cents,display}, monthly:{cents,display} }, priciest
     first). Surfaces what a tracked staple costs from its matching purchases —
     "~$42/mo on coffee". Read-only insight; no action, no client date needed
     (the derivation already gates on >= 3 purchases). Money strings come from
     the server verbatim ({cents,display}) — the pantry reports money, never
     moves it. Honest limit: merchant-level, so it's the whole coffee shop, not
     one cup, and a grocery-hidden staple shows nothing. */
  function stapleSpendHTML(spend) {
    if (!spend || !spend.length) return "";
    const li = spend.map((s) => {
      const monthly = s.monthly ? esc(s.monthly.display) : "";
      const total = s.total ? esc(s.total.display) : "";
      return `<li>
        <span class="ic">${itemIcon({ name: s.name })}</span>
        <div class="grow">
          <div class="title">${esc(s.name)}</div>
          <div class="sub">${total} over ${s.months_spanned} mo · ${s.purchases_seen}×</div>
        </div>
        <span class="badge due">~${monthly}/mo</span>
      </li>`;
    }).join("");
    return `<div class="card suggest-card">
        <p class="eyebrow">What your staples cost</p>
        <ul class="list">${li}</ul>
      </div>`;
  }

  /* ===== "You shopped — check your list" — post-shopping review nudge =====
     Pure function of last_shopping_trip ({date, merchant, category} | null), the
     shopping list, and the client's today. Leans into the merchant-not-product
     limit: the feed can't say WHAT you bought inside a grocery run, so rather
     than guess which staples you restocked, it prompts a human to review. Shows
     ONLY for a recent trip (within windowDays, default 3) when there's a non-
     empty list to check off — so it clears itself once the list is emptied or the
     trip ages out. No action of its own: it points at the "Need to buy" card
     right below (whose Got-it buttons do the checking off). The derivation is
     clock-free; "recent" is decided here (no today → no nudge). */
  function postShoppingHTML(trip, shopping, todayISO, windowDays) {
    if (!trip || !todayISO || !shopping || !shopping.length) return "";
    const window = windowDays == null ? 3 : windowDays;
    const days = daysBetween(trip.date, todayISO);
    if (days < 0 || days > window) return "";   // future, or aged out
    const when = days === 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
    return `<div class="card restock-card">
        <p class="eyebrow">You shopped ${when}</p>
        <p class="match-hint" style="margin:2px">🛒 ${esc(trip.merchant)} — check off anything you restocked below.</p>
      </div>`;
  }

  /* ===== inventory ("the pantry") — INVENTORY-DESIGN inc 3 + step 5 =====
     Pure function of the /api/inventory JSON:
       { items: [staples, urgent-first], shopping: [staples low/out + oneoffs],
         low_count, restock_suggestions: [{ item_id, name, status, matched_by,
           purchase: { date, description, amount: {cents, display} } }] }
     Three cards: a purchase-feed "Looks like you restocked?" nudge (each a
     one-tap confirm that marks the staple stocked, showing the evidence
     purchase — INVENTORY-DESIGN step 5), a derived "Need to buy" list (check
     items off as bought), and the staples tracker (a tap-to-cycle
     stocked→low→out chip + a 🔎 to set the item's optional purchase-match
     phrase). Each add/track card ends in a quick-add field. Interaction wiring
     lives in app.js; this only builds the markup + its data-* hooks. A staple
     that's low/out shows in both the nudge and shopping cards on purpose —
     they're views over the same items. */
  function inventoryHTML(data, todayISO) {
    const items = data.items || [];
    const shopping = data.shopping || [];
    const suggestions = data.restock_suggestions || [];
    const forecastCard = restockForecastHTML(data.restock_forecast, todayISO);
    const trackCard = newStapleSuggestionsHTML(data.new_staple_suggestions);
    const matchCard = unmatchedStaplesHTML(data.unmatched_staples, todayISO);
    const staleCard = staleShoppingHTML(data.stale_shopping_items, todayISO);
    const spendCard = stapleSpendHTML(data.staple_spend);
    const tripCard = postShoppingHTML(data.last_shopping_trip, shopping, todayISO);

    const restockCard = suggestions.length
      ? `<div class="card restock-card">
          <p class="eyebrow">Looks like you restocked?</p>
          <ul class="list">${suggestions.map((s) => {
            const p = s.purchase || {};
            const amt = p.amount ? esc(p.amount.display) : "";
            return `<li>
              <span class="ic">${itemIcon({ name: s.name })}</span>
              <div class="grow">
                <div class="title">${esc(s.name)}</div>
                <div class="sub">Bought ${esc(p.description || "")} · ${esc(shortDate(p.date))}${amt ? " · " + amt : ""}</div>
              </div>
              <button class="btn small primary" data-restock-confirm="${s.item_id}">Yes, restocked</button>
            </li>`;
          }).join("")}</ul>
        </div>`
      : "";

    const shopRows = shopping.length
      ? `<ul class="list">${shopping.map((it) => {
          const tag = it.kind === "oneoff"
            ? `<span class="badge due">need</span>`
            : `<span class="badge ${it.status === "out" ? "overdue" : "due"}">${esc(it.status)}</span>`;
          const sub = it.note ? esc(it.note) : (it.kind === "oneoff" ? "one-off" : "staple");
          return `<li>
            <span class="ic">${itemIcon(it)}</span>
            <div class="grow">
              <div class="title">${esc(it.name)}</div>
              <div class="sub">${sub}</div>
            </div>
            ${tag}
            <button class="btn small" data-item-got="${it.id}">Got it</button>
          </li>`;
        }).join("")}</ul>`
      : `<p class="empty">Nothing to buy — you're all stocked 🌿</p>`;

    const stapleRows = items.length
      ? `<ul class="list">${items.map((it) => `
          <li>
            <span class="ic">${itemIcon(it)}</span>
            <div class="grow">
              <div class="title">${esc(it.name)}</div>
              ${it.note ? `<div class="sub">${esc(it.note)}</div>` : ""}
              ${it.restock_match ? `<div class="sub match-hint">🔎 matches “${esc(it.restock_match)}”</div>` : ""}
            </div>
            <button class="status-chip ${it.status}" data-item-cycle="${it.id}"
                    data-status="${it.status}"
                    aria-label="${esc(it.name)} is ${it.status}; tap to change">${esc(it.status)}</button>
            <button class="item-x item-match" data-item-match="${it.id}"
                    aria-label="Set a purchase match for ${esc(it.name)}">🔎</button>
            <button class="item-x" data-item-remove="${it.id}"
                    aria-label="Stop tracking ${esc(it.name)}">✕</button>
          </li>`).join("")}</ul>`
      : `<p class="empty">No staples tracked yet. Add the things you'd hate to run out of.</p>`;

    return `
      <div class="section-head">
        <p class="eyebrow" style="margin:0">The pantry</p>
        ${data.low_count > 0 ? `<span class="badge due">${data.low_count} running low</span>` : ""}
      </div>
      ${restockCard}
      ${tripCard}
      <div class="card">
        <p class="eyebrow">Need to buy</p>
        ${shopRows}
        <form class="inv-add" id="inv-add-oneoff" autocomplete="off">
          <input name="name" maxlength="100" placeholder="Add something to buy…">
          <button class="btn small primary" type="submit">Add</button>
        </form>
      </div>
      ${staleCard}
      ${forecastCard}
      <div class="card">
        <p class="eyebrow">Staples</p>
        ${stapleRows}
        <form class="inv-add" id="inv-add-staple" autocomplete="off">
          <input name="name" maxlength="100" placeholder="Track a staple…">
          <button class="btn small primary" type="submit">Add</button>
        </form>
      </div>
      ${spendCard}
      ${trackCard}
      ${matchCard}`;
  }

  // Which "power" a pip signals: green for read-only/advisory, honey for
  // anything that can write, sage for recommend/edit-then-stop. A glanceable
  // encoding of what each agent is allowed to do — real catalog status.
  function agentTone(access) {
    const a = (access || "").toLowerCase();
    if (/write|tag/.test(a)) return "w";
    if (/recommend|edit|advis/.test(a)) return "a";
    return "r";
  }

  // A chip that explains itself: the term is glossed on hover (desktop) /
  // long-press (mobile) via title, and the Key card lists them all for touch.
  function agentChip(val, gloss, cls) {
    if (!val) return "";
    const g = gloss[val];
    const t = g ? ` title="${esc(g)}"` : "";
    const explains = g ? " explains" : "";
    return `<span class="agent-chip${cls ? " " + cls : ""}${explains}"${t}>${esc(val)}</span>`;
  }

  function agentTile(ag, gloss) {
    const showModel = ag.model && ag.model !== "—" && ag.model !== "inherits";
    const kindGloss = gloss[ag.kind];
    const chips = [
      agentChip(ag.access, gloss),
      showModel ? `<span class="agent-chip model">${esc(ag.model)}</span>` : "",
      agentChip(ag.surface, gloss, "ghost"),
      agentChip(ag.cadence, gloss, "ghost"),
    ].join("");
    return `
      <div class="agent-tile">
        <div class="agent-top">
          <span class="agent-ic">${esc(ag.icon)}</span>
          <div class="agent-id">
            <div class="agent-name">${esc(ag.name)}</div>
            <div class="agent-kind${kindGloss ? " explains" : ""}"${kindGloss ? ` title="${esc(kindGloss)}"` : ""}>${esc(ag.kind)}</div>
          </div>
          <span class="agent-pip ${agentTone(ag.access)}" title="${esc(ag.access)}"></span>
        </div>
        <p class="agent-tagline">${esc(ag.tagline)}</p>
        <div class="agent-chips">${chips}</div>
      </div>`;
  }

  // The "What the labels mean" key — every pill's plain-language meaning, from
  // the catalog's own glossary (single source), so touch users get an
  // explanation the hover tooltips can't give them.
  function agentKeyHTML(glossary) {
    if (!glossary || !glossary.length) return "";
    const groups = glossary.map((grp) => `
      <div class="key-group">
        <div class="key-h">${esc(grp.label)}</div>
        <dl class="key-list">${grp.terms.map((t) => `
          <div class="key-row"><dt>${esc(t.term)}</dt><dd>${esc(t.gloss)}</dd></div>`).join("")}
        </dl>
      </div>`).join("");
    return `
      <div class="card agent-key">
        <p class="eyebrow">What the labels mean</p>
        ${groups}
      </div>`;
  }

  // The Agents tab: Ledger's autonomous layer as a field of Garden tiles,
  // grouped by nature, with a Key that explains every label. Pure function of
  // the catalog; v1 shows catalog facts, not live health.
  function agentsHTML(data) {
    const groups = (data && data.groups) || [];
    const glossaryList = (data && data.glossary) || [];
    const gloss = {};
    glossaryList.forEach((grp) => grp.terms.forEach((t) => { gloss[t.term] = t.gloss; }));
    const sections = groups
      .filter((g) => g.agents && g.agents.length)
      .map((g) => `
        <section class="agent-group">
          <p class="eyebrow">${esc(g.label)}</p>
          <div class="agent-grid">${g.agents.map((ag) => agentTile(ag, gloss)).join("")}</div>
        </section>`).join("");
    const note = data && data.live_status
      ? ""
      : `<p class="agent-note">Catalog view — what each one is and can do. Live health is coming.</p>`;
    return `
      <div class="agents-view">
        <div class="agents-head">
          <h2>Agents</h2>
          ${note}
        </div>
        ${sections || `<p class="empty">No agents configured.</p>`}
        ${sections ? agentKeyHTML(glossaryList) : ""}
      </div>`;
  }

  return { fmt, esc, ord, monthName, nudgeText, ruleSuggestionText, catEmoji, itemIcon,
           shortDate, daysBetween, restockForecastHTML, newStapleSuggestionsHTML,
           unmatchedStaplesHTML, staleShoppingHTML, stapleSpendHTML,
           postShoppingHTML, inventoryHTML,
           vsLastMonth, incomeCardHTML, shortMonth, trendSummary, trendBars, incomeTrendChartHTML,
           spendingCompositionHTML, memberBreakdownHTML, billVarianceHTML,
           budgetStatusHTML,
           savingsRateTrendHTML, categoryTrendHTML,
           cashFlowForecastHTML, anomaliesHTML, recurringChargesHTML, goalPaceHTML,
           askThreadHTML, agentsHTML };
});
