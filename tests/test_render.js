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

console.log(`render tests passed (${passed} checks)`);
