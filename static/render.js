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

  return { fmt, esc, ord, monthName, nudgeText, incomeCardHTML };
});
