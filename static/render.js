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

  const fmt = (n) =>
    "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

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
    // savings_rate is a ratio or null (no paycheck income to divide by).
    const rate = inc.savings_rate == null ? "—" : Math.round(inc.savings_rate * 100) + "%";
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
      <p class="stat-big income-amt">${fmt(inc.true_income)}</p>
      <p class="income-label">earned — paychecks only</p>
      <div class="income-grid">
        <div class="income-cell">
          <span class="income-cell-label">Net cash flow</span>
          <span class="income-cell-val ${netCls}">${netStr}</span>
        </div>
        <div class="income-cell">
          <span class="income-cell-label">Savings rate</span>
          <span class="income-cell-val">${rate}</span>
        </div>
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

  return { fmt, esc, ord, monthName, nudgeText, ruleSuggestionText,
           incomeCardHTML, shortMonth, trendSummary, trendBars, incomeTrendChartHTML };
});
