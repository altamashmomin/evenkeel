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
check("fmt puts the minus before the symbol (−$X, not $-X)", () => {
  assert.strictEqual(R.fmt(-353.51), "−$353.51");
  assert.strictEqual(R.fmt(-1234.5), "−$1,234.50");
  assert.ok(!R.fmt(-5).includes("$-"), "no $- form");
  assert.strictEqual(R.fmt(-0), "$0.00", "negative zero has no minus");
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

check("positive net signed +, marked pos, ring fills to the rate", () => {
  const html = R.incomeCardHTML(base, "2026-07");
  assert.ok(html.includes("+$1,393.50"), "positive net with +");
  assert.ok(html.includes('net <b class="pos">'), "net marked positive");
  assert.ok(html.includes('class="ring'), "savings ring present");
  assert.ok(html.includes("--p:44"), "ring fills to 44%");
});

check("negative net marks net + ring red, ring fill clamps to 0", () => {
  const html = R.incomeCardHTML(
    { ...base, net_cash_flow: -902, savings_rate: -0.28 }, "2026-07");
  assert.ok(html.includes("−$902.00"), "negative net with minus sign");
  assert.ok(html.includes('net <b class="neg">'), "net marked negative");
  assert.ok(html.includes("ring neg"), "ring marked negative");
  assert.ok(html.includes("--p:0"), "negative rate clamps ring fill to 0");
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

// ---- catEmoji ----
check("catEmoji maps categories and falls back to a card", () => {
  assert.strictEqual(R.catEmoji("Groceries"), "🛒");
  assert.strictEqual(R.catEmoji("Coffee shop"), "☕");
  assert.strictEqual(R.catEmoji("Electric bill"), "⚡");
  assert.strictEqual(R.catEmoji("Rent"), "🏠");
  assert.strictEqual(R.catEmoji("Something unmapped"), "💳");
  assert.strictEqual(R.catEmoji(""), "💳");
});

// ---- vsLastMonth ----
check("vsLastMonth pill: up/down/flat, null without a baseline", () => {
  const s = (a, b) => [{ month: "2026-06", month_spend: a },
                       { month: "2026-07", month_spend: b }];
  assert.deepStrictEqual(R.vsLastMonth(s(1000, 1200)), { dir: "up", text: "▲ 20% vs Jun" });
  assert.deepStrictEqual(R.vsLastMonth(s(1000, 800)), { dir: "down", text: "▼ 20% vs Jun" });
  assert.strictEqual(R.vsLastMonth(s(1000, 1000)).dir, "flat");
  assert.strictEqual(R.vsLastMonth(s(0, 500)), null, "no baseline last month");
  assert.strictEqual(R.vsLastMonth([{ month: "2026-07", month_spend: 5 }]), null, "one month");
  assert.strictEqual(R.vsLastMonth([]), null);
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

// ---- analytics tab (Tier A) ----
const M = (c) => ({ cents: c, display: "$" + (c / 100).toFixed(2) });

check("spendingComposition renders category bars + top merchants", () => {
  const html = R.spendingCompositionHTML({
    month: "2026-07", total: M(232074),
    by_category: [{ category: "Rent", amount: M(185000), share: 0.797 },
                  { category: "Dining", amount: M(1526), share: 0.0066 }],
    top_merchants: [{ description: "Whole Foods", amount: M(8210), count: 3 }],
  });
  assert.ok(html.includes("Rent") && html.includes("$1,850.00"), "category + amount");
  assert.ok(html.includes("cat-bar"), "reuses the bar visual");
  assert.ok(html.includes("Whole Foods") && html.includes("3 charges"), "merchant + count");
});
check("spendingComposition empty state", () => {
  assert.ok(R.spendingCompositionHTML({ month: "2026-07", total: M(0), by_category: [] })
    .includes("Nothing spent"), "empty copy");
});

check("memberBreakdown signs net and colors it", () => {
  const html = R.memberBreakdownHTML({ month: "2026-07", members: [
    { name: "Avery", paid: M(185000), owed: M(92500), net: M(92500) },
    { name: "Blake", paid: M(0), owed: M(92500), net: M(-92500) },
  ]});
  assert.ok(html.includes("+$925.00"), "positive net signed +");
  assert.ok(html.includes("−$925.00"), "negative net signed − before $");
  assert.ok(html.includes("mb-fig pos") && html.includes("mb-fig neg"), "net color classes");
});
check("memberBreakdown empty when no shared spend", () => {
  assert.ok(R.memberBreakdownHTML({ month: "2026-07", members: [
    { name: "Avery", paid: M(0), owed: M(0), net: M(0) }]}).includes("No shared spending"));
});

check("billVariance flags over/under/unpaid", () => {
  const html = R.billVarianceHTML({ period: "2026-07", bills: [
    { name: "Rent", defined: M(185000), actual: M(185000), variance: M(0), paid: true },
    { name: "Electric", defined: M(9000), actual: M(9500), variance: M(500), paid: true },
    { name: "Water", defined: M(4000), actual: M(3600), variance: M(-400), paid: true },
    { name: "Internet", defined: M(6500), actual: null, variance: null, paid: false },
  ]});
  assert.ok(html.includes("on budget"), "zero variance label");
  assert.ok(html.includes("+$5.00 over") && html.includes("badge overdue"), "over = red badge");
  assert.ok(html.includes("−$4.00 under") && html.includes("badge paid"), "under = green badge");
  assert.ok(html.includes("unpaid"), "unpaid bill");
});

check("savingsRateTrend headlines latest rolling rate, strips months", () => {
  const html = R.savingsRateTrendHTML({ series: [
    { month: "2026-05", savings_rate: 0.2, rolling_savings_rate: 0.2 },
    { month: "2026-06", savings_rate: -0.3, rolling_savings_rate: -0.05 },
    { month: "2026-07", savings_rate: 0.4, rolling_savings_rate: 0.18 },
  ]});
  assert.ok(html.includes("18%"), "headline latest rolling");
  assert.ok(html.includes("sr-pct neg"), "a negative rolling month is red");
  assert.ok(html.includes("May") && html.includes("Jul"), "month labels");
});
check("savingsRateTrend empty when no rate yet", () => {
  assert.ok(R.savingsRateTrendHTML({ series: [
    { month: "2026-07", savings_rate: null, rolling_savings_rate: null }]})
    .includes("Not enough income"));
});

check("categoryTrend renders dollar bars + MoM delta, empty -> ''", () => {
  const html = R.categoryTrendHTML({ category: "Groceries", series: [
    { month: "2026-06", spend: 420.5, rolling_avg: 400, mom_delta: null },
    { month: "2026-07", spend: 512.25, rolling_avg: 466, mom_delta: 91.75 },
  ]});
  assert.ok(html.includes("Groceries") && html.includes("$512.25"), "category + dollar amount");
  assert.ok(html.includes("+$91.75 vs the month before"), "MoM delta");
  assert.strictEqual(R.categoryTrendHTML({ category: "X", series: [
    { month: "2026-07", spend: 0, rolling_avg: 0, mom_delta: null }]}), "", "no activity -> omitted");
});

// ---- Ask tab chat thread ----
check("askThread empty state offers example questions", () => {
  const html = R.askThreadHTML([], false);
  assert.ok(html.includes("Ask about your money"), "starter title");
  assert.ok(html.includes("data-ask-eg="), "example buttons present");
  assert.ok(!html.includes("ask-msg"), "no bubbles when empty");
});
check("askThread renders user and assistant bubbles", () => {
  const html = R.askThreadHTML([
    { role: "user", content: "is rent paid?" },
    { role: "assistant", content: "Yes, on the 2nd." },
  ], false);
  assert.ok(html.includes('ask-msg ask-you') && html.includes("is rent paid?"), "user bubble");
  assert.ok(html.includes('ask-msg ask-bot') && html.includes("Yes, on the 2nd."), "assistant bubble");
  assert.ok(!html.includes("ask-thinking"), "no thinking bubble when not pending");
});
check("askThread shows a ✓ tagged chip only on a write reply", () => {
  const html = R.askThreadHTML([
    { role: "assistant", content: "Tagged it as your paycheck", tagged: true },
    { role: "assistant", content: "You spent $40 on coffee." },
  ], false);
  const chips = html.match(/ask-tagged/g) || [];
  assert.equal(chips.length, 1, "exactly one chip — only the tagged reply");
  assert.ok(html.includes("✓ tagged"), "chip label present");
});
check("askThread empty state no longer claims it can't change anything", () => {
  const html = R.askThreadHTML([], false);
  assert.ok(!/never changes|read-only/i.test(html), "stale read-only copy gone");
  assert.ok(/tag a deposit/i.test(html), "mentions it can tag");
});
check("askThread shows a thinking bubble while pending", () => {
  const html = R.askThreadHTML([{ role: "user", content: "hi" }], true);
  assert.ok(html.includes("ask-thinking"), "thinking indicator present");
});
check("askThread escapes message content (no HTML injection)", () => {
  const html = R.askThreadHTML([{ role: "assistant", content: "<img src=x onerror=1>" }], false);
  assert.ok(html.includes("&lt;img"), "content escaped");
  assert.ok(!html.includes("<img src=x"), "no raw tag");
});

// ---- itemIcon: pantry rows fall back to a basket, not the money card ----
check("itemIcon maps known keywords and falls back to a basket", () => {
  assert.strictEqual(R.itemIcon({ name: "Paper towels" }), "🧻");
  assert.strictEqual(R.itemIcon({ name: "Dish soap" }), "🧴");
  assert.strictEqual(R.itemIcon({ name: "Coffee beans" }), "☕");
  assert.strictEqual(R.itemIcon({ name: "Batteries" }), "🧺");   // unmapped -> basket
  assert.strictEqual(R.itemIcon({ category: "Groceries", name: "x" }), "🛒");
});

// ---- inventoryHTML ----
check("inventoryHTML renders staples with a tap-to-cycle status chip", () => {
  const html = R.inventoryHTML({
    items: [{ id: 1, name: "Coffee", category: null, kind: "staple", status: "low", note: "the dark roast" }],
    shopping: [], low_count: 1,
  });
  assert.ok(html.includes('data-item-cycle="1"'), "chip carries the id");
  assert.ok(html.includes('data-status="low"'), "chip carries current status");
  assert.ok(html.includes('status-chip low'), "chip coloured by status");
  assert.ok(html.includes("the dark roast"), "note shown as sub");
  assert.ok(html.includes('data-item-remove="1"'), "remove control present");
  assert.ok(html.includes("1 running low"), "low_count badge");
});
check("inventoryHTML shopping list shows staple status and one-off need", () => {
  const html = R.inventoryHTML({
    items: [],
    shopping: [
      { id: 2, name: "Milk", kind: "staple", status: "out", note: null },
      { id: 3, name: "Party candles", kind: "oneoff", status: "out", note: null },
    ],
    low_count: 1,
  });
  assert.ok(html.includes('data-item-got="2"') && html.includes('data-item-got="3"'), "each row checkable");
  assert.ok(html.includes('badge overdue">out'), "out staple badged as urgent");
  assert.ok(html.includes('badge due">need'), "one-off shows 'need'");
});
check("inventoryHTML empty states for both cards", () => {
  const html = R.inventoryHTML({ items: [], shopping: [], low_count: 0 });
  assert.ok(html.includes("Nothing to buy"), "empty shopping list");
  assert.ok(html.includes("No staples tracked yet"), "empty staples");
  assert.ok(!html.includes("running low"), "no low badge at zero");
  assert.ok(html.includes('id="inv-add-staple"') && html.includes('id="inv-add-oneoff"'), "both add fields");
});
check("inventoryHTML escapes item names (no injection)", () => {
  const html = R.inventoryHTML({
    items: [{ id: 4, name: "<b>x</b>", kind: "staple", status: "stocked", note: null }],
    shopping: [], low_count: 0,
  });
  assert.ok(html.includes("&lt;b&gt;x&lt;/b&gt;"), "name escaped");
  assert.ok(!html.includes("<b>x</b>"), "no raw tag");
});

console.log(`render tests passed (${passed} checks)`);
