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
  ];
  function catEmoji(s) {
    const t = (s || "").toLowerCase();
    for (const [re, e] of CAT_EMOJI) if (re.test(t)) return e;
    return "💳";
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

  // The Ask tab's chat thread. Pure function of the client-held messages
  // ([{role:'user'|'assistant', content}]) plus a pending flag. Content is
  // escaped and rendered as plain text (newlines preserved by CSS white-space);
  // an empty thread shows a friendly starter with example questions.
  const ASK_EXAMPLES = ["How are we doing this month?", "Did my paycheck land?",
                        "What still needs tagging?", "Who owes who right now?"];

  function askThreadHTML(messages, pending) {
    if (!messages.length && !pending) {
      return `
      <div class="ask-empty">
        <p class="ask-empty-title">Ask about your money</p>
        <p class="ask-empty-sub">Plain questions, plain answers. It reads the
          same numbers the app shows — and can tag a deposit for you when you
          tell it what it was.</p>
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

  return { fmt, esc, ord, monthName, nudgeText, ruleSuggestionText, catEmoji,
           vsLastMonth, incomeCardHTML, shortMonth, trendSummary, trendBars, incomeTrendChartHTML,
           spendingCompositionHTML, memberBreakdownHTML, billVarianceHTML,
           savingsRateTrendHTML, categoryTrendHTML, askThreadHTML };
});
