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

  // Escape &, <, >, and BOTH quote forms for safe interpolation into HTML —
  // text nodes AND double-quoted attributes. esc()-ed values land in aria-label
  // attributes (pantry/budget buttons), so a bare " would break out and inject
  // an event handler; escaping quotes closes that (CODE-REVIEW-2026-08-07 P0).
  // &quot;/&#39; render as "/' in text and decode correctly through dataset, so
  // there's no display regression. Ampersand first, so the entities we add
  // aren't themselves re-escaped.
  function esc(s) {
    return (s == null ? "" : String(s))
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
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
  // The transfer-rule nudge copy — a transfer isn't income, so it reads
  // differently from ruleSuggestionText's income wording.
  function transferRuleText() {
    return "You've marked two of these as transfers. " +
           "Auto-mark future transactions that match?";
  }

  function ruleSuggestionText(setType) {
    const label = setType.charAt(0).toUpperCase() + setType.slice(1);
    return `You've tagged two as ${label}. Auto-tag future income that matches?`;
  }

  // Advisory (F3): a caution when the phrase the human is about to save is
  // BROAD — short, with nothing else narrowing it — so it will also catch
  // FUTURE deposits, not just the one in front of them. Returns a string to
  // show under the input, or "" when the phrase is specific enough. Mirrors
  // actions.rule_breadth_warning (the server's authoritative check on the
  // propose path); the in-app dialog only ever carries match_desc + transfer,
  // so those are the only inputs here. Non-blocking — the save still works.
  const BROAD_MATCH_MIN_LEN = 5;           // income: a lone phrase shorter than this
  const BROAD_TRANSFER_MATCH_MIN_LEN = 8;  // transfer: hides money both ways
  function ruleBreadthWarning(matchDesc, isTransfer) {
    const desc = (matchDesc || "").trim();
    if (!desc) return "";
    const floor = isTransfer ? BROAD_TRANSFER_MATCH_MIN_LEN : BROAD_MATCH_MIN_LEN;
    if (desc.length >= floor) return "";
    return isTransfer
      ? `Heads up: “${desc}” is short, so this will also mark FUTURE ` +
        "deposits that contain it — including a paycheck — as transfers, " +
        "hiding them from income and spending. A longer phrase is safer."
      : `Heads up: “${desc}” is short, so this will also tag FUTURE ` +
        "deposits whose description contains it. A longer phrase is safer.";
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

  /* ===== Goals tab: pace line + per-goal what-if =====
     Pure helpers over one /api/analytics/goal-pace entry. The server projects
     at the goal's lifetime-average rate; the what-if is the user's own number,
     computed client-side and never stored. */

  // Months to cover `remaining_cents` at `monthly_cents` per month — ceiling
  // division in integer cents. 0 when nothing remains; null when the rate can
  // never get there (missing, zero, or negative).
  function goalWhatIf(remaining_cents, monthly_cents) {
    if (remaining_cents <= 0) return 0;
    if (!monthly_cents || monthly_cents <= 0) return null;
    return Math.ceil(remaining_cents / monthly_cents);
  }

  // 'YYYY-MM' + n months, the same months-since-year-zero integer walk the
  // backend's _month_window uses, so year boundaries can't drift.
  function addMonths(ym, n) {
    const [y, m] = ym.split("-").map(Number);
    const idx = y * 12 + (m - 1) + n;
    return `${String(Math.floor(idx / 12)).padStart(4, "0")}-${String((idx % 12) + 1).padStart(2, "0")}`;
  }

  // The what-if readout. anchorYM is the month to count from — the view
  // layer's "now" (derivations stay clock-free; today enters here, like the
  // pantry's forecast framing). "" until a usable amount is typed.
  function goalWhatIfText(remaining_cents, monthly_cents, anchorYM) {
    const months = goalWhatIf(remaining_cents, monthly_cents);
    if (months == null) return "";
    if (months === 0) return "already funded";
    return `≈ ${months} mo — around ${monthName(addMonths(anchorYM, months))}`;
  }

  // The per-goal pace sentence under the progress bar, prose form of the
  // analytics card's status chips.
  function goalPaceLineHTML(p) {
    if (!p) return "";
    if (p.status === "complete")
      return `<p class="goal-pace">funded 🎉</p>`;
    if (p.status === "no_pace" || !p.monthly_rate || !p.projected_date)
      return `<p class="goal-pace">no pace yet — log contributions to project a finish</p>`;
    const when = monthName(p.projected_date.slice(0, 7));
    const chip = p.status === "behind" ? " — behind target"
               : p.status === "on_track" ? " — on track" : "";
    return `<p class="goal-pace">at ~${amt(p.monthly_rate)}/mo, done around ${when}${chip}</p>`;
  }

  // The Ask tab's chat thread. Pure function of the client-held messages
  // ([{role:'user'|'assistant', content}]) plus a pending flag. Content is
  // escaped and rendered as plain text (newlines preserved by CSS white-space);
  // an empty thread shows a friendly starter with example questions.
  const ASK_EXAMPLES = ["How are we doing this month?", "Did my paycheck land?",
                        "What do we need from the store?", "Who owes who right now?"];

  // A2: adaptive follow-up prompts. After each reply we offer up to three
  // tappable next questions, chosen from what the reply just DID — the tab a
  // write landed on, and the read tools it used — so the suggestions track the
  // conversation instead of being the same four forever. Pure/client-side (no
  // model call, no cost); the chips reuse the `data-ask-eg` wiring.
  const FOLLOWUPS_BY_TAB = {
    inventory: ["What else are we low on?", "What's the shopping trip look like?"],
    activity: ["How's our income this month?", "Any other deposits to tag?"],
  };
  const FOLLOWUPS_BY_TOOL = {
    ledger_household_snapshot: ["Who owes who right now?", "How's our income this month?"],
    ledger_balance: ["Why is it that amount?", "When did we last settle up?"],
    ledger_spending_composition: ["What did we spend most on?", "How's that vs last month?"],
    ledger_category_trend: ["How's that vs last month?", "What did we spend most on?"],
    ledger_budget_status: ["Which budget am I closest to?", "Where can we cut back?"],
    ledger_income_summary: ["Any deposits still to tag?", "How's our savings rate?"],
    ledger_income_trend: ["How's our savings rate?"],
    ledger_savings_rate_trend: ["How's our income this month?"],
    ledger_member_breakdown: ["Who owes who right now?"],
    ledger_bill_variance: ["What bills are coming up?", "Any charge creeping up?"],
    ledger_recurring_charges: ["Any charge creeping up?", "What bills are coming up?"],
    ledger_cash_flow_forecast: ["Can we afford a big purchase?"],
    ledger_goal_pace: ["How are our goals doing?"],
    ledger_unclassified_inflows: ["What deposits are unlabeled?", "Tag my paycheck"],
    ledger_inventory: ["What's running low?", "What's the shopping trip look like?"],
    ledger_pantry_pulse: ["What's coming due?", "Anything gone quiet?"],
    ledger_search_transactions: ["Break that down by category"],
  };

  // Suggestions for the LAST reply: writes first (the natural next thing to do),
  // then topical by the read tools it used, then a generic fallback so there's
  // always a nudge. Never suggests back the question just asked; capped at 3.
  function askFollowups(messages) {
    if (!Array.isArray(messages) || !messages.length) return [];
    const last = messages[messages.length - 1];
    if (!last || last.role !== "assistant") return [];
    let lastQ = "";
    for (let i = messages.length - 2; i >= 0; i--) {
      if (messages[i].role === "user") { lastQ = (messages[i].content || "").trim(); break; }
    }
    const out = [];
    const add = (arr) => (arr || []).forEach((q) => {
      if (out.length < 3 && !out.includes(q) &&
          q.toLowerCase() !== lastQ.toLowerCase()) out.push(q);
    });
    const tabs = new Set((last.actions || []).map((a) => a.tab));
    if (tabs.has("inventory")) add(FOLLOWUPS_BY_TAB.inventory);
    if (tabs.has("activity")) add(FOLLOWUPS_BY_TAB.activity);
    (last.tools_used || []).forEach((t) => add(FOLLOWUPS_BY_TOOL[t]));
    if (!out.length) add(ASK_EXAMPLES);
    return out;
  }

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
        <button type="button" class="ask-help-link" data-help>How does this work?</button>
      </div>`;
    }
    const bubbles = messages.map((m) => {
      // A1: when the assistant actually changed something, its reply carries
      // tap-through chips — one per destination tab (deduped server-side) — so
      // a write reads differently from a plain answer AND jumps you to where
      // the change lives to see or adjust it. Reads carry none.
      const acts = (m.role === "assistant" && Array.isArray(m.actions)) ? m.actions : [];
      const chips = acts.map((a) =>
        `<button type="button" class="ask-nav" data-ask-nav="${esc(a.tab)}">✓ ${esc(a.label)} →</button>`
      ).join("");
      return `<div class="ask-msg ${m.role === "user" ? "ask-you" : "ask-bot"}">${esc(m.content)}${chips}</div>`;
    }).join("");
    const thinking = pending
      ? `<div class="ask-msg ask-bot ask-thinking"><span></span><span></span><span></span></div>`
      : "";
    // A2: follow-up chips hang under the latest reply (never mid-think). They
    // reuse the example-chip wiring (data-ask-eg → askSend).
    const followups = pending ? [] : askFollowups(messages);
    const fuHTML = followups.length
      ? `<div class="ask-followups">${followups.map((q) =>
          `<button type="button" class="ask-eg ask-followup" data-ask-eg="${esc(q)}">${esc(q)}</button>`
        ).join("")}</div>`
      : "";
    return bubbles + thinking + fuHTML;
  }

  // A purchase date (ISO "YYYY-MM-DD") as a friendly "Aug 1". Used by the
  // restock-suggestion evidence line.
  function shortDate(iso) {
    const [y, m, d] = (iso || "").split("-").map(Number);
    if (!y) return iso || "";
    return new Date(y, m - 1, d).toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }

  // Settle-up breakdown: why the balance is the number it is, told from the
  // viewer's side (meId). Pure function of the /api/settle/breakdown payload
  // plus who's looking. The server already reconciled the signed lines to the
  // balance to the cent; this only presents them. `owed.cents` per line is
  // signed (+ = the SECOND member owes the first), and `owed_to_first/second`
  // are the two directional subtotals — remapped here into "owed to you" vs
  // "you owe" so each user reads their own perspective.
  function settleBreakdownHTML(bd, meId) {
    const members = bd.members || [];
    const lines = bd.lines || [];
    const other = members.find((m) => m.id !== meId) || members[1] || { name: "them" };
    if (bd.state !== "owing") {
      return `<p class="settle-net">Nothing outstanding — you're square.</p>`;
    }
    // Headline is authoritative (from compute_balance via ower/owed/amount),
    // so it always matches the settle-up figure even when a carryover exists.
    const iAmOwer = bd.ower && bd.ower.id === meId;
    const netLine = iAmOwer
      ? `<b class="neg">You owe ${esc(other.name)} ${bd.amount.display}</b>`
      : `<b class="pos">${esc(other.name)} owes you ${bd.amount.display}</b>`;
    // Open-line subtotals, mapped to the viewer's side.
    const first = members[0];
    const meIsFirst = first && meId === first.id;
    const owedToMe = meIsFirst ? bd.owed_to_first.cents : bd.owed_to_second.cents;
    const iOwe = meIsFirst ? bd.owed_to_second.cents : bd.owed_to_first.cents;
    const rows = lines.map((ln) => {
      const mine = ln.paid_by && ln.paid_by.id === meId;   // I paid => they owe me
      const amt = Math.abs((ln.owed && ln.owed.cents) || 0) / 100;
      const tag = mine
        ? `<span class="amt amount pos">+${fmt(amt)}</span>`
        : `<span class="amt amount neg">−${fmt(amt)}</span>`;
      return `<li>
        <div class="grow"><div class="title">${esc(ln.description)}</div>
          <div class="sub">${esc(shortDate((ln.date || "").slice(0, 10)))} · ${esc(ln.paid_by ? ln.paid_by.name : "?")} paid ${esc(ln.amount.display)} · your ${ln.share_pct}%</div></div>
        ${tag}</li>`;
    }).join("");
    // The carryover line only appears when history the settle-links don't
    // cover exists — it keeps the itemization honest instead of over-counting.
    const carry = (bd.carryover && bd.carryover.cents) || 0;
    const carryRow = carry === 0 ? "" : `<li class="settle-carry">
        <div class="grow"><div class="title">Earlier balance</div>
          <div class="sub">from before your last recorded settle-up</div></div>
        <span class="amt amount">${bd.carryover.display}</span></li>`;
    const ledger = lines.length || carry
      ? `<details class="settle-ledger">
           <summary>See the ${lines.length} expense${lines.length === 1 ? "" : "s"} behind this</summary>
           <ul class="list">${rows}${carryRow}</ul>
         </details>`
      : "";
    return `
      <p class="settle-net">${netLine}</p>
      <p class="settle-sub">${esc(other.name)} covered ${fmt(owedToMe / 100)} of your share · you covered ${fmt(iOwe / 100)} of theirs</p>
      ${ledger}`;
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
      // Name where the interval came from, so the prediction isn't a black
      // box: a manual "every N days" the person set, the item's own
      // stocked→out history (inc 3), or a cadence inferred from the feed.
      const cadence = f.interval_source === "manual"
        ? `every ${f.interval_days} days (you set this)`
        : f.interval_source === "status"
        ? `about every ${f.interval_days} days (from your last ${f.cycles_seen} cycles)`
        : `about every ${f.interval_days} days (from your purchases)`;
      return `<li>
        <span class="ic">${itemIcon({ name: f.name })}</span>
        <div class="grow">
          <div class="title">${esc(f.name)}</div>
          <div class="sub">${when} · ${cadence}</div>
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
  /* ===== "Still tracking these?" — the curation guard (Pantry v2 inc 6)
     Pure function of stale_staples[] (each { item_id, name, last_activity,
     tracked_since, last_status_change, last_purchase }) and the client's
     today. The derivation is clock-free; the ~6-month grace is decided here.
     A prompt to review, never an assertion — the action is the existing
     "stop tracking" (data-item-remove, with its confirm), and leaving it is
     fine: salt is stocked forever. */
  function staleStaplesHTML(stale, todayISO, graceDays) {
    if (!stale || !stale.length || !todayISO) return "";
    const grace = graceDays == null ? 180 : graceDays;
    const rows = stale
      .map((s) => ({ s, days: daysBetween(s.last_activity, todayISO) }))
      .filter((x) => x.days >= grace);
    if (!rows.length) return "";
    const li = rows.map(({ s, days }) => `<li>
        <span class="ic">${itemIcon({ name: s.name })}</span>
        <div class="grow">
          <div class="title">${esc(s.name)}</div>
          <div class="sub">no sign of life since ${esc(shortDate(s.last_activity))} · ${Math.floor(days / 30)} mo</div>
        </div>
        <button class="btn small" data-item-remove="${s.item_id}">Stop tracking</button>
      </li>`).join("");
    return `<div class="card suggest-card">
        <p class="eyebrow">Still tracking these?</p>
        <ul class="list">${li}</ul>
        <p class="recat-note">Stocked for months with no restock seen — keep the list to the things you'd hate to run out of. Leaving one is fine.</p>
      </div>`;
  }

  /* ===== "Also grab while you're out" — the trip, composed (Pantry v2 inc 5)
     Pure function of trip_plan.due_soon[] (each { item_id, name, store,
     snoozed_until, predicted_date, typical }) and the client's today. The
     derivation is clock-free; "due this week" and "not snoozed right now"
     are decided here. Each row's action is the existing "Mark low" (it drops
     the staple onto the list — the same verb as the forecast card). Returns
     { html, total_cents, count } so the estimate line can price the trip two
     ways. */
  function tripDueHTML(plan, todayISO, horizonDays) {
    const empty = { html: "", total_cents: 0, count: 0 };
    if (!plan || !plan.due_soon || !plan.due_soon.length || !todayISO) return empty;
    const horizon = horizonDays == null ? 7 : horizonDays;
    const rows = plan.due_soon
      .filter((d) => !(d.snoozed_until && d.snoozed_until > todayISO))
      .map((d) => ({ d, days: daysBetween(todayISO, d.predicted_date) }))
      .filter((x) => x.days <= horizon);
    if (!rows.length) return empty;
    const total = rows.reduce((n, x) => n + (x.d.typical ? x.d.typical.cents : 0), 0);
    const li = rows.map(({ d, days }) => {
      const when = days < 0 ? "overdue" : days === 0 ? "due today" : `due in ${days} day${days === 1 ? "" : "s"}`;
      const bits = [when, d.store ? "🏬 " + esc(d.store) : "", d.typical ? "~" + esc(d.typical.display) : ""]
        .filter(Boolean).join(" · ");
      return `<li>
        <span class="ic">${itemIcon({ name: d.name })}</span>
        <div class="grow">
          <div class="title">${esc(d.name)}</div>
          <div class="sub">${bits}</div>
        </div>
        <button class="btn small" data-mark-low="${d.item_id}">Add to list</button>
      </li>`;
    }).join("");
    return {
      html: `<details class="trip-due" open>
          <summary>Also grab while you're out (${rows.length})</summary>
          <ul class="list">${li}</ul>
        </details>`,
      total_cents: total, count: rows.length,
    };
  }

  /* ===== "Costco, Aug 19 — restock these 3?" — trip closure (inc 5)
     Pure function of trip_closure[] (each { purchase: { date, description,
     amount }, items: [{ item_id, name, status }], item_ids }). One confirm
     per TRIP fires the restock_items batch (data-restock-all carries the
     ids); the per-item "Looks like you restocked?" card keeps only items not
     covered by a group (the caller filters). Suggest-don't-assert: it asks. */
  function tripClosureHTML(groups) {
    if (!groups || !groups.length) return "";
    const cards = groups.map((g) => {
      const p = g.purchase || {};
      const chips = (g.items || []).map((i) =>
        `<span class="badge ${i.status === "out" ? "overdue" : "due"}">${esc(i.name)}</span>`).join(" ");
      return `<div class="card restock-card">
          <p class="eyebrow">Looks like a restock trip</p>
          <p class="match-hint" style="margin:2px">🛒 ${esc(p.description || "")} · ${esc(shortDate(p.date))}${p.amount ? " · " + esc(p.amount.display) : ""}</p>
          <p class="sub" style="margin:6px 2px">${chips}</p>
          <div class="dlg-actions"><span class="spacer"></span>
            <button class="btn small primary" data-restock-all="${(g.item_ids || []).join(",")}">Yes, restocked all ${(g.items || []).length}</button>
          </div>
        </div>`;
    }).join("");
    return cards;
  }

  /* ===== "This trip ≈ $X" — the shopping list, priced (Pantry v2 inc 4)
     Pure function of list_estimate ({ lines, total, priced_count,
     unpriced_count }). Honest about coverage: the total is only over items
     with a purchase history, and the line says how many of the list that is.
     Empty when nothing on the list could be priced. */
  function listEstimateHTML(est, dueTotalCents, dueCount) {
    if (!est || !est.priced_count || !est.total) return "";
    const n = est.priced_count + est.unpriced_count;
    const cover = est.unpriced_count ? ` · ${est.priced_count} of ${n} priced` : "";
    const withDue = dueCount && dueTotalCents
      ? ` · ≈ ${esc(fmt((est.total.cents + dueTotalCents) / 100))} with the ${dueCount} due soon`
      : "";
    return `<p class="sub list-estimate">This trip ≈ ${esc(est.total.display)}${cover}${withDue}</p>`;
  }

  // Price drift badge for a staple_spend entry: shown only when the recent
  // restocks moved >= 15% vs earlier ones (view threshold over change_bp).
  function priceTrendBadge(s) {
    if (s.change_bp == null || Math.abs(s.change_bp) < 1500) return "";
    const pct = Math.round(Math.abs(s.change_bp) / 100);
    return s.change_bp > 0
      ? `<span class="badge overdue">↑ ${pct}%</span>`
      : `<span class="badge">↓ ${pct}%</span>`;
  }

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
        ${priceTrendBadge(s)}
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
     items off as bought; 2+ items also get a one-tap "Got everything" batch —
     Pantry v2 inc 1), and the staples tracker (a tap-to-cycle
     stocked→low→out chip + a 🔎 to set the item's optional purchase-match
     phrase). Each add/track card ends in a quick-add field. Interaction wiring
     lives in app.js; this only builds the markup + its data-* hooks. A staple
     that's low/out shows in both the nudge and shopping cards on purpose —
     they're views over the same items. */
  function inventoryHTML(data, todayISO) {
    const items = data.items || [];
    const shopping = data.shopping || [];
    // #014 snooze is view-layer: the derivations are clock-free, so "snoozed
    // right now" means snoozed_until is strictly after the client's today.
    // A snoozed item leaves the active list AND its restock nudges.
    const isSnoozed = (x) =>
      !!(x.snoozed_until && todayISO && x.snoozed_until > todayISO);
    const closure = (data.trip_closure || []).map((g) => ({
      ...g, items: (g.items || []).filter((i) => !isSnoozed(i)) }))
      .filter((g) => g.items.length >= 2)
      .map((g) => ({ ...g, item_ids: g.items.map((i) => i.item_id) }));
    const covered = new Set(closure.flatMap((g) => g.item_ids));
    const suggestions = (data.restock_suggestions || [])
      .filter((s) => !isSnoozed(s) && !covered.has(s.item_id));
    const closureCard = tripClosureHTML(closure);
    const due = tripDueHTML(data.trip_plan, todayISO);
    const forecastCard = restockForecastHTML(
      (data.restock_forecast || []).filter((f) => !isSnoozed(f)), todayISO);
    const trackCard = newStapleSuggestionsHTML(data.new_staple_suggestions);
    const matchCard = unmatchedStaplesHTML(data.unmatched_staples, todayISO);
    const staleCard = staleShoppingHTML(data.stale_shopping_items, todayISO);
    const curationCard = staleStaplesHTML(data.stale_staples, todayISO);
    const spendCard = stapleSpendHTML(data.staple_spend);
    // The generic "you shopped — check your list" nudge yields to a concrete
    // closure card when one exists for a recent trip (same event, one card).
    const tripCard = closureCard ? "" : postShoppingHTML(data.last_shopping_trip, shopping, todayISO);

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

    const activeShopping = shopping.filter((it) => !isSnoozed(it));
    const snoozedShopping = shopping.filter(isSnoozed);
    const shopRow = (it) => {
      const tag = it.kind === "oneoff"
        ? `<span class="badge due">need</span>`
        : `<span class="badge ${it.status === "out" ? "overdue" : "due"}">${esc(it.status)}</span>`;
      // A deadline badge frames urgency against the client's today (#014).
      const needBy = it.need_by
        ? `<span class="badge ${todayISO && it.need_by < todayISO ? "overdue" : "due"}">by ${esc(shortDate(it.need_by))}</span>`
        : "";
      const sub = it.note ? esc(it.note) : (it.kind === "oneoff" ? "one-off" : "staple");
      return `<li>
        <span class="ic">${itemIcon(it)}</span>
        <div class="grow">
          <div class="title">${esc(it.name)}</div>
          <div class="sub">${sub}</div>
        </div>
        ${needBy}
        ${tag}
        <button class="btn small" data-item-got="${it.id}">Got it</button>
        <button class="item-x item-match" data-item-ordered="${it.id}"
                aria-label="Mark ${esc(it.name)} as ordered (on the way)">📦</button>
        <button class="item-x item-match" data-item-needby="${it.id}"
                aria-label="Set a deadline for ${esc(it.name)}">📅</button>
        <button class="item-x item-match" data-item-snooze="${it.id}"
                aria-label="Snooze ${esc(it.name)}">💤</button>
      </li>`;
    };
    // Grouped by store when any active row has one (#014): stores A→Z, the
    // ungrouped rest last under "Anywhere". No stores set → the flat list.
    let shopList;
    if (activeShopping.some((it) => it.store)) {
      const groups = new Map();
      activeShopping.forEach((it) => {
        const key = it.store || "";
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(it);
      });
      const stores = [...groups.keys()].filter(Boolean)
        .sort((a, b) => a.localeCompare(b));
      if (groups.has("")) stores.push("");
      shopList = stores.map((store) => `
        <p class="sub shop-store">${store ? "🏬 " + esc(store) : "Anywhere"}</p>
        <ul class="list">${groups.get(store).map(shopRow).join("")}</ul>`).join("");
    } else {
      shopList = `<ul class="list">${activeShopping.map(shopRow).join("")}</ul>`;
    }
    const snoozedRows = snoozedShopping.length
      ? `<details class="shop-snoozed">
          <summary>💤 Snoozed (${snoozedShopping.length})</summary>
          <ul class="list">${snoozedShopping.map((it) => `<li>
            <span class="ic">${itemIcon(it)}</span>
            <div class="grow">
              <div class="title">${esc(it.name)}</div>
              <div class="sub">until ${esc(shortDate(it.snoozed_until))}</div>
            </div>
            <button class="btn small" data-item-wake="${it.id}">Wake</button>
          </li>`).join("")}</ul>
        </details>`
      : "";
    // "On the way" (#015): ordered, not yet arrived — handled, not stocked.
    // updated_at is the ordered-at; past 7 days the row asks "still waiting?".
    const onTheWay = data.on_the_way || [];
    const orderedRows = onTheWay.length
      ? `<details class="shop-ordered" open>
          <summary>📦 On the way (${onTheWay.length})</summary>
          <ul class="list">${onTheWay.map((it) => {
            const since = (it.updated_at || "").slice(0, 10);
            const days = todayISO && since ? daysBetween(since, todayISO) : 0;
            const wait = days >= 7 ? ` · still waiting? (${days} days)` : "";
            return `<li>
              <span class="ic">${itemIcon(it)}</span>
              <div class="grow">
                <div class="title">${esc(it.name)}</div>
                <div class="sub">ordered ${esc(shortDate(since))}${wait}</div>
              </div>
              <button class="btn small primary" data-item-arrived="${it.id}">Arrived</button>
              <button class="btn small" data-item-missed="${it.id}">Didn't come</button>
            </li>`;
          }).join("")}</ul>
        </details>`
      : "";
    const shopRows = (activeShopping.length
      ? shopList
      : `<p class="empty">${snoozedShopping.length
          ? "Nothing needed right now — some items are snoozed below."
          : "Nothing to buy — you're all stocked 🌿"}</p>`) + orderedRows + snoozedRows;

    const stapleRows = items.length
      ? `<ul class="list">${items.map((it) => `
          <li>
            <span class="ic">${itemIcon(it)}</span>
            <div class="grow">
              <div class="title">${esc(it.name)}</div>
              ${it.note ? `<div class="sub">${esc(it.note)}</div>` : ""}
              ${it.restock_interval_days ? `<div class="sub match-hint">⏰ remind every ${it.restock_interval_days} days</div>` : ""}
              ${it.restock_match ? `<div class="sub match-hint">🔎 matches “${esc(it.restock_match)}”</div>` : ""}
              ${it.store ? `<div class="sub match-hint">🏬 ${esc(it.store)}</div>` : ""}
            </div>
            <button class="status-chip ${it.status}" data-item-cycle="${it.id}"
                    data-status="${it.status}"
                    aria-label="${esc(it.name)} is ${it.status}; tap to change">${esc(it.status)}</button>
            <button class="item-x item-match" data-item-store="${it.id}"
                    aria-label="Set where ${esc(it.name)} is bought">🏬</button>
            <button class="item-x item-match" data-item-interval="${it.id}"
                    aria-label="Set a restock reminder for ${esc(it.name)}">⏰</button>
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
      ${closureCard}
      ${restockCard}
      ${tripCard}
      <div class="card">
        <p class="eyebrow">Need to buy</p>
        ${listEstimateHTML(data.list_estimate, due.total_cents, due.count)}
        ${shopRows}
        ${due.html}
        ${activeShopping.length > 1 ? `<button class="btn small" id="inv-got-all"
          data-got-all="${activeShopping.map((it) => it.id).join(",")}">Got everything (${activeShopping.length})</button>` : ""}
        <form class="inv-add" id="inv-add-oneoff" autocomplete="off">
          <input name="name" maxlength="100" placeholder="Add something to buy…">
          <button class="btn small primary" type="submit">Add</button>
        </form>
        <button type="button" class="ask-from" data-ask="pantry">💬 Ask about the pantry</button>
      </div>
      ${staleCard}
      ${forecastCard}
      ${curationCard}
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

  function agentChip(val, cls) {
    if (!val) return "";
    return `<span class="agent-chip${cls ? " " + cls : ""}">${esc(val)}</span>`;
  }

  function agentTile(ag) {
    const showModel = ag.model && ag.model !== "—" && ag.model !== "inherits";
    const chips = [
      agentChip(ag.access),
      showModel ? `<span class="agent-chip model">${esc(ag.model)}</span>` : "",
      agentChip(ag.surface, "ghost"),
      agentChip(ag.cadence, "ghost"),
    ].join("");
    return `
      <div class="agent-tile">
        <div class="agent-top">
          <span class="agent-ic">${esc(ag.icon)}</span>
          <div class="agent-id">
            <div class="agent-name">${esc(ag.name)}</div>
            <div class="agent-kind">${esc(ag.kind)}</div>
          </div>
          <span class="agent-pip ${agentTone(ag.access)}"></span>
        </div>
        <p class="agent-tagline">${esc(ag.tagline)}</p>
        <div class="agent-chips">${chips}</div>
      </div>`;
  }

  // The "What the labels mean" key — every pill's plain-language meaning, from
  // the catalog's own glossary (single source). Collapsed by default: each
  // dimension is a native <details>, click the header to reveal its terms.
  function agentKeyHTML(glossary) {
    if (!glossary || !glossary.length) return "";
    const groups = glossary.map((grp) => `
      <details class="key-group">
        <summary class="key-h">${esc(grp.label)}</summary>
        <dl class="key-list">${grp.terms.map((t) => `
          <div class="key-row"><dt>${esc(t.term)}</dt><dd>${esc(t.gloss)}</dd></div>`).join("")}
        </dl>
      </details>`).join("");
    return `
      <div class="card agent-key">
        <p class="eyebrow">What the labels mean</p>
        ${groups}
      </div>`;
  }

  // The ops panel at the foot of the Agents tab: guardian heartbeat, a
  // Sync-now control, and recent audit activity. Pure — the button's result
  // line is re-rendered by app.js after the POST.
  function opsPanelHTML(health, audit) {
    const h = health || {};
    const badge = !h.available
      ? `<span class="badge due">no report</span>`
      : /GREEN/.test(h.report || "") ? `<span class="badge paid">green</span>`
      : /RED/.test(h.report || "") ? `<span class="badge overdue">red</span>`
      : `<span class="badge due">amber</span>`;
    const healthBody = h.available
      ? `<pre class="ops-report">${esc(h.report)}</pre>
         <p class="agent-note">checked ${h.age_hours}h ago</p>`
      : `<p class="agent-note">No guardian report here — it lives on the Pi (normal on a dev machine).</p>`;
    const entries = ((audit && audit.entries) || []).map((e) => `
      <div class="key-row"><dt>${esc(e.actor)}</dt>
        <dd>${esc(e.action)}${e.target ? " · " + esc(e.target) : ""}
          <span class="ops-when">${esc(shortDate((e.at || "").slice(0, 10)))}</span></dd>
      </div>`).join("");
    return `
      <div class="card ops-panel">
        <p class="eyebrow">Operations</p>
        <div class="ops-row">
          <span class="ops-h">Pi health ${badge}</span>
        </div>
        <details class="key-group">
          <summary class="key-h">Guardian report</summary>
          ${healthBody}
        </details>
        <details class="key-group">
          <summary class="key-h">Recent activity</summary>
          <dl class="key-list">${entries || ""}</dl>
          ${entries ? "" : `<p class="agent-note">Nothing logged yet.</p>`}
        </details>
      </div>`;
  }

  // The Agents tab: Ledger's autonomous layer as a field of Garden tiles,
  // grouped by nature, with a Key that explains every label. Pure function of
  // the catalog; v1 shows catalog facts, not live health.
  function agentsHTML(data) {
    const groups = (data && data.groups) || [];
    const glossaryList = (data && data.glossary) || [];
    const sections = groups
      .filter((g) => g.agents && g.agents.length)
      .map((g) => `
        <details class="agent-group">
          <summary class="group-h"><span class="group-name">${esc(g.label)}</span><span class="group-count">${g.agents.length}</span></summary>
          <div class="agent-grid">${g.agents.map((ag) => agentTile(ag)).join("")}</div>
        </details>`).join("");
    const note = data && data.live_status
      ? ""
      : `<p class="agent-note">Catalog view — what each one is and can do. Live health is coming.</p>`;
    // The architecture map lives at its own page (/trace); surface it here,
    // since this tab is where the system explains itself. Opens in a new tab so
    // the app keeps its place (the map page has no "back" of its own).
    const traceLink = `
      <a class="trace-link" href="/trace" target="_blank" rel="noopener">
        <span class="trace-ic">🗺</span>
        <span class="trace-txt"><b>Architecture map</b><span class="trace-sub">Trace every path — caller to verb to object to derivation to door</span></span>
        <span class="trace-arrow">↗</span>
      </a>`;
    return `
      <div class="agents-view">
        <div class="agents-head">
          <h2>Agents</h2>
          ${note}
        </div>
        ${traceLink}
        ${sections || `<p class="empty">No agents configured.</p>`}
        ${sections ? agentKeyHTML(glossaryList) : ""}
      </div>`;
  }

  // The Help sheet: the Charlee-facing "Your Money, Together" guide, in-app.
  // Plain language, nothing technical: start with Ask, what it can do for
  // you, what it will never touch, and a one-line map of the tabs. The tab
  // map is driven by the SAME `tabs` list the nav is built from
  // ([[key, label, glyph], ...]) so it cannot drift from the real app — a
  // tab without a blurb here still appears, just without the sentence. Every
  // tab row and the "open Ask" button carry data-tab so app.js can navigate;
  // nothing here reads or writes data.
  const HELP_TAB_BLURB = {
    dashboard: "The big picture — who owes who, what you've spent this month, and what's coming up.",
    activity:  "Every transaction. Tap one to fix its label or details.",
    bills:     "Your shared bill calendar — what's due, when, and what's paid. Mark things paid here.",
    goals:     "What you're saving toward, and how it's growing.",
    analytics: "Trends over time — for when you're curious. Nothing you need to touch.",
    inventory: "The shared shopping list and the staples you keep on hand.",
    ask:       "The assistant. Your shortcut whenever you're unsure — start here.",
    agents:    "The helpers running in the background, and a map of how the app fits together. Just for looking.",
  };
  function helpSheetHTML(tabs) {
    const tabRows = (tabs || []).map(([key, label, glyph]) =>
      `<button class="help-tab" data-tab="${esc(key)}" type="button">` +
      `<span class="g">${glyph || ""}</span>` +
      `<span class="grow"><span class="name">${esc(label)}</span>` +
      `<span class="what">${esc(HELP_TAB_BLURB[key] || "")}</span></span></button>`).join("");
    return `
      <p class="sheet-title">How this works</p>
      <div class="help-body">
        <h3 class="help-h">The easiest way in: just ask.</h3>
        <p>There's a tab called <b>Ask</b>. Type a question the way you'd say it out loud, and it answers in plain words — reading the very same numbers the app shows you. No wrong questions.</p>
        <div class="help-chat" role="img" aria-label="Example conversation in the Ask tab">
          <div class="ask-msg ask-you">How are we doing this month?</div>
          <div class="ask-msg ask-bot">You've spent a bit less than last month, and both bills are covered. 🌿</div>
          <div class="ask-msg ask-you">Who owes who right now?</div>
          <div class="ask-msg ask-bot">Right now it's a little from shared expenses. Want the breakdown?</div>
        </div>
        <button class="btn primary help-open-ask" data-tab="ask" type="button">💬 Open Ask</button>

        <h3 class="help-h">A few things it'll do for you.</h3>
        <p>Just say the word. It always tells you what it did, it asks first before changing anything, and <b>every change can be undone.</b></p>
        <ul class="help-can">
          <li><span class="ic">🏷️</span><span><b>Label a deposit</b><br>“That $500 was my paycheck” — and it tags it so your income stays right.</span></li>
          <li><span class="ic">🔀</span><span><b>Move a charge to the right spot</b><br>“That Target run was Household, not Groceries” — it re-files it. Totals and who-owes-who don't budge.</span></li>
          <li><span class="ic">🧺</span><span><b>Keep the pantry list</b><br>“We're out of coffee” or “add paper towels” — it updates the shared shopping list.</span></li>
          <li><span class="ic">🌱</span><span><b>Start a bill or a savings goal</b><br>“Add a $60 gym bill on the 3rd” or “save toward a $1,200 trip” — it sets it up.</span></li>
        </ul>
        <p class="help-tip">💬 <b>See a little “Ask” button on a screen?</b> Tap it — it opens Ask already knowing what you were looking at, so you don't have to explain.</p>

        <div class="help-safe">
          <h3 class="help-h">What it will never touch.</h3>
          <p>The assistant helps with words and lists — never with your actual money. You're always the one in control.</p>
          <ul>
            <li><span class="check">✓</span><span>It <b>never moves money</b>, pays anyone, or sends anything anywhere.</span></li>
            <li><span class="check">✓</span><span>It <b>never settles up</b> on its own — recording a payback is always a tap <em>you</em> make.</span></li>
            <li><span class="check">✓</span><span>It <b>asks before</b> it changes anything, and shows you where it landed so you can check.</span></li>
            <li><span class="check">✓</span><span>Anything it does can be <b>undone</b>. You genuinely cannot break this by trying things.</span></li>
          </ul>
        </div>

        <h3 class="help-h">See it on your calendar.</h3>
        <p>Bills, savings-goal dates, and shopping deadlines can show up right in your phone's calendar app — and they keep themselves up to date. Tap once, subscribe, done.</p>
        <div class="help-cal" id="help-cal"><button class="btn" id="help-cal-btn" type="button">📅 Get my calendar link</button></div>

        <h3 class="help-h">The tabs, at a glance.</h3>
        <p>If you'd rather poke around than ask, tap one:</p>
        <div class="help-tabs">${tabRows}</div>

        <p class="help-stuck">🌿 <b>If you're ever stuck</b> — open Ask and say it in your own words. You can't get it wrong, and you can't break anything.<br><em>And if all else fails — just text Alta. 💚</em></p>
      </div>`;
  }

  // The calendar-subscribe links, swapped into the Help sheet's #help-cal box
  // once app.js has fetched /api/calendar/link. Pure function of the links
  // object ({webcal, https}); null/undefined renders nothing. webcal:// is
  // the one-tap subscribe on iPhone/Mac; the https URL is shown for pasting
  // into anything else (Google Calendar's "from URL", a desktop client).
  function calendarLinkHTML(links) {
    if (!links) return "";
    return `<a class="btn primary" href="${esc(links.webcal)}">📅 Add to my calendar</a>
      <p class="help-cal-url">On iPhone that's one tap. Anywhere else, subscribe to this address:</p>
      <code class="help-cal-code">${esc(links.https)}</code>`;
  }

  // The "More" bottom sheet: every tab as a tile, so all tabs are reachable
  // from any page (the mobile bar only pins Home/Activity/Ask + Add). `items`
  // is [[key, label, glyph], ...]; the active tab is highlighted.
  function moreSheetHTML(items, active) {
    const tiles = items.map(([key, label, glyph]) =>
      `<button class="more-tile${key === active ? " on" : ""}" data-tab="${esc(key)}" type="button">` +
      `<span class="g">${glyph}</span><span class="l">${esc(label)}</span></button>`).join("");
    return `<p class="sheet-title">All tabs</p><div class="more-grid">${tiles}</div>`;
  }

  // The recategorize bottom sheet: the transactions behind one "Spent by
  // category" row, as a checklist you can move into another category. Pure
  // function of (category, month label, the filtered spending txns) — the
  // move itself is app.js looping the edit-transaction verb over the checked
  // ids, so nothing here writes. Reclassifying only relabels: amounts,
  // splits, and the balance are untouched, which the copy states plainly.
  // The category input shares the app's #category-list datalist (existing
  // names) and accepts a brand-new one; a new name simply appears in the
  // breakdown once spend lands in it.
  function recatSheetHTML(category, monthLabel, txns) {
    const rows = (txns || []).map((t) => `
      <label class="recat-row">
        <input type="checkbox" class="recat-check" data-recat-id="${t.id}" checked>
        <span class="ic">${catEmoji(t.category)}</span>
        <span class="grow">
          <span class="title">${esc(t.description)}</span>
          <span class="sub">${esc(shortDate((t.date || "").slice(0, 10)))}</span>
        </span>
        <span class="amt amount">${fmt(t.amount)}</span>
      </label>`).join("");
    const body = txns && txns.length
      ? `<label class="recat-all">
           <input type="checkbox" id="recat-select-all" checked>
           <span>Select all ${txns.length}</span>
         </label>
         <div class="recat-list">${rows}</div>
         <label class="lbl">Move selected to</label>
         <input id="recat-category" list="category-list" autocomplete="off"
                placeholder="New or existing category…"
                aria-label="Move selected transactions to this category">
         <div class="dlg-actions">
           <button class="btn ghost" type="button" id="recat-cancel">Cancel</button>
           <span class="spacer"></span>
           <button class="btn primary" type="button" id="recat-move" disabled>Move</button>
         </div>
         <p class="recat-note">Reclassifying only relabels — amounts, splits, and who owes whom don't change.</p>
         <details class="recat-danger">
           <summary>Delete “${esc(category)}” everywhere…</summary>
           <p class="recat-note">Moves every “${esc(category)}” transaction from <b>every month</b> — plus its budget and any bill or pantry references — into the category typed above. Then “${esc(category)}” is gone. Nothing is deleted from history; no amount or balance changes.</p>
           <button class="btn danger small" type="button" id="recat-delete-cat" disabled>Move everything &amp; delete</button>
         </details>`
      : `<p class="empty">No spending tagged “${esc(category)}” in ${esc(monthLabel)}.</p>
         <div class="dlg-actions"><span class="spacer"></span>
           <button class="btn ghost" type="button" id="recat-cancel">Close</button></div>`;
    return `<p class="sheet-title">Recategorize · ${esc(category)} · ${esc(monthLabel)}</p>${body}`;
  }

  // ---- state-injected row renderers ----
  // These two were the last presentation fns stuck in app.js because they read
  // household state — which member paid, their palette color. Extracted here
  // per the CLAUDE.md testability note by DEPENDENCY-INJECTING `users` rather
  // than reaching for a global: pass the members array in and they're pure
  // again (same (row, users) in, same string out), so test_render.js covers
  // them headless. app.js keeps a thin wrapper that supplies state.users.
  function userById(users, id) {
    return (users || []).find((u) => u.id === id) || { display_name: "?" };
  }
  function userColor(users, id) {
    // First member gets palette slot 1, everyone else slot 2 — the same
    // 2-swatch palette the avatars use, reused for the payer dot. Not a
    // member-count assumption: identical to the deployed behavior.
    const idx = (users || []).findIndex((u) => u.id === id);
    return idx === 0 ? "var(--p1)" : "var(--p2)";
  }

  // The Garden hero: the who-owes-who number as the emotional centerpiece.
  function beamHTML(bal) {
    const msg = bal.settled
      ? `<p class="bh-who">All settled up</p>
         <p class="bh-sub">No one owes anything on shared expenses</p>`
      : `<p class="bh-who">${esc(bal.owes.name)} owes ${esc(bal.owed.name)}
           <b>${fmt(bal.amount)}</b></p>
         <p class="bh-sub">across all shared expenses</p>`;
    const settleBtn = bal.settled
      ? ""
      : `<button class="bh-settle" id="btn-settle" type="button">Settle up</button>`;
    return `
      <div class="balance-hero">
        <p class="bh-eyebrow">Between you two</p>
        ${msg}
        ${settleBtn}
      </div>`;
  }

  // One activity/recent row. `users` is injected (the household members) so the
  // payer name + color stay data-driven without a global.
  function txnRow(t, users) {
    const payer = userById(users, t.paid_by);
    // A transfer between the household's own accounts is neither income nor
    // spend — render it neutrally (a grey chip, no +/− coloring) so it reads as
    // "money moved, not earned or spent." Tapping still opens its dialog.
    if (t.is_transfer) {
      const src = t.source === "manual" ? "" : ` · ${esc(t.source)}`;
      const sign = t.direction === "in" ? "+" : "−";
      return `
      <li class="tap" data-txn="${t.id}">
        <span class="ic">🔁</span>
        <div class="grow">
          <div class="title">${esc(t.description)}</div>
          <div class="sub">
            <span class="dot" style="--pcolor:${userColor(users, t.paid_by)}"></span>${esc(payer.display_name)}
            · transfer${src} <span class="badge">transfer</span>
          </div>
        </div>
        <div style="text-align:right">
          <div class="amt amount transfer-amt">${sign}${fmt(t.amount)}</div>
          <div class="sub">${t.date.slice(5)}</div>
        </div>
      </li>`;
    }
    // direction is absent on the dashboard's "recent" rows (they come from
    // the frozen txn_to_json), so treat missing as an outflow — those keep
    // their existing spend styling untouched.
    if (t.direction === "in") {
      const src = t.source === "manual" ? "" : ` · ${esc(t.source)}`;
      const chip = t.income_type === "unclassified"
        ? `<span class="badge untagged">tag</span>`
        : `<span class="badge income">${esc(t.income_type)}</span>`;
      return `
      <li class="tap" data-txn="${t.id}">
        <span class="ic in">💵</span>
        <div class="grow">
          <div class="title">${esc(t.description)}</div>
          <div class="sub">
            <span class="dot" style="--pcolor:${userColor(users, t.paid_by)}"></span>${esc(payer.display_name)}
            · money in${src} ${chip}
          </div>
        </div>
        <div style="text-align:right">
          <div class="amt amount income-in">+${fmt(t.amount)}</div>
          <div class="sub">${t.date.slice(5)}</div>
        </div>
      </li>`;
    }
    const shared = t.is_shared
      ? t.payer_share_pct === 50 ? "shared 50/50" : `shared · payer ${t.payer_share_pct}%`
      : "personal";
    const src = t.source === "manual" ? "" :
      ` · <span class="badge">${esc(t.source)}</span>`;
    return `
      <li class="tap" data-txn="${t.id}">
        <span class="ic">${catEmoji(t.category)}</span>
        <div class="grow">
          <div class="title">${esc(t.description)}</div>
          <div class="sub">
            <span class="dot" style="--pcolor:${userColor(users, t.paid_by)}"></span>${esc(payer.display_name)}
            · ${esc(t.category)} · ${shared}${src}
          </div>
        </div>
        <div style="text-align:right">
          <div class="amt amount">${fmt(t.amount)}</div>
          <div class="sub">${t.date.slice(5)}</div>
        </div>
      </li>`;
  }

  // The Calendar tab's agenda: this month's bills laid out by due-date, each
  // showing paid / due / overdue, with today highlighted. It reads the very
  // same /api/bills rows the manage-list below uses — it only reorganizes them
  // by date, so the two views can never disagree. Pure and CLOCK-FREE: todayISO
  // and monthISO are injected (same convention as the calendar_events
  // derivation taking as_of), so it's headless-testable.
  //   - each bill's due_day is clamped into the month (due_day 31 -> the
  //     month's real last day; mirrors derivations._clamped_due_date), so
  //     Feb/short months never render a nonexistent date;
  //   - rows sort by clamped date then name, independent of caller order;
  //   - unpaid + due-date-already-past reads "overdue", else "due".
  // NO money is summed here (bill amounts arrive as dollars-as-floats at the
  // JSON edge): the summary is a COUNT, honoring the integer-cents invariant.
  // A "$ still due" figure would have to come from the server's
  // cash_flow_forecast, never JS addition.
  function calendarAgendaHTML(bills, todayISO, monthISO) {
    if (!bills || !bills.length)
      return `<div class="card cal-agenda"><p class="empty">No bills this month.</p></div>`;
    const [y, mo] = monthISO.split("-").map(Number);
    const lastDay = new Date(y, mo, 0).getDate();   // day 0 of next month = this month's last
    const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    const dated = bills.map((b) => {
      const day = Math.min(b.due_day, lastDay);
      const dISO = `${monthISO}-${String(day).padStart(2, "0")}`;
      return { b, day, dISO, wd: WD[new Date(y, mo - 1, day).getDay()] };
    }).sort((p, q) => p.day - q.day || String(p.b.name).localeCompare(String(q.b.name)));
    const rows = dated.map(({ b, day, dISO, wd }) => {
      const paid = b.paid_this_period;
      const overdue = !paid && dISO < todayISO;
      const state = paid ? "paid" : overdue ? "overdue" : "due";
      const today = dISO === todayISO ? " cal-today" : "";
      return `
        <li class="cal-row${today}">
          <span class="cal-day"><b>${day}</b><span>${wd}</span></span>
          <div class="grow">
            <div class="title">${esc(b.name)}</div>
            <div class="sub">${esc(b.category || "")}</div>
          </div>
          <span class="amt amount">${fmt(b.amount)}</span>
          <span class="badge ${state}">${state}</span>
        </li>`;
    });
    const paidCount = bills.filter((b) => b.paid_this_period).length;
    const sum = paidCount === bills.length
      ? `All ${bills.length} paid 🎉`
      : `${paidCount} of ${bills.length} paid`;
    return `
      <div class="card cal-agenda">
        <p class="cal-sum">${sum}</p>
        <ul class="list">${rows.join("")}</ul>
      </div>`;
  }

  // The Home "Pending approvals" card: two-phase changes an assistant PROPOSED
  // that a person must approve (MIRAGE F-1 — an automation proposes, a human
  // confirms). Pure: app.js fetches /api/actions/pending and passes the rows;
  // empty -> "" so the card only shows when something is waiting. Each summary
  // carries bank/user text, so esc() it; the token rides in a data attr for the
  // Approve handler (POST /api/actions/confirm under the person's session).
  function pendingApprovalsHTML(items) {
    if (!items || !items.length) return "";
    const one = items.length === 1;
    const rows = items.map((a) => `
        <li>
          <div class="grow">
            <div class="title">${esc(a.summary)}</div>
            <div class="sub">${esc(a.detail)} · proposed ${esc(a.proposed_by)}</div>
          </div>
          <button class="btn small" data-approve="${esc(a.token)}">Approve</button>
        </li>`).join("");
    return `
      <div class="card approvals">
        <p class="eyebrow">Pending approvals</p>
        <p class="approvals-sub">An assistant proposed ${one ? "this change" : "these changes"} —
          approve to apply, or ignore to let ${one ? "it" : "them"} expire.</p>
        <ul class="list">${rows}</ul>
      </div>`;
  }

  // One Bills-tab row. Pure: paid_this_period picks the "paid" badge + Undo vs
  // the Mark-paid button; the icon falls back to the bill name when the bill
  // carries no category. The section-head + empty state stay in app.js, as with
  // txnRow — this is just the row.
  function billRowHTML(b) {
    const action = b.paid_this_period
      ? `<span class="badge paid">paid</span>
               <button class="btn small ghost" data-bill-unpay="${b.id}">Undo</button>`
      : `<button class="btn small" data-bill-pay="${b.id}">Mark paid</button>`;
    return `
        <li>
          <span class="ic">${catEmoji(b.category || b.name)}</span>
          <div class="grow tap" data-bill-edit="${b.id}">
            <div class="title">${esc(b.name)}</div>
            <div class="sub">${esc(b.category)} · due the ${ord(b.due_day)}</div>
          </div>
          <span class="amt amount">${fmt(b.amount)}</span>
          ${action}
        </li>`;
  }

  // The contribution log inside an expanded goal card: rows -> list, with an
  // empty state. Pure (esc/fmt); app.js fetches the rows on demand and passes
  // them in. A negative contribution (a withdrawal) shows a − sign.
  function contribLogHTML(rows) {
    const body = rows.length
      ? rows.map((c) => `
          <li>
            <div class="grow">
              <div class="title">${esc(c.by)}${c.note ? ` — <span class="sub">${esc(c.note)}</span>` : ""}</div>
              <div class="sub">${c.date}</div>
            </div>
            <span class="amt amount">${c.amount < 0 ? "−" : "+"}${fmt(Math.abs(c.amount))}</span>
          </li>`).join("")
      : `<li><div class="grow sub">No contributions yet.</div></li>`;
    return `<div class="contrib-log"><ul class="list">${body}</ul></div>`;
  }

  // One Goals-tab card. State is INJECTED so the fn stays pure/testable:
  //   paceEntry — the /api/analytics/goal-pace entry for this goal (or undefined),
  //   logOpen   — whether the contribution log is expanded (drives the toggle
  //               label AND whether logHTML is shown),
  //   logHTML   — the pre-rendered contribution log ("" when collapsed).
  // The what-if input appears only when a pace entry exists and the goal isn't
  // already complete. goalPaceLineHTML is render-local.
  function goalCardHTML(g, paceEntry, logOpen, logHTML) {
    const eta = g.target_date ? ` · by ${g.target_date}` : "";
    const whatif = paceEntry && paceEntry.status !== "complete" ? `
        <div class="goal-whatif">
          <input type="number" min="0" step="10" inputmode="decimal"
                 placeholder="What if $/mo…" data-goal-whatif="${g.id}"
                 aria-label="What-if monthly contribution for ${esc(g.name)}">
          <span class="goal-whatif-out" data-goal-whatif-out="${g.id}"></span>
        </div>` : "";
    return `
      <div class="card" data-goal-card="${g.id}">
        <div class="goal-head">
          <h3>🌱 ${esc(g.name)}</h3>
          <button class="btn small" data-goal-add="${g.id}">Add</button>
        </div>
        <div class="goal-bar"><i style="width:${g.progress * 100}%"></i></div>
        <div class="goal-meta">
          <span class="amount">${fmt(g.saved)} of ${fmt(g.target)}${eta}</span>
          <span>${Math.round(g.progress * 100)}%</span>
        </div>
        ${goalPaceLineHTML(paceEntry)}
        ${whatif}
        <div class="goal-meta" style="margin-top:10px">
          <button class="btn small ghost" data-goal-log="${g.id}">
            ${logOpen ? "Hide log" : "Show log"}</button>
          <button class="btn small ghost" data-goal-del="${g.id}">Delete</button>
        </div>
        ${logHTML}
      </div>`;
  }

  return { fmt, esc, ord, monthName, nudgeText, ruleSuggestionText, transferRuleText,
           ruleBreadthWarning,
           userById, userColor, beamHTML, txnRow, calendarAgendaHTML, billRowHTML,
           pendingApprovalsHTML,
           contribLogHTML, goalCardHTML,
           catEmoji, itemIcon,
           moreSheetHTML, helpSheetHTML, calendarLinkHTML, recatSheetHTML, settleBreakdownHTML,
           listEstimateHTML, priceTrendBadge,
           tripDueHTML, tripClosureHTML,
           staleStaplesHTML,
           shortDate, daysBetween, restockForecastHTML, newStapleSuggestionsHTML,
           unmatchedStaplesHTML, staleShoppingHTML, stapleSpendHTML,
           postShoppingHTML, inventoryHTML,
           vsLastMonth, incomeCardHTML, shortMonth, trendSummary, trendBars, incomeTrendChartHTML,
           spendingCompositionHTML, memberBreakdownHTML, billVarianceHTML,
           budgetStatusHTML,
           savingsRateTrendHTML, categoryTrendHTML,
           cashFlowForecastHTML, anomaliesHTML, recurringChargesHTML, goalPaceHTML,
           goalWhatIf, addMonths, goalWhatIfText, goalPaceLineHTML,
           askThreadHTML, askFollowups, agentsHTML, opsPanelHTML };
});
