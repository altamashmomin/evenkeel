/* Pure-render-function unit tests — plain node, no framework, no build
   step. Run: `node tests/test_render.js` (or via test_frontend_render.py,
   which folds it into the Python suite). Covers the presentation helpers
   split into static/render.js — the one corner of the frontend that's
   pure data -> string and so can be tested without a browser.

   These would have caught the "1 inflow still need tagging" subject-verb
   bug that previously only my eye caught in the browser. */
"use strict";
const assert = require("assert");
const R = require("../static/render.js");

let passed = 0;
function check(name, fn) { fn(); passed++; }

// ---- esc: escapes the &<> subset, nothing else; null -> "" ----
check("esc escapes &<>", () => {
  assert.strictEqual(R.esc("a<b>&c"), "a&lt;b&gt;&amp;c");
});
check("esc handles null/undefined", () => {
  assert.strictEqual(R.esc(null), "");
  assert.strictEqual(R.esc(undefined), "");
});
check("esc leaves quotes alone (matches prior behavior)", () => {
  assert.strictEqual(R.esc('he said "hi"'), 'he said "hi"');
});

// ---- fmt: US currency, two decimals, thousands separators ----
check("fmt formats money", () => {
  assert.strictEqual(R.fmt(1234.5), "$1,234.50");
  assert.strictEqual(R.fmt(0), "$0.00");
  assert.strictEqual(R.fmt(3200), "$3,200.00");
});

// ---- monthName ----
check("monthName renders long month + year", () => {
  assert.strictEqual(R.monthName("2026-07"), "July 2026");
  assert.strictEqual(R.monthName("2026-01"), "January 2026");
});

// ---- nudgeText: subject-verb agreement (shared by card + activity banner) ----
check("nudgeText agrees with count", () => {
  assert.strictEqual(R.nudgeText(1), "1 inflow still needs tagging");
  assert.strictEqual(R.nudgeText(2), "2 inflows still need tagging");
  assert.strictEqual(R.nudgeText(3), "3 inflows still need tagging");
});

// ---- ruleSuggestionText: type capitalized, one wording ----
check("ruleSuggestionText capitalizes the type", () => {
  assert.strictEqual(
    R.ruleSuggestionText("paycheck"),
    "You've tagged two as Paycheck. Auto-tag future income that matches?");
  assert.strictEqual(
    R.ruleSuggestionText("reimbursement"),
    "You've tagged two as Reimbursement. Auto-tag future income that matches?");
});

// ---- incomeCardHTML: every state ----
const base = {
  gross_inflows: 3450, true_income: 3200, month_spend: 1806.5,
  net_cash_flow: 1393.5, savings_rate: 0.4355, unclassified_count: 3,
};

check("card headline is true_income in green", () => {
  const html = R.incomeCardHTML(base, "2026-07");
  assert.ok(html.includes("income-amt"), "green income class present");
  assert.ok(html.includes("$3,200.00"), "true income headline");
});

check("positive net is signed + and pos class", () => {
  const html = R.incomeCardHTML(base, "2026-07");
  assert.ok(html.includes("+$1,393.50"), "positive net with +");
  assert.ok(html.includes("income-cell-val pos"), "pos class");
});

check("negative net is signed − and neg class", () => {
  const html = R.incomeCardHTML(
    { ...base, net_cash_flow: -902, savings_rate: -0.28 }, "2026-07");
  assert.ok(html.includes("−$902.00"), "negative net with minus sign");
  assert.ok(html.includes("income-cell-val neg"), "neg class");
});

check("savings rate is a percent, or — when null", () => {
  assert.ok(R.incomeCardHTML(base, "2026-07").includes("44%"), "44%");
  assert.ok(R.incomeCardHTML({ ...base, savings_rate: null }, "2026-07").includes("—"),
            "em dash when null");
});

check("total-money-in row only when gross != true", () => {
  assert.ok(R.incomeCardHTML(base, "2026-07").includes("Total money in"),
            "shown when gross (3450) != true (3200)");
  assert.ok(!R.incomeCardHTML({ ...base, gross_inflows: 3200 }, "2026-07")
              .includes("Total money in"),
            "hidden when gross == true");
});

check("nudge subject-verb agreement (the bug that once shipped)", () => {
  const one = R.incomeCardHTML({ ...base, unclassified_count: 1 }, "2026-07");
  assert.ok(one.includes("1 inflow still needs tagging"), "singular: needs");
  assert.ok(!one.includes("1 inflow still need tagging"), "no disagreement");
  const many = R.incomeCardHTML({ ...base, unclassified_count: 3 }, "2026-07");
  assert.ok(many.includes("3 inflows still need tagging"), "plural: need");
  const none = R.incomeCardHTML({ ...base, unclassified_count: 0 }, "2026-07");
  assert.ok(!none.includes("tagging"), "no nudge at zero");
});

