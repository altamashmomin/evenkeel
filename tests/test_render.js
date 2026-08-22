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
check("esc escapes quotes too (safe in attribute position)", () => {
  // Both quote forms must be escaped: esc()-ed values are interpolated into
  // double-quoted HTML attributes (aria-labels on pantry/budget buttons), so a
  // bare " would break out of the attribute and inject a handler. See the
  // CODE-REVIEW-2026-08-07 P0 XSS finding.
  assert.strictEqual(R.esc('a"b'), "a&quot;b");
  assert.strictEqual(R.esc("a'b"), "a&#39;b");
  const out = R.esc('x" onmouseover="alert(1)');
  assert.ok(!/"/.test(out), "no bare double-quote survives esc");
});
check("esc'd item name cannot break out of an aria-label attribute", () => {
  // Render the REAL shipped inventoryHTML with a hostile item name and assert
  // the payload cannot escape the aria-label into a live event handler.
  const evil = 'Milk" onmouseover=alert(document.cookie) x="';
  const html = R.inventoryHTML({
    items: [{ id: 1, name: evil, kind: "staple", status: "low", active: 1 }],
    shopping: [], low_count: 1, restock_suggestions: [], restock_forecast: [],
    new_staple_suggestions: [], unmatched_staples: [], stale_shopping_items: [],
    staple_spend: [], last_shopping_trip: null,
  });
  // The breakout signature is a BARE quote closing the attribute followed by a
  // handler. A neutralized payload keeps the text but as inert &quot; entities.
  assert.ok(!/"\s+onmouseover=/.test(html),
    "hostile item name broke out of the attribute into a live handler");
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

// ---- transferRuleText: transfer nudge copy (not income wording) ----
check("transferRuleText reads as transfers, not income", () => {
  const t = R.transferRuleText();
  assert.ok(/transfer/i.test(t));
  assert.ok(!/income/i.test(t), "transfer nudge should not say income");
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
check("askThread shows tap-through nav chips only on a write reply (A1)", () => {
  const html = R.askThreadHTML([
    { role: "assistant", content: "Tagged it as your paycheck",
      actions: [{ tab: "activity", label: "Review in Activity" }] },
    { role: "assistant", content: "You spent $40 on coffee." },
  ], false);
  const chips = html.match(/class="ask-nav"/g) || [];
  assert.equal(chips.length, 1, "exactly one chip — only the write reply");
  assert.ok(html.includes('data-ask-nav="activity"'), "chip carries its tab");
  assert.ok(html.includes("Review in Activity"), "chip label present");
});
check("askThread renders one chip per action, deduped list preserved (A1)", () => {
  const html = R.askThreadHTML([
    { role: "assistant", content: "Marked milk and coffee out",
      actions: [{ tab: "inventory", label: "Open Pantry" }] },
  ], false);
  assert.equal((html.match(/class="ask-nav"/g) || []).length, 1, "one pantry chip");
  assert.ok(html.includes('data-ask-nav="inventory"'), "targets the pantry tab");
});
check("askThread: a plain read reply carries no chip (A1)", () => {
  const html = R.askThreadHTML([
    { role: "assistant", content: "You spent $40 on coffee.", actions: [] },
    { role: "assistant", content: "No actions field at all." },
  ], false);
  assert.equal((html.match(/class="ask-nav"/g) || []).length, 0, "reads stay chip-free");
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
// ---- A2: adaptive follow-up prompts ----
check("askFollowups suggests pantry next steps after a pantry write (A2)", () => {
  const fu = R.askFollowups([
    { role: "user", content: "we're out of coffee" },
    { role: "assistant", content: "Marked coffee out.",
      actions: [{ tab: "inventory", label: "Open Pantry" }], tools_used: ["ledger_set_item_status"] },
  ]);
  assert.ok(fu.length >= 1 && fu.length <= 3, "1–3 suggestions");
  assert.ok(fu.some((q) => /low|shopping trip/i.test(q)), "pantry-flavored");
});
check("askFollowups is topical to the read tools used (A2)", () => {
  const fu = R.askFollowups([
    { role: "user", content: "how are the budgets?" },
    { role: "assistant", content: "You're under on all but dining.",
      actions: [], tools_used: ["ledger_budget_status"] },
  ]);
  assert.ok(fu.some((q) => /budget|cut back/i.test(q)), "budget follow-ups");
});
check("askFollowups never suggests back the question just asked (A2)", () => {
  const q = "Who owes who right now?";
  const fu = R.askFollowups([
    { role: "user", content: q },
    { role: "assistant", content: "Charlee owes you $20.",
      actions: [], tools_used: ["ledger_household_snapshot"] },
  ]);
  assert.ok(!fu.map((s) => s.toLowerCase()).includes(q.toLowerCase()), "excluded");
  assert.ok(fu.length <= 3, "still capped");
});
check("askFollowups falls back to examples when nothing keys (A2)", () => {
  const fu = R.askFollowups([
    { role: "user", content: "hello" },
    { role: "assistant", content: "Hi!", actions: [], tools_used: [] },
  ]);
  assert.ok(fu.length >= 1, "always offers a nudge");
});
check("askFollowups: none when the last turn isn't an assistant reply (A2)", () => {
  assert.deepEqual(R.askFollowups([{ role: "user", content: "hi" }]), []);
  assert.deepEqual(R.askFollowups([]), []);
});
check("askThread renders a follow-up row after a reply, none while pending (A2)", () => {
  const msgs = [
    { role: "user", content: "how's the pantry?" },
    { role: "assistant", content: "All stocked.", actions: [], tools_used: ["ledger_inventory"] },
  ];
  assert.ok(R.askThreadHTML(msgs, false).includes("ask-followups"), "row shown when idle");
  assert.ok(!R.askThreadHTML(msgs, true).includes("ask-followups"), "hidden while thinking");
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
check("recatSheetHTML offers the delete-category zone only when rows exist", () => {
  const withRows = R.recatSheetHTML("Doomed", "August 2026",
    [{ id: 1, description: "X", category: "Doomed", date: "2026-08-02", amount: { cents: 100, display: "$1.00" } }]);
  assert.ok(withRows.includes('id="recat-delete-cat"'), "delete button present");
  assert.ok(withRows.includes("every month"), "copy says the move spans all months");
  assert.ok(withRows.includes("Delete “Doomed” everywhere…"), "summary names the category");
  const empty = R.recatSheetHTML("Doomed", "August 2026", []);
  assert.ok(!empty.includes("recat-delete-cat"), "no delete zone without rows");
});
check("inventoryHTML groups the shopping list by store when any is set", () => {
  const html = R.inventoryHTML({
    items: [],
    shopping: [
      { id: 2, name: "Milk", kind: "staple", status: "out", store: "Trader Joes" },
      { id: 3, name: "Paper towels", kind: "staple", status: "low", store: "Costco" },
      { id: 4, name: "Candles", kind: "oneoff", status: "out", store: null },
    ],
    low_count: 2,
  }, "2026-08-20");
  assert.ok(html.includes("🏬 Costco") && html.includes("🏬 Trader Joes"), "store headers");
  assert.ok(html.indexOf("Costco") < html.indexOf("Trader Joes"), "stores A→Z");
  assert.ok(html.indexOf("Trader Joes") < html.indexOf("Anywhere"), "ungrouped last");
  const flat = R.inventoryHTML({
    items: [],
    shopping: [{ id: 2, name: "Milk", kind: "staple", status: "out", store: null }],
    low_count: 1,
  }, "2026-08-20");
  assert.ok(!flat.includes("Anywhere"), "no store set → flat list, no headers");
});
check("inventoryHTML shows need-by badges framed against today", () => {
  const html = R.inventoryHTML({
    items: [],
    shopping: [
      { id: 2, name: "Candles", kind: "oneoff", status: "out", need_by: "2026-08-22" },
      { id: 3, name: "Card", kind: "oneoff", status: "out", need_by: "2026-08-10" },
    ],
    low_count: 0,
  }, "2026-08-20");
  assert.ok(/badge due">by /.test(html), "future deadline badged due");
  assert.ok(/badge overdue">by /.test(html), "past deadline badged overdue");
});
check("inventoryHTML snoozed items leave the active list, nudges, and got-all", () => {
  const html = R.inventoryHTML({
    items: [],
    shopping: [
      { id: 2, name: "Milk", kind: "staple", status: "out", snoozed_until: "2026-09-01" },
      { id: 3, name: "Eggs", kind: "staple", status: "out" },
      { id: 4, name: "Bread", kind: "staple", status: "low" },
    ],
    restock_suggestions: [
      { item_id: 2, name: "Milk", status: "out", snoozed_until: "2026-09-01", matched_by: "name", purchase: { date: "2026-08-19", description: "MILK RUN" } },
    ],
    low_count: 3,
  }, "2026-08-20");
  assert.ok(html.includes("💤 Snoozed (1)"), "snoozed drawer with count");
  assert.ok(html.includes('data-item-wake="2"'), "wake control");
  assert.ok(html.includes('data-got-all="3,4"'), "got-all covers only awake rows");
  assert.ok(!html.includes("Yes, restocked"), "snoozed item's restock nudge suppressed");
});
check("inventoryHTML: ordered items sit in an On-the-way drawer with Arrived / Didn't come", () => {
  const html = R.inventoryHTML({
    items: [],
    shopping: [{ id: 2, name: "Milk", kind: "staple", status: "out" }],
    on_the_way: [
      { id: 7, name: "Dog food", kind: "staple", status: "ordered", updated_at: "2026-08-10T12:00:00+00:00" },
      { id: 8, name: "Candles", kind: "oneoff", status: "ordered", updated_at: "2026-08-20T12:00:00+00:00" },
    ],
    low_count: 1,
  }, "2026-08-21");
  assert.ok(html.includes("📦 On the way (2)"), "drawer with count");
  assert.ok(html.includes('data-item-arrived="7"') && html.includes('data-item-missed="7"'), "both actions");
  assert.ok(html.includes("still waiting? (11 days)"), "old order gets the nudge");
  assert.ok(!html.includes("still waiting? (1 days)"), "fresh order does not");
  assert.ok(html.includes('data-item-ordered="2"'), "list rows can be marked ordered");
});
check("inventoryHTML offers Got-everything only for 2+ shopping items", () => {
  const two = R.inventoryHTML({
    items: [],
    shopping: [
      { id: 2, name: "Milk", kind: "staple", status: "out", note: null },
      { id: 3, name: "Party candles", kind: "oneoff", status: "out", note: null },
    ],
    low_count: 1,
  });
  assert.ok(two.includes('data-got-all="2,3"'), "batch button carries the listed ids");
  assert.ok(two.includes("Got everything (2)"), "labelled with the count");
  const one = R.inventoryHTML({
    items: [],
    shopping: [{ id: 2, name: "Milk", kind: "staple", status: "out", note: null }],
    low_count: 1,
  });
  assert.ok(!one.includes("data-got-all"), "no batch button for a single item");
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
check("restockForecastHTML names where the interval came from (manual vs inferred)", () => {
  const manual = R.restockForecastHTML(
    [{ item_id: 1, name: "Coffee", status: "stocked", interval_days: 14,
       interval_source: "manual", predicted_date: "2026-08-10" }],
    "2026-08-04");
  assert.ok(manual.includes("every 14 days (you set this)"), "manual cadence is attributed to the person");
  const inferred = R.restockForecastHTML(
    [{ item_id: 2, name: "Milk", status: "stocked", interval_days: 7,
       interval_source: "cadence", predicted_date: "2026-08-08" }],
    "2026-08-04");
  assert.ok(inferred.includes("about every 7 days (from your purchases)"), "inferred cadence is attributed to the feed");
  const status = R.restockForecastHTML(
    [{ item_id: 3, name: "Dog food", status: "stocked", interval_days: 11,
       interval_source: "status", cycles_seen: 4, predicted_date: "2026-08-09" }],
    "2026-08-04");
  assert.ok(status.includes("about every 11 days (from your last 4 cycles)"),
    "status cadence is attributed to the item's own history");
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
check("staleStaplesHTML surfaces only staples past the grace, with stop-tracking", () => {
  const html = R.staleStaplesHTML([
    { item_id: 1, name: "Moon dust", last_activity: "2026-01-10" },
    { item_id: 2, name: "Star salt", last_activity: "2026-08-01" },
  ], "2026-08-21");
  assert.ok(html.includes("Still tracking these?"), "card");
  assert.ok(html.includes('data-item-remove="1"'), "stale one gets the stop-tracking action");
  assert.ok(!html.includes('data-item-remove="2"'), "recent one is not listed");
  assert.ok(html.includes("7 mo"), "months quiet shown");
  assert.equal(R.staleStaplesHTML([{ item_id: 2, name: "X", last_activity: "2026-08-01" }], "2026-08-21"), "", "nothing past grace → no card");
});
check("tripDueHTML lists due-this-week stocked staples with store and price, skipping snoozed", () => {
  const out = R.tripDueHTML({ due_soon: [
    { item_id: 1, name: "Coffee", store: "Costco", predicted_date: "2026-08-24", typical: { cents: 1200, display: "$12.00" } },
    { item_id: 2, name: "Milk", store: null, predicted_date: "2026-09-30", typical: null },
    { item_id: 3, name: "Eggs", store: null, predicted_date: "2026-08-22", snoozed_until: "2026-09-01", typical: { cents: 500, display: "$5.00" } },
  ] }, "2026-08-21");
  assert.equal(out.count, 1, "only the in-horizon, unsnoozed row");
  assert.equal(out.total_cents, 1200, "priced from typical");
  assert.ok(out.html.includes("due in 3 days") && out.html.includes("🏬 Costco") && out.html.includes("~$12.00"), "row facts");
  assert.ok(out.html.includes('data-mark-low="1"'), "Add to list = mark low");
  assert.equal(R.tripDueHTML({ due_soon: [] }, "2026-08-21").html, "", "empty");
});
check("tripClosureHTML offers one confirm per trip carrying the item ids", () => {
  const html = R.tripClosureHTML([{ purchase: { date: "2026-08-19", description: "COSTCO WHSE", amount: { cents: 14200, display: "$142.00" } },
    items: [{ item_id: 4, name: "Coffee", status: "out" }, { item_id: 5, name: "Milk", status: "low" }], item_ids: [4, 5] }]);
  assert.ok(html.includes("COSTCO WHSE") && html.includes("$142.00"), "trip evidence");
  assert.ok(html.includes('data-restock-all="4,5"'), "batch ids");
  assert.ok(html.includes("Yes, restocked all 2"), "count in the confirm");
});
check("inventoryHTML: a closure group hides its items from per-item hints and the generic nudge", () => {
  const html = R.inventoryHTML({
    items: [], shopping: [{ id: 4, name: "Coffee", kind: "staple", status: "out" }], low_count: 1,
    restock_suggestions: [
      { item_id: 4, name: "Coffee", status: "out", matched_by: "name", purchase: { date: "2026-08-19", description: "COSTCO WHSE" } },
      { item_id: 5, name: "Milk", status: "low", matched_by: "name", purchase: { date: "2026-08-19", description: "COSTCO WHSE" } },
      { item_id: 9, name: "Rice", status: "out", matched_by: "name", purchase: { date: "2026-08-20", description: "RICE BARN" } },
    ],
    trip_closure: [{ purchase: { date: "2026-08-19", description: "COSTCO WHSE" },
      items: [{ item_id: 4, name: "Coffee", status: "out" }, { item_id: 5, name: "Milk", status: "low" }], item_ids: [4, 5] }],
    last_shopping_trip: { date: "2026-08-20", merchant: "Costco", category: "Groceries" },
  }, "2026-08-21");
  assert.ok(html.includes('data-restock-all="4,5"'), "closure card present");
  assert.ok(!html.includes('data-restock-confirm="4"') && !html.includes('data-restock-confirm="5"'), "covered items leave the per-item card");
  assert.ok(html.includes('data-restock-confirm="9"'), "uncovered lone hint stays");
  assert.ok(!html.includes("You shopped"), "generic nudge yields to the closure card");
});
check("listEstimateHTML prices the trip and is honest about coverage", () => {
  const full = R.listEstimateHTML({ lines: [], total: { cents: 8500, display: "$85.00" },
                                    priced_count: 3, unpriced_count: 0 });
  assert.ok(full.includes("This trip ≈ $85.00") && !full.includes("priced"), "no coverage note when all priced");
  const partial = R.listEstimateHTML({ lines: [], total: { cents: 8500, display: "$85.00" },
                                       priced_count: 3, unpriced_count: 2 });
  assert.ok(partial.includes("3 of 5 priced"), "coverage note when some are unpriced");
  assert.equal(R.listEstimateHTML({ lines: [], total: { cents: 0, display: "$0.00" },
                                    priced_count: 0, unpriced_count: 2 }), "", "nothing priced → no line");
});
check("priceTrendBadge flags drift of 15%+ either way, nothing below", () => {
  assert.ok(R.priceTrendBadge({ change_bp: 2000 }).includes("↑ 20%"), "up");
  assert.ok(R.priceTrendBadge({ change_bp: -1800 }).includes("↓ 18%"), "down");
  assert.equal(R.priceTrendBadge({ change_bp: 900 }), "", "below threshold");
  assert.equal(R.priceTrendBadge({ change_bp: null }), "", "no earlier window");
});
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
// ---- agentsHTML: the owner-only Agents catalog ----
const AGENT_CAT = {
  live_status: false,
  groups: [
    { id: "analysts", label: "Analysts & advisors", agents: [
      { id: "ledger-analyst", kind: "subagent", name: "Analyst", icon: "📊",
        access: "read-only", model: "sonnet", surface: "Claude Code",
        cadence: "on-demand", tagline: "Answers money questions from live data." } ] },
    { id: "assistants", label: "The assistants", agents: [
      { id: "mcp", kind: "service", name: "MCP server", icon: "🔌",
        access: "reads + writes", model: "—", surface: "tailnet",
        cadence: "always-on", tagline: "Your Claude's 18 tools over Tailscale." } ] },
  ],
  glossary: [
    { label: "What it can do", terms: [
      { term: "read-only", gloss: "Only looks at data — never changes anything." },
      { term: "reads + writes", gloss: "Can both look at and change data." } ] },
    { label: "Type", terms: [
      { term: "subagent", gloss: "A role-scoped Claude agent." } ] },
  ],
};
check("agentsHTML renders group labels, tiles, names, taglines", () => {
  const h = R.agentsHTML(AGENT_CAT);
  assert.ok(h.includes("Analysts &amp; advisors"), "group label escaped+shown");
  assert.ok(h.includes("Analyst") && h.includes("MCP server"), "both tiles named");
  assert.ok(h.includes("Answers money questions from live data."), "tagline shown");
  assert.strictEqual((h.match(/agent-tile/g) || []).length, 2, "one tile per agent");
});
check("agentsHTML shows a real model chip but hides '—'/inherits", () => {
  const h = R.agentsHTML(AGENT_CAT);
  assert.ok(h.includes("sonnet"), "meaningful model shown");
  assert.strictEqual((h.match(/agent-chip model/g) || []).length, 1,
    "only the sonnet tile gets a model chip; mcp's '—' is hidden");
});
check("agentsHTML pip tone encodes power: read-only=r, writes=w", () => {
  const h = R.agentsHTML(AGENT_CAT);
  assert.ok(/agent-pip r/.test(h), "read-only -> r");
  assert.ok(/agent-pip w/.test(h), "reads + writes -> w");
});
check("agentsHTML shows the catalog note only when live_status is false", () => {
  assert.ok(R.agentsHTML(AGENT_CAT).includes("Catalog view"), "note when not live");
  const live = { live_status: true, groups: AGENT_CAT.groups };
  assert.ok(!R.agentsHTML(live).includes("Catalog view"), "no note when live");
});
check("agentsHTML empty state", () => {
  assert.ok(R.agentsHTML({ live_status: false, groups: [] }).includes("No agents configured"));
});
check("agentsHTML pills are plain — no per-pill tooltip; the Key explains them", () => {
  const h = R.agentsHTML(AGENT_CAT);
  assert.ok(!/data-tip=/.test(h), "no CSS-tooltip attribute on pills");
  assert.ok(!/agent-chip[^>]*\btitle=/.test(h), "no native title tooltip on pills");
  assert.ok(!/agent-pip[^>]*\btitle=/.test(h), "no title tooltip on the pip either");
  assert.ok(h.includes("What the labels mean"), "the Key card still explains the labels");
  assert.ok(h.includes("Only looks at data — never changes anything."), "Key lists each meaning");
});
check("agentsHTML Key is collapsible: a <details>/<summary> per dimension", () => {
  const h = R.agentsHTML(AGENT_CAT);
  assert.ok(/<details class="key-group">/.test(h), "each group is a <details>");
  assert.ok(/<summary class="key-h">What it can do<\/summary>/.test(h), "the label is the clickable summary");
  // collapsed by default = no `open` attribute on the details
  assert.ok(!/<details class="key-group" open/.test(h), "collapsed by default");
});
check("agentsHTML groups are collapsible: <details> per group, count, collapsed by default", () => {
  const h = R.agentsHTML(AGENT_CAT);
  assert.ok(/<details class="agent-group">/.test(h), "each group is a <details>");
  assert.ok(/<summary class="group-h">.*Analysts &amp; advisors/.test(h), "label is the clickable summary");
  assert.ok(/<span class="group-count">1<\/span>/.test(h), "shows how many agents are inside");
  assert.ok(!/<details class="agent-group" open/.test(h), "collapsed by default");
});
check("agentsHTML renders the field once — no repeat", () => {
  const h = R.agentsHTML(AGENT_CAT);
  assert.strictEqual((h.match(/agents-view/g) || []).length, 1, "one view wrapper");
  assert.strictEqual((h.match(/What the labels mean/g) || []).length, 1, "one Key card");
});
check("agentsHTML surfaces the architecture map link to /trace (new tab)", () => {
  const h = R.agentsHTML(AGENT_CAT);
  assert.ok(h.includes('href="/trace"'), "links to the /trace page");
  assert.ok(h.includes('target="_blank"'), "opens a new tab so the app keeps its place");
  assert.ok(/Architecture map/i.test(h), "labelled");
});

// ---- opsPanelHTML: the Agents-tab operations foot ----
check("opsPanelHTML: green badge, report + activity behind collapsibles", () => {
  const h = R.opsPanelHTML(
    { available: true, report: "Ledger Pi Ops — 🟢 GREEN — all healthy", age_hours: 3 },
    { entries: [{ id: 9, at: "2026-08-06T12:00:00", actor: "ui:avery",
                  action: "set_item_status", target: "item:2" }] });
  assert.ok(h.includes("badge paid"), "GREEN report -> green badge");
  assert.ok(h.includes("checked 3h ago"), "age surfaced");
  assert.ok(h.includes("set_item_status"), "audit entry listed");
  assert.ok(/<details class="key-group">/.test(h), "report + activity collapsed");
  assert.ok(!/btn-ops-sync/.test(h), "no sync button (removed)");
});
check("opsPanelHTML: absent report is a normal state; amber/red map to badges", () => {
  const none = R.opsPanelHTML({ available: false }, { entries: [] });
  assert.ok(none.includes("no report") && none.includes("lives on the Pi"));
  const red = R.opsPanelHTML({ available: true, report: "🔴 RED — down", age_hours: 1 }, {});
  assert.ok(red.includes("badge overdue"), "RED -> red badge");
});

// ---- moreSheetHTML: the "More" bottom sheet lists every tab ----
check("moreSheetHTML: a tile per tab, active one highlighted", () => {
  const tabs = [["dashboard", "Home", "🏡"], ["bills", "Bills", "📅"],
                ["ask", "Ask", "💬"], ["agents", "Agents", "🤖"]];
  const h = R.moreSheetHTML(tabs, "bills");
  assert.strictEqual((h.match(/more-tile/g) || []).length, 4, "one tile per tab");
  assert.ok(/data-tab="bills"/.test(h) && /data-tab="agents"/.test(h), "keys present");
  // the active tab (and only it) gets the .on class
  assert.ok(/class="more-tile on" data-tab="bills"/.test(h), "active tab highlighted");
  assert.strictEqual((h.match(/more-tile on/g) || []).length, 1, "exactly one active");
  assert.ok(h.includes("🏡") && h.includes("🤖") && h.includes("Agents"), "glyphs + labels");
});
check("moreSheetHTML: none active when the current tab isn't in the list", () => {
  const h = R.moreSheetHTML([["dashboard", "Home", "🏡"]], "activity");
  assert.strictEqual((h.match(/more-tile on/g) || []).length, 0, "no false highlight");
});

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

check("app.js: every bare call resolves — no call to a name that exists nowhere", () => {
  // The INVERSE of the guard above (CODE-REVIEW-2026-08-07 #9). That one asks
  // "is every Render export that app.js calls destructured?" — it CANNOT see a
  // bare call to a name that exists nowhere (a typo like catEmojiX(, or a helper
  // renamed in render.js), which is exactly what blanked a tab in production.
  // Here we resolve every bare call site against locals (decls + params + catch
  // bindings), the Render destructuring block, and a small globals allowlist;
  // anything left over is an unresolved call that would throw at runtime.
  const fs = require("fs"), path = require("path");
  const app = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
  const stripped = app
    .replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ")
    .replace(/`(?:\\.|[^`\\])*`/g, " ").replace(/"(?:\\.|[^"\\])*"/g, " ")
    .replace(/'(?:\\.|[^'\\])*'/g, " ");
  const KEYWORDS = new Set(["if", "for", "while", "switch", "catch", "return",
    "typeof", "await", "async", "function", "new", "delete", "void", "in", "of",
    "do", "else", "throw", "yield", "case"]);
  const GLOBALS = new Set(["Date", "Error", "Set", "Map", "Promise", "fetch",
    "confirm", "prompt", "alert", "matchMedia", "JSON", "Object", "Array",
    "Number", "String", "Math", "parseInt", "parseFloat", "isNaN", "setTimeout",
    "setInterval", "requestAnimationFrame", "structuredClone", "Boolean",
    "RegExp", "Symbol", "WeakMap", "console", "document", "window", "localStorage"]);
  const defined = new Set();
  for (const m of app.matchAll(/function\s+([a-zA-Z_$][\w$]*)/g)) defined.add(m[1]);
  for (const m of app.matchAll(/(?:const|let|var)\s+([a-zA-Z_$][\w$]*)/g)) defined.add(m[1]);
  const sigs = [...app.matchAll(/function\s*[a-zA-Z_$\w]*\s*\(([^)]*)\)/g),
                ...app.matchAll(/\(([^)]*)\)\s*=>/g),
                ...app.matchAll(/catch\s*\(([^)]*)\)/g)];
  for (const m of sigs)
    for (const p of m[1].split(",").map((s) =>
        s.trim().replace(/[.]{3}/, "").split(/[=:]/)[0].trim()).filter(Boolean))
      if (/^[a-zA-Z_$][\w$]*$/.test(p)) defined.add(p);
  const block = app.match(/const\s*\{([^}]*)\}\s*=\s*window\.Render/);
  const imported = new Set(block[1].split(",").map((s) => s.trim()).filter(Boolean));
  const unresolved = [];
  for (const m of stripped.matchAll(/(^|[^.\w])([a-zA-Z_$][\w$]*)\s*\(/g)) {
    const n = m[2];
    if (KEYWORDS.has(n) || GLOBALS.has(n) || defined.has(n) || imported.has(n)) continue;
    if (!unresolved.includes(n)) unresolved.push(n);
  }
  assert.deepStrictEqual(unresolved, [],
    `app.js calls name(s) that resolve to nothing (typo, deleted/renamed helper, `
    + `or a missing window.Render import): ${unresolved.join(", ")}`);
});

// ---- Recategorize sheet (Home "Spent" drill-in) ----
const RECAT_TXNS = [
  { id: 7, date: "2026-08-03", amount: 42.5, description: "ShopRite", category: "Groceries" },
  { id: 9, date: "2026-08-12", amount: 8.25, description: "Corner deli", category: "Groceries" },
];
check("recatSheetHTML lists each txn with a checked box carrying its id", () => {
  const html = R.recatSheetHTML("Groceries", "August 2026", RECAT_TXNS);
  assert.ok(html.includes('data-recat-id="7"'));
  assert.ok(html.includes('data-recat-id="9"'));
  const boxes = html.match(/class="recat-check"[^>]*checked/g) || [];
  assert.strictEqual(boxes.length, 2, "both rows start checked");
  assert.ok(html.includes("ShopRite") && html.includes("Corner deli"));
  assert.ok(html.includes("$42.50"), "amount formatted as money");
});
check("recatSheetHTML has select-all, a category input, and a disabled Move", () => {
  const html = R.recatSheetHTML("Groceries", "August 2026", RECAT_TXNS);
  assert.ok(html.includes('id="recat-select-all"'));
  assert.ok(/Select all 2/.test(html));
  assert.ok(html.includes('list="category-list"'), "shares the app datalist");
  assert.ok(/id="recat-move"[^>]*disabled/.test(html), "Move starts disabled");
  assert.ok(/only relabels/.test(html), "honest copy present");
});
check("recatSheetHTML empty state has no checklist or Move", () => {
  const html = R.recatSheetHTML("Travel", "August 2026", []);
  assert.ok(/No spending tagged/.test(html));
  assert.ok(!/recat-check/.test(html));
  assert.ok(!/id="recat-move"/.test(html));
});
check("recatSheetHTML neutralizes a hostile category (title + empty copy)", () => {
  // The breakout signature is a BARE quote closing an attribute followed by a
  // handler; esc turns it into inert &quot;, so it must never survive.
  const evil = 'Groceries" onmouseover=alert(1) x="';
  assert.ok(!/"\s+onmouseover=/.test(R.recatSheetHTML(evil, "August 2026", RECAT_TXNS)),
    "hostile category broke out in the populated sheet");
  assert.ok(!/"\s+onmouseover=/.test(R.recatSheetHTML(evil, "August 2026", [])),
    "hostile category broke out in the empty sheet");
});

// ---- Settle-up breakdown (why the amount is what it is) ----
// Server payload: members [first, second]; owed_to_first = what second owes
// first; per-line owed.cents signed (+ = second owes first).
const SB = {
  state: "owing",
  members: [{ id: 1, name: "Avery" }, { id: 2, name: "Blake" }],
  // Net = 6000 - 2000 = 4000 => Blake owes Avery $40 (authoritative headline).
  amount: { cents: 4000, display: "$40.00" },
  ower: { id: 2, name: "Blake" },
  owed: { id: 1, name: "Avery" },
  carryover: { cents: 0, display: "$0.00" },
  owed_to_first: { cents: 6000, display: "$60.00" },   // Blake owes Avery
  owed_to_second: { cents: 2000, display: "$20.00" },  // Avery owes Blake
  lines: [
    { transaction_id: 10, date: "2026-08-14", description: "Costco",
      amount: { cents: 12000, display: "$120.00" },
      paid_by: { id: 1, name: "Avery" }, share_pct: 50,
      owed: { cents: 6000, display: "$60.00" } },
    { transaction_id: 11, date: "2026-08-11", description: "Dinner",
      amount: { cents: 4000, display: "$40.00" },
      paid_by: { id: 2, name: "Blake" }, share_pct: 50,
      owed: { cents: -2000, display: "-$20.00" } },
  ],
};
check("settleBreakdownHTML nets to the same figure from each viewer's side", () => {
  // Net = 6000 - 2000 = 4000 => Blake owes Avery $40.
  const avery = R.settleBreakdownHTML(SB, 1);
  assert.ok(/Blake owes you \$40\.00/.test(avery), "Avery's view: they owe you");
  const blake = R.settleBreakdownHTML(SB, 2);
  assert.ok(/You owe Avery \$40\.00/.test(blake), "Blake's view: you owe them");
});
check("settleBreakdownHTML lists every contributing expense in the ledger", () => {
  const html = R.settleBreakdownHTML(SB, 1);
  assert.ok(/See the 2 expenses behind this/.test(html));
  assert.ok(html.includes("Costco") && html.includes("Dinner"));
  // Avery paid Costco => it's money owed TO Avery (pos); Blake paid Dinner => Avery owes (neg).
  assert.ok(/Costco[\s\S]*?amount pos/.test(html), "Costco is owed-to-you for Avery");
});
check("settleBreakdownHTML flips per-line sign for the other viewer", () => {
  const html = R.settleBreakdownHTML(SB, 2);   // Blake's side
  // Blake paid Dinner => owed to Blake (pos); Avery paid Costco => Blake owes (neg).
  assert.ok(/Dinner[\s\S]*?amount pos/.test(html));
  assert.ok(/Costco[\s\S]*?amount neg/.test(html));
});
check("settleBreakdownHTML settled state says square", () => {
  assert.ok(/square/.test(R.settleBreakdownHTML(
    { state: "settled", members: SB.members, lines: [], amount: { cents: 0, display: "$0.00" },
      ower: null, owed: null, carryover: { cents: 0, display: "$0.00" },
      owed_to_first: { cents: 0 }, owed_to_second: { cents: 0 } }, 1)));
});
check("settleBreakdownHTML headline is authoritative amount, not recomputed", () => {
  // Even if the open-line subtotals were incomplete, the headline uses
  // amount/ower straight from the payload, so it can't disagree with settle-up.
  const html = R.settleBreakdownHTML(SB, 1);   // Avery's view; Blake is ower
  assert.ok(/Blake owes you \$40\.00/.test(html));
});
check("settleBreakdownHTML shows a carryover row only when non-zero", () => {
  assert.ok(!/Earlier balance/.test(R.settleBreakdownHTML(SB, 1)), "clean data: no carry line");
  const withCarry = { ...SB, carryover: { cents: -3000, display: "-$30.00" } };
  const html = R.settleBreakdownHTML(withCarry, 1);
  assert.ok(/Earlier balance/.test(html), "carryover surfaced");
  assert.ok(html.includes("-$30.00"));
});
check("settleBreakdownHTML escapes a hostile description", () => {
  const evil = { ...SB, lines: [{ ...SB.lines[0],
    description: 'Costco<img src=x onerror=alert(1)>' }] };
  const html = R.settleBreakdownHTML(evil, 1);
  assert.ok(!/<img/.test(html), "hostile description was not escaped");
});

// ---- Goals tab: pace line + per-goal what-if ----
check("goalWhatIf is ceiling division; 0 when funded; null when unreachable", () => {
  assert.strictEqual(R.goalWhatIf(100000, 25000), 4);
  assert.strictEqual(R.goalWhatIf(100001, 25000), 5);   // partial month rounds UP
  assert.strictEqual(R.goalWhatIf(0, 25000), 0);
  assert.strictEqual(R.goalWhatIf(-50, 25000), 0);      // overfunded is funded
  assert.strictEqual(R.goalWhatIf(100000, 0), null);
  assert.strictEqual(R.goalWhatIf(100000, -100), null);
  assert.strictEqual(R.goalWhatIf(100000, null), null);
});
check("addMonths crosses year boundaries like the backend's month window", () => {
  assert.strictEqual(R.addMonths("2026-08", 4), "2026-12");
  assert.strictEqual(R.addMonths("2026-08", 5), "2027-01");
  assert.strictEqual(R.addMonths("2026-12", 25), "2029-01");
  assert.strictEqual(R.addMonths("2026-01", 0), "2026-01");
});
check("goalWhatIfText: readout forms", () => {
  assert.strictEqual(R.goalWhatIfText(100000, 25000, "2026-08"),
    "≈ 4 mo — around December 2026");
  assert.strictEqual(R.goalWhatIfText(0, 25000, "2026-08"), "already funded");
  assert.strictEqual(R.goalWhatIfText(100000, null, "2026-08"), "");
  assert.strictEqual(R.goalWhatIfText(100000, 0, "2026-08"), "");
});
check("goalPaceLineHTML: complete / no_pace / projected forms", () => {
  assert.ok(/funded/.test(R.goalPaceLineHTML({ status: "complete" })));
  assert.ok(/no pace yet/.test(R.goalPaceLineHTML({ status: "no_pace",
    monthly_rate: null, projected_date: null })));
  const line = R.goalPaceLineHTML({ status: "on_track",
    monthly_rate: { cents: 25000, display: "$250.00" },
    projected_date: "2027-05-14" });
  assert.ok(line.includes("at ~$250.00/mo"), "rate in the sentence");
  assert.ok(line.includes("May 2027"), "projected month in the sentence");
  assert.ok(line.includes("on track"));
  assert.ok(/behind target/.test(R.goalPaceLineHTML({ status: "behind",
    monthly_rate: { cents: 100 }, projected_date: "2027-01-02" })));
  assert.strictEqual(R.goalPaceLineHTML(null), "", "no pace entry, no line");
});

// ---- userById / userColor: the dependency-injected household lookups ----
// (extracted from app.js with txnRow; app.js now passes state.users in.)
check("userById finds the member, falls back to '?'", () => {
  const users = [{ id: 1, display_name: "Alta" }, { id: 2, display_name: "Charlee" }];
  assert.strictEqual(R.userById(users, 2).display_name, "Charlee");
  assert.strictEqual(R.userById(users, 999).display_name, "?", "unknown id -> placeholder");
  assert.strictEqual(R.userById([], 1).display_name, "?");
  assert.strictEqual(R.userById(undefined, 1).display_name, "?", "missing users survives");
});
check("userColor: first member slot 1, everyone else slot 2", () => {
  const users = [{ id: 7, display_name: "A" }, { id: 9, display_name: "B" }];
  assert.strictEqual(R.userColor(users, 7), "var(--p1)");
  assert.strictEqual(R.userColor(users, 9), "var(--p2)");
  assert.strictEqual(R.userColor(users, 999), "var(--p2)", "unknown id -> slot 2");
});

// ---- beamHTML: the Garden hero (who-owes-who) ----
check("beamHTML settled: no name, no settle button", () => {
  const h = R.beamHTML({ settled: true });
  assert.ok(h.includes("All settled up"));
  assert.ok(!h.includes("btn-settle"), "settled state shows no Settle button");
});
check("beamHTML unsettled: names + amount + settle button", () => {
  const h = R.beamHTML({ settled: false, owes: { name: "Alta" },
    owed: { name: "Charlee" }, amount: 353.51 });
  assert.ok(h.includes("Alta owes"));
  assert.ok(h.includes("Charlee"));
  assert.ok(h.includes("$353.51"));
  assert.ok(h.includes('id="btn-settle"'), "unsettled shows the Settle button");
});
check("beamHTML escapes member names (no attribute/markup breakout)", () => {
  const h = R.beamHTML({ settled: false, owes: { name: 'A<img src=x>' },
    owed: { name: 'B"&' }, amount: 1 });
  assert.ok(!h.includes("<img"), "hostile name is escaped, not injected");
  assert.ok(h.includes("&lt;img"));
  assert.ok(h.includes("&amp;"));
});

// ---- txnRow: transfer / income-in / spend branches, users injected ----
const RUSERS = [{ id: 1, display_name: "Alta" }, { id: 2, display_name: "Charlee" }];
check("txnRow transfer: neutral 🔁, transfer badge, signed but no in/out color class", () => {
  const inb = R.txnRow({ id: 10, paid_by: 1, is_transfer: 1, direction: "in",
    source: "simplefin", description: "Payment Thank You", date: "2026-08-05", amount: 270.36 }, RUSERS);
  assert.ok(inb.includes("🔁"), "transfer glyph");
  assert.ok(inb.includes("transfer-amt"), "neutral amount class");
  assert.ok(inb.includes(">transfer<") || inb.includes("transfer"), "transfer label");
  assert.ok(inb.includes("+$270.36"), "inbound transfer keeps + sign");
  assert.ok(!inb.includes("income-in"), "a transfer is not colored as income");
  assert.ok(inb.includes('data-txn="10"'), "still tappable");
  assert.ok(inb.includes("Alta"), "injected payer name");
  const out = R.txnRow({ id: 11, paid_by: 2, is_transfer: 1, direction: "out",
    source: "manual", description: "CHASE EPAY", date: "2026-07-15", amount: 500 }, RUSERS);
  assert.ok(out.includes("−$500.00"), "outbound transfer shows minus");
  assert.ok(out.includes("var(--p2)"), "second member's color for Charlee");
});
check("txnRow income-in: 💵, +amount, income class, tag chip when unclassified", () => {
  const tagged = R.txnRow({ id: 12, paid_by: 1, direction: "in", income_type: "paycheck",
    source: "simplefin", description: "ACME", date: "2026-08-01", amount: 3200 }, RUSERS);
  assert.ok(tagged.includes("💵"));
  assert.ok(tagged.includes("income-in"));
  assert.ok(tagged.includes("+$3,200.00"));
  assert.ok(tagged.includes("badge income"), "classified inflow shows its type chip");
  const untagged = R.txnRow({ id: 13, paid_by: 2, direction: "in", income_type: "unclassified",
    source: "manual", description: "Venmo", date: "2026-08-02", amount: 40 }, RUSERS);
  assert.ok(untagged.includes("badge untagged"), "unclassified inflow nags with a tag chip");
});
check("txnRow spend: category emoji, shared/personal sub, plain amount", () => {
  const shared = R.txnRow({ id: 14, paid_by: 1, direction: "out", is_shared: 1,
    payer_share_pct: 50, category: "Groceries", source: "simplefin",
    description: "Whole Foods", date: "2026-08-03", amount: 88.2 }, RUSERS);
  assert.ok(shared.includes("shared 50/50"));
  assert.ok(shared.includes("$88.20"));
  assert.ok(!shared.includes("income-in") && !shared.includes("transfer-amt"), "plain spend styling");
  const pct = R.txnRow({ id: 15, paid_by: 2, direction: "out", is_shared: 1,
    payer_share_pct: 70, category: "Rent", source: "manual", description: "Rent",
    date: "2026-08-01", amount: 1800 }, RUSERS);
  assert.ok(pct.includes("payer 70%"), "non-50 split spells the payer share");
  const personal = R.txnRow({ id: 16, paid_by: 1, direction: "out", is_shared: 0,
    category: "Coffee", source: "manual", description: "Cafe", date: "2026-08-04", amount: 5 }, RUSERS);
  assert.ok(personal.includes("personal"));
});
check("txnRow missing direction (frozen dashboard shape) renders as spend, not income", () => {
  // dashboard 'recent' rows come from txn_to_json with no direction — must not
  // fall into the income branch.
  const r = R.txnRow({ id: 17, paid_by: 999, category: "Misc", source: "manual",
    description: "No direction", date: "2026-08-06", amount: 9.99 }, RUSERS);
  assert.ok(!r.includes("income-in"), "no-direction row is not income-colored");
  assert.ok(r.includes("?"), "unknown payer falls back to '?'");
});
check("txnRow escapes hostile description/category (no breakout)", () => {
  const r = R.txnRow({ id: 18, paid_by: 1, direction: "out", is_shared: 0,
    category: 'C"<b>', source: "manual", description: 'D<img src=x onerror=alert(1)>',
    date: "2026-08-07", amount: 1 }, RUSERS);
  assert.ok(!r.includes("<img"), "description injection neutralized");
  assert.ok(!r.includes("<b>"), "category injection neutralized");
  assert.ok(r.includes("&lt;img"));
});

// ---- billRowHTML: the Bills-tab row (paid/unpaid branch, icon fallback) ----
check("billRowHTML paid: 'paid' badge + Undo, no Mark-paid button", () => {
  const h = R.billRowHTML({ id: 7, name: "Rent", category: "Housing",
    due_day: 1, amount: 1800, paid_this_period: true });
  assert.ok(h.includes("badge paid"), "paid badge shown");
  assert.ok(h.includes('data-bill-unpay="7"'), "Undo wired");
  assert.ok(!h.includes("data-bill-pay="), "no Mark-paid button when already paid");
  assert.ok(h.includes("Rent") && h.includes("$1,800.00") && h.includes("due the 1st"));
  assert.ok(h.includes('data-bill-edit="7"'), "row body taps through to edit");
});
check("billRowHTML unpaid: Mark-paid button, no paid badge/Undo", () => {
  const h = R.billRowHTML({ id: 8, name: "Netflix", category: "Entertainment",
    due_day: 22, amount: 15.49, paid_this_period: false });
  assert.ok(h.includes('data-bill-pay="8"'), "Mark-paid wired");
  assert.ok(!h.includes("badge paid") && !h.includes("data-bill-unpay"), "no paid affordances");
  assert.ok(h.includes("due the 22nd"));
});
check("billRowHTML icon falls back to the bill name when category is blank", () => {
  // catEmoji(category || name): "Water Co" has no category -> matches /water/.
  const h = R.billRowHTML({ id: 9, name: "Water Co", category: "",
    due_day: 3, amount: 42, paid_this_period: false });
  assert.ok(h.includes("💧"), "name-derived emoji when category empty");
});
check("billRowHTML escapes hostile name/category (no breakout)", () => {
  const h = R.billRowHTML({ id: 10, name: 'A"<b>&', category: 'C"<x>',
    due_day: 21, amount: 1, paid_this_period: true });
  assert.ok(!h.includes("<b>") && !h.includes("<x>"), "markup neutralized");
  assert.ok(h.includes("&lt;b&gt;") && h.includes("&amp;"));
});

// ---- contribLogHTML: the goal contribution log (empty / sign / escaping) ----
check("contribLogHTML empty state", () => {
  const h = R.contribLogHTML([]);
  assert.ok(h.includes("No contributions yet."));
  assert.ok(h.includes("contrib-log"));
});
check("contribLogHTML: + for a deposit, − for a withdrawal, note inline", () => {
  const h = R.contribLogHTML([
    { by: "Alta", note: "", date: "2026-08-01", amount: 100 },
    { by: "Charlee", note: "birthday", date: "2026-07-15", amount: -50 },
  ]);
  assert.ok(h.includes("+$100.00"), "deposit gets +");
  assert.ok(h.includes("−$50.00"), "withdrawal gets − and abs value");
  assert.ok(!h.includes("−$-"), "no double sign");
  assert.ok(h.includes("birthday") && h.includes("Charlee"));
});
check("contribLogHTML escapes contributor + note", () => {
  const h = R.contribLogHTML([{ by: 'A<b>', note: 'n"<i>', date: "2026-08-02", amount: 5 }]);
  assert.ok(!h.includes("<b>") && !h.includes("<i>"), "markup neutralized");
  assert.ok(h.includes("&lt;b&gt;"));
});

// ---- goalCardHTML: the Goals-tab card (what-if gate, log toggle, injection) ----
const GPACE = { status: "on_track", monthly_rate: { cents: 25000, display: "$250.00" },
  projected_date: "2027-05-14", remaining: { cents: 380000 } };
check("goalCardHTML shows the what-if input when a live pace entry exists", () => {
  const h = R.goalCardHTML({ id: 1, name: "Roof", saved: 1200, target: 5000,
    progress: 0.24, target_date: "2027-06-01" }, GPACE, false, "");
  assert.ok(h.includes('data-goal-whatif="1"'), "what-if input present");
  assert.ok(h.includes("by 2027-06-01"), "target date in the meta line");
  assert.ok(h.includes("$1,200.00 of $5,000.00"));
  assert.ok(h.includes("24%"));
  assert.ok(h.includes("at ~$250.00/mo"), "goalPaceLineHTML folded in");
  assert.ok(h.includes("Show log"), "collapsed -> Show log");
});
check("goalCardHTML hides the what-if when the goal is complete", () => {
  const h = R.goalCardHTML({ id: 2, name: "Trip", saved: 800, target: 800,
    progress: 1, target_date: null }, { status: "complete" }, false, "");
  assert.ok(!h.includes("data-goal-whatif"), "no what-if once complete");
  assert.ok(!h.includes(" · by "), "no eta when target_date is null");
});
check("goalCardHTML hides the what-if when there's no pace entry", () => {
  const h = R.goalCardHTML({ id: 3, name: "Buffer", saved: 300, target: 2000,
    progress: 0.15, target_date: null }, undefined, false, "");
  assert.ok(!h.includes("data-goal-whatif"), "no pace -> no what-if");
});
check("goalCardHTML: logOpen flips the toggle and appends the log", () => {
  const log = '<div class="contrib-log">LOG</div>';
  const open = R.goalCardHTML({ id: 4, name: "Car", saved: 0, target: 100,
    progress: 0, target_date: null }, undefined, true, log);
  assert.ok(open.includes("Hide log"), "open -> Hide log");
  assert.ok(open.includes(log), "the pre-rendered log is appended");
  const shut = R.goalCardHTML({ id: 4, name: "Car", saved: 0, target: 100,
    progress: 0, target_date: null }, undefined, false, "");
  assert.ok(shut.includes("Show log") && !shut.includes("contrib-log"));
});
check("goalCardHTML escapes the goal name (heading + aria-label attribute)", () => {
  // GPACE present -> the what-if input renders, so the name lands in BOTH a text
  // node (the heading) and a double-quoted aria-label. A bare " would break out.
  const h = R.goalCardHTML({ id: 5, name: 'X"<b> & y', saved: 1, target: 2,
    progress: 0.5, target_date: null }, GPACE, false, "");
  assert.ok(!h.includes("<b>"), "name markup neutralized");
  assert.ok(h.includes("&lt;b&gt;") && h.includes("&amp;") && h.includes("&quot;"));
  assert.ok(!/aria-label="What-if monthly contribution for X"/.test(h),
    "the bare quote cannot close the aria-label attribute");
});

console.log(`render tests passed (${passed} checks)`);
