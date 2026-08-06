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

check("budgetStatusHTML bars under/over budget with actions + unbudgeted line", () => {
  const html = R.budgetStatusHTML({
    period: "2026-06",
    budgets: [
      { category: "Groceries", budgeted: M(50000), actual: M(25000),
        remaining: M(25000), over: false, pct: 50 },
      { category: "Dining", budgeted: M(10000), actual: M(15000),
        remaining: M(-5000), over: true, pct: 150 },
    ],
    unbudgeted_spend: M(8000),
  }, ["Groceries", "Dining", "Gas"]);
  assert.ok(html.includes("Budgets"), "heading");
  assert.ok(html.includes("$250.00 of $500.00 · 50%"), "actual of limit + pct");
  assert.ok(html.includes("width:50%"), "under-budget bar = pct");
  assert.ok(html.includes('class="over" style="width:100%"'), "over-budget bar capped + red class");
  assert.ok(html.includes("$250.00 left") && html.includes("$50.00 over"), "remaining / over badges");
  assert.ok(html.includes("Unbudgeted spend: $80.00"), "unbudgeted line");
  assert.ok(html.includes('data-budget-edit="0"') && html.includes('data-budget-remove="1"'), "index-addressed actions");
  assert.ok(html.includes('id="budget-add"') && html.includes('<option value="Gas">'), "add form + category datalist");
});
check("budgetStatusHTML shows an empty state but still offers the add form", () => {
  const html = R.budgetStatusHTML(
    { period: "2026-06", budgets: [], unbudgeted_spend: M(0) }, ["Gas"]);
  assert.ok(html.includes("No budgets yet"), "empty state");
  assert.ok(html.includes('id="budget-add"'), "add form always present");
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

// ---- inventory step 5: restock-suggestion nudge + match affordance ----
check("inventoryHTML renders a restock nudge with evidence + one-tap confirm", () => {
  const html = R.inventoryHTML({
    items: [{ id: 7, name: "Dog food", kind: "staple", status: "out", note: null }],
    shopping: [], low_count: 1,
    restock_suggestions: [{
      item_id: 7, name: "Dog food", status: "out", matched_by: "phrase",
      purchase: { date: "2026-08-01", description: "CHEWY.COM", amount: { cents: 5420, display: "$54.20" } },
    }],
  });
  assert.ok(html.includes("Looks like you restocked?"), "nudge card heading");
  assert.ok(html.includes('data-restock-confirm="7"'), "one-tap confirm hooked to the item id");
  assert.ok(html.includes("Yes, restocked"), "confirm label");
  assert.ok(html.includes("CHEWY.COM") && html.includes("Aug 1") && html.includes("$54.20"),
    "evidence line quotes the purchase description, date, and display amount");
});
check("inventoryHTML shows no restock card when there are no suggestions", () => {
  const html = R.inventoryHTML({
    items: [{ id: 1, name: "Milk", kind: "staple", status: "stocked", note: null }],
    shopping: [], low_count: 0, restock_suggestions: [],
  });
  assert.ok(!html.includes("Looks like you restocked?"), "no nudge card at zero suggestions");
});
check("inventoryHTML gives each staple a match-editor button; shows the phrase when set", () => {
  const html = R.inventoryHTML({
    items: [{ id: 3, name: "Coffee", kind: "staple", status: "low", note: null, restock_match: "blue bottle" }],
    shopping: [], low_count: 1,
  });
  assert.ok(html.includes('data-item-match="3"'), "match-editor hooked to the item id");
  assert.ok(html.includes("matches") && html.includes("blue bottle"), "current match phrase shown");
});
check("shortDate turns an ISO date into a friendly day, passing junk through", () => {
  assert.strictEqual(R.shortDate("2026-08-01"), "Aug 1");
  assert.strictEqual(R.shortDate(""), "");
});

// ---- restock forecast ("Coming up") — step 5 second half ----
check("daysBetween counts whole calendar days (b − a)", () => {
  assert.strictEqual(R.daysBetween("2026-08-04", "2026-08-10"), 6);
  assert.strictEqual(R.daysBetween("2026-08-04", "2026-08-01"), -3);
  assert.strictEqual(R.daysBetween("2026-08-04", "2026-08-04"), 0);
});
check("restockForecastHTML surfaces stocked staples due soon, soonest first", () => {
  const html = R.restockForecastHTML([
    { item_id: 1, name: "Coffee", status: "stocked", interval_days: 30, predicted_date: "2026-08-12" },
    { item_id: 2, name: "Milk", status: "stocked", interval_days: 7, predicted_date: "2026-08-06" },
  ], "2026-08-04");
  assert.ok(html.includes("Coming up"), "section heading");
  assert.ok(html.includes("likely need in 8 days") && html.includes("likely need in 2 days"), "day counts");
  assert.ok(html.includes("about every 7 days"), "cadence shown");
  assert.ok(html.indexOf("Milk") < html.indexOf("Coffee"), "soonest-due (Milk) first");
  assert.ok(!html.includes("data-mark-low"), "future rows are a heads-up — no action");
});
check("restockForecastHTML labels an overdue staple and offers 'Mark low'", () => {
  const html = R.restockForecastHTML(
    [{ item_id: 1, name: "Coffee", status: "stocked", interval_days: 14, predicted_date: "2026-08-01" }],
    "2026-08-04");
  assert.ok(html.includes("overdue by 3 days"), "overdue label");
  assert.ok(html.includes('data-mark-low="1"'), "overdue row carries the mark-low action for its item");
  assert.ok(html.includes("Mark low"), "action label");
});
check("restockForecastHTML offers 'Mark low' when due today, not before", () => {
  const dueToday = R.restockForecastHTML(
    [{ item_id: 5, name: "Eggs", status: "stocked", interval_days: 7, predicted_date: "2026-08-04" }],
    "2026-08-04");
  assert.ok(dueToday.includes("likely due today") && dueToday.includes('data-mark-low="5"'),
    "due-today row is actionable");
  const soon = R.restockForecastHTML(
    [{ item_id: 6, name: "Eggs", status: "stocked", interval_days: 7, predicted_date: "2026-08-05" }],
    "2026-08-04");
  assert.ok(!soon.includes("data-mark-low"), "a row still one day out is only a heads-up");
});
check("restockForecastHTML hides far-future and non-stocked staples", () => {
  const html = R.restockForecastHTML([
    { item_id: 1, name: "Rice", status: "stocked", interval_days: 60, predicted_date: "2026-09-30" }, // beyond horizon
    { item_id: 2, name: "Soap", status: "low", interval_days: 7, predicted_date: "2026-08-05" },      // already in "Need to buy"
  ], "2026-08-04");
  assert.strictEqual(html, "", "no card when nothing is stocked-and-due-soon");
});
check("restockForecastHTML is empty without a today (derivation is clock-free)", () => {
  const html = R.restockForecastHTML(
    [{ item_id: 1, name: "Coffee", status: "stocked", interval_days: 30, predicted_date: "2026-08-06" }],
    undefined);
  assert.strictEqual(html, "", "no forecast section without a client date");
});

// ---- new-staple suggestions ("Bought a lot — track it?") — step 5 sibling ----
check("newStapleSuggestionsHTML offers a track button carrying the row index", () => {
  const html = R.newStapleSuggestionsHTML([
    { merchant: "Chewy", example_description: "CHEWY.COM* NJ", purchases_seen: 4,
      first_purchase: "2026-06-02", last_purchase: "2026-08-01",
      total_spent: { cents: 8400, display: "$84.00" }, suggested_match: "chewy" },
  ]);
  assert.ok(html.includes("Bought a lot — track it?"), "card heading");
  assert.ok(html.includes("Chewy"), "merchant name");
  assert.ok(html.includes("Bought 4×") && html.includes("$84.00") && html.includes("Aug 1"),
    "count, total spent, and last-purchase date");
  assert.ok(html.includes('data-track-staple="0"'), "track button carries the array index");
});
check("newStapleSuggestionsHTML is empty when there is nothing to suggest", () => {
  assert.strictEqual(R.newStapleSuggestionsHTML([]), "", "no card at zero suggestions");
  assert.strictEqual(R.newStapleSuggestionsHTML(undefined), "", "no card when field absent");
});
check("newStapleSuggestionsHTML escapes the merchant name (no injection)", () => {
  const html = R.newStapleSuggestionsHTML([
    { merchant: "<b>x</b>", purchases_seen: 3, last_purchase: "2026-08-01",
      total_spent: { cents: 100, display: "$1.00" }, suggested_match: "x" },
  ]);
  assert.ok(html.includes("&lt;b&gt;x&lt;/b&gt;") && !html.includes("<b>x</b>"), "merchant escaped");
});

// ---- broken-match detector ("Check the match?") — step 5 sibling ----
check("unmatchedStaplesHTML surfaces a long-tracked staple with a Fix-match action", () => {
  const html = R.unmatchedStaplesHTML(
    [{ item_id: 7, name: "Dish soap", restock_match: null, matched_by: "name",
       tracked_since: "2026-07-01" }],
    "2026-08-04");   // tracked 34 days → past the 21-day grace
  assert.ok(html.includes("Check the match?"), "card heading");
  assert.ok(html.includes('data-item-match="7"') && html.includes("Fix match"), "edit action for the item");
  assert.ok(html.includes("nothing matched its name"), "name-match wording");
});
check("unmatchedStaplesHTML shows the override phrase when matched by phrase", () => {
  const html = R.unmatchedStaplesHTML(
    [{ item_id: 8, name: "Paper towels", restock_match: "costco", matched_by: "phrase",
       tracked_since: "2026-06-01" }],
    "2026-08-04");
  assert.ok(html.includes("costco"), "phrase shown so the user sees what failed to match");
});
check("unmatchedStaplesHTML respects the grace period (no nag on a fresh staple)", () => {
  const forecasts = [{ item_id: 9, name: "Rice", restock_match: null, matched_by: "name",
    tracked_since: "2026-08-01" }];   // only 3 days tracked
  assert.strictEqual(R.unmatchedStaplesHTML(forecasts, "2026-08-04"), "",
    "a just-added staple isn't flagged before there's time to expect a purchase");
});
check("unmatchedStaplesHTML is empty without a today (derivation is clock-free)", () => {
  const html = R.unmatchedStaplesHTML(
    [{ item_id: 7, name: "Dish soap", restock_match: null, matched_by: "name",
       tracked_since: "2026-01-01" }],
    undefined);
  assert.strictEqual(html, "", "no card without a client date");
});

// ---- list-rot detector ("Still need these?") — step 5 sibling ----
check("staleShoppingHTML surfaces a long-neglected low/out staple with a remove action", () => {
  const html = R.staleShoppingHTML(
    [{ item_id: 3, name: "Dish soap", status: "out", low_since: "2026-07-01" }],
    "2026-08-04");   // out 34 days → past the 14-day grace
  assert.ok(html.includes("Still need these?"), "card heading");
  assert.ok(html.includes("out for 34 days"), "how long it's been neglected");
  assert.ok(html.includes('data-item-remove="3"') && html.includes("Not anymore"),
    "reuses the archive action");
});
check("staleShoppingHTML respects the grace period (no nag on a fresh low)", () => {
  const html = R.staleShoppingHTML(
    [{ item_id: 3, name: "Milk", status: "low", low_since: "2026-08-01" }],  // 3 days
    "2026-08-04");
  assert.strictEqual(html, "", "a recently-low item isn't nagged");
});
check("staleShoppingHTML is empty without a today (derivation is clock-free)", () => {
  assert.strictEqual(R.staleShoppingHTML(
    [{ item_id: 3, name: "Milk", status: "low", low_since: "2026-01-01" }], undefined),
    "", "no card without a client date");
});

// ---- money tie-in ("What your staples cost") — step 5's named future ----
check("stapleSpendHTML shows the monthly rate and total verbatim", () => {
  const html = R.stapleSpendHTML([
    { item_id: 1, name: "Coffee", purchases_seen: 3, months_spanned: 3,
      total: { cents: 6000, display: "$60.00" },
      monthly: { cents: 2000, display: "$20.00" } },
  ]);
  assert.ok(html.includes("What your staples cost"), "card heading");
  assert.ok(html.includes("~$20.00/mo"), "monthly rate badge");
  assert.ok(html.includes("$60.00 over 3 mo · 3×"), "total + span + count");
});
check("stapleSpendHTML is empty when nothing qualifies", () => {
  assert.strictEqual(R.stapleSpendHTML([]), "", "no card at zero rows");
  assert.strictEqual(R.stapleSpendHTML(undefined), "", "no card when field absent");
});

// ---- post-shopping review nudge ("You shopped …") — step 5 sibling ----
check("postShoppingHTML nudges after a recent trip when the list is non-empty", () => {
  const html = R.postShoppingHTML(
    { date: "2026-08-03", merchant: "Fresh Mart", category: "Groceries" },
    [{ id: 1, name: "Milk" }], "2026-08-04");   // 1 day ago
  assert.ok(html.includes("You shopped yesterday"), "recency phrasing");
  assert.ok(html.includes("Fresh Mart"), "merchant named");
});
check("postShoppingHTML says 'today' for a same-day trip", () => {
  assert.ok(R.postShoppingHTML(
    { date: "2026-08-04", merchant: "Fresh Mart", category: "Groceries" },
    [{ id: 1 }], "2026-08-04").includes("You shopped today"), "same-day phrasing");
});
check("postShoppingHTML stays quiet when there's nothing to prompt", () => {
  const trip = { date: "2026-08-03", merchant: "Fresh Mart", category: "Groceries" };
  assert.strictEqual(R.postShoppingHTML(trip, [], "2026-08-04"), "", "empty list → no nudge");
  assert.strictEqual(R.postShoppingHTML(trip, [{ id: 1 }], "2026-08-20"), "", "trip aged out → no nudge");
  assert.strictEqual(R.postShoppingHTML(null, [{ id: 1 }], "2026-08-04"), "", "no trip → no nudge");
  assert.strictEqual(R.postShoppingHTML(trip, [{ id: 1 }], undefined), "", "no today → no nudge");
});

// ---- analytics frontend batch (Tier B cards) ----
const money = (c) => ({ cents: c, display: (c < 0 ? "−$" : "$") + Math.abs(c / 100).toFixed(2) });

check("cashFlowForecastHTML shows projected net + remaining bills", () => {
  const html = R.cashFlowForecastHTML({
    period: "2026-08", net_so_far: money(180000),
    bills_remaining_total: money(8000), projected_net: money(172000),
    bills_remaining: [{ bill_id: 1, name: "Internet", due_day: 15, amount: money(5000) }],
  });
  assert.ok(html.includes("Cash flow — August 2026"), "titled by month");
  assert.ok(html.includes(">$1,720.00<") && html.includes('class="pos"'), "projected net, green");
  assert.ok(html.includes("Internet") && html.includes("due day 15"), "remaining bill listed");
});
check("cashFlowForecastHTML colors a negative projection red", () => {
  const html = R.cashFlowForecastHTML({
    period: "2026-08", net_so_far: money(-1000), bills_remaining_total: money(0),
    projected_net: money(-1000), bills_remaining: [],
  });
  assert.ok(html.includes('class="neg"'), "negative projection is red");
  assert.ok(html.includes("No bills left to pay"), "empty bills state");
});
check("anomaliesHTML flags a spike, and has an all-clear empty state", () => {
  const flagged = R.anomaliesHTML({
    month: "2026-04", threshold_pct: 50,
    anomalies: [{ category: "Groceries", current: money(20000), baseline: money(10000),
                  delta: money(10000), pct_over: 100 }],
  });
  assert.ok(flagged.includes("Groceries") && flagged.includes("100% over"), "shows overage");
  const clear = R.anomaliesHTML({ month: "2026-04", threshold_pct: 50, anomalies: [] });
  assert.ok(clear.includes("Nothing unusual"), "all-clear empty state");
});
check("recurringChargesHTML lists a subscription, else the sparse note", () => {
  const html = R.recurringChargesHTML({
    recurring: [{ merchant: "Netflix", cadence: "monthly", amount: money(1549),
                  predicted_next: "2026-09-01" }],
  });
  assert.ok(html.includes("Netflix") && html.includes("monthly"), "subscription row");
  assert.ok(R.recurringChargesHTML({ recurring: [] }).includes("None detected yet"), "sparse note");
});
check("goalPaceHTML maps status to a chip and shows the projection", () => {
  const html = R.goalPaceHTML({
    goals: [
      { name: "Vacation", saved: money(40000), target: money(100000),
        projected_date: "2026-10-28", status: "on_track" },
      { name: "Car", saved: money(20000), target: money(100000),
        projected_date: "2027-02-01", status: "behind" },
    ],
  });
  assert.ok(html.includes("Vacation") && html.includes("on track") && html.includes('badge paid'), "on_track → green chip");
  assert.ok(html.includes("Car") && html.includes("behind") && html.includes('badge overdue'), "behind → clay chip");
  assert.ok(html.includes("$400.00 of $1,000.00"), "saved of target");
  assert.ok(R.goalPaceHTML({ goals: [] }).includes("No goals yet"), "empty state");
});

// Wiring guard: app.js pulls Render helpers off window.Render in ONE destructuring
// block and then calls them by BARE name. A helper added to render.js + called in
// app.js but forgotten in that block is undefined at runtime ("Can't find variable")
// — exactly what took out the Analytics tab when the budgets card shipped. This
// fails the build instead of production.
check("app.js destructures every Render helper it calls by bare name", () => {
  const fs = require("fs"), path = require("path");
  const app = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
  const block = app.match(/const\s*\{([^}]*)\}\s*=\s*window\.Render/);
  assert.ok(block, "found the `= window.Render` destructuring block");
  const imported = new Set(block[1].split(",").map((s) => s.trim()).filter(Boolean));
  for (const name of Object.keys(R)) {
    // bare call `name(` — not `.name(` (a method) and not `xname(` (a longer word)
    const calledBare = new RegExp("(^|[^.\\w])" + name + "\\s*\\(").test(app);
    if (calledBare) {
      assert.ok(imported.has(name),
        `app.js calls ${name}() but never destructures it from window.Render`);
    }
  }
});

console.log(`render tests passed (${passed} checks)`);