check("empty state when no inflows", () => {
  const html = R.incomeCardHTML({ ...base, gross_inflows: 0 }, "2026-07");
  assert.ok(html.includes("No income recorded this month."), "empty copy");
  assert.ok(!html.includes("income-grid"), "no stat grid in empty state");
});

// ---- shortMonth ----
check("shortMonth abbreviates", () => {
  assert.strictEqual(R.shortMonth("2026-07"), "Jul");
  assert.strictEqual(R.shortMonth("2026-01"), "Jan");
});

// ---- trendSummary: window aggregate, refund-aware, zero-income guard ----
check("trendSummary sums income/spend/saved and rate", () => {
  const s = R.trendSummary([
    { true_income: 6000, month_spend: 3000 },
    { true_income: 4000, month_spend: 5000 },
  ]);
  assert.strictEqual(s.income, 10000);
  assert.strictEqual(s.spend, 8000);
  assert.strictEqual(s.saved, 2000);
  assert.strictEqual(s.rate, 0.2);
});
check("trendSummary rate is null with no income", () => {
  assert.strictEqual(R.trendSummary([{ true_income: 0, month_spend: 500 }]).rate, null);
});

// ---- trendBars: the stacked-bar geometry, every sign case ----
const DIMS = { w: 320, h: 176, padL: 6, padR: 6, padT: 16, padB: 20 };
const approx = (a, b, tol = 0.5) => assert.ok(Math.abs(a - b) < tol, `${a} ≈ ${b}`);

check("trendBars: max spans income and spend; baseline at plot bottom", () => {
  const g = R.trendBars([
    { month: "2026-05", true_income: 6000, month_spend: 3000 },  // surplus
    { month: "2026-06", true_income: 3000, month_spend: 5000 },  // deficit
    { month: "2026-07", true_income: 4000, month_spend: -500 },  // refund month
  ], DIMS);
  assert.strictEqual(g.max, 6000);        // max(income, spend) across window
  assert.strictEqual(g.baselineY, 156);   // h - padB
  assert.strictEqual(g.bars.length, 3);
});
check("trendBars: surplus month has a green cap, no red", () => {
  const b = R.trendBars([{ month: "m", true_income: 6000, month_spend: 3000 }],
    DIMS).bars[0];
  approx(b.spent.h, 70); approx(b.saved.h, 70);
  assert.strictEqual(b.over.h, 0);
});
check("trendBars: deficit month has a red cap, no green", () => {
  const b = R.trendBars([{ month: "m", true_income: 3000, month_spend: 5000 }],
    DIMS).bars[0];
  assert.ok(b.over.h > 0, "deficit shows over-cap");
  assert.strictEqual(b.saved.h, 0);
});
check("trendBars: refund month (negative net spend) is all saved", () => {
  const b = R.trendBars([{ month: "m", true_income: 4000, month_spend: -500 }],
    DIMS).bars[0];
  assert.strictEqual(b.spent.h, 0);
  assert.ok(b.saved.h > 0, "all green when nothing net was spent");
});
check("trendBars: bars sit inside the plot area", () => {
  const g = R.trendBars([
    { month: "a", true_income: 1000, month_spend: 500 },
    { month: "b", true_income: 1000, month_spend: 500 },
  ], DIMS);
  g.bars.forEach((b) => {
    assert.ok(b.x >= DIMS.padL, "left of padding");
    assert.ok(b.x + b.w <= DIMS.w - DIMS.padR + 0.01, "right of padding");
  });
});

// ---- incomeTrendChartHTML: states ----
const SERIES = [
  { month: "2026-05", true_income: 6000, month_spend: 3000 },
  { month: "2026-06", true_income: 4000, month_spend: 2000 },
  { month: "2026-07", true_income: 5000, month_spend: 2500 },
];
check("chart renders svg, bars, labels, legend, headline", () => {
  const html = R.incomeTrendChartHTML(SERIES);
  assert.ok(html.includes("chart-svg"), "svg present");
  assert.ok(html.includes("bar-saved"), "a saved bar present");
  assert.ok(html.includes("Jul"), "month label present");
  assert.ok(html.includes("saved"), "headline present");
  assert.ok(html.includes("Spent") && html.includes("Saved"), "legend present");
});
check("chart headline is positive class when the window saved", () => {
  const html = R.incomeTrendChartHTML(SERIES);
  assert.ok(html.includes('class="pos"'), "pos class for a saving window");
});
check("chart headline is negative class when the window overspent", () => {
  const html = R.incomeTrendChartHTML([
    { month: "2026-07", true_income: 1000, month_spend: 4000 }]);
  assert.ok(html.includes('class="neg"'), "neg class when overspent");
});
check("chart empty state when there is no data", () => {
  assert.ok(R.incomeTrendChartHTML([]).includes("No income or spending"), "empty array");
  assert.ok(R.incomeTrendChartHTML([{ month: "2026-07", true_income: 0, month_spend: 0 }])
    .includes("No income or spending"), "all-zero months");
});

console.log(`render tests passed (${passed} checks)`);
