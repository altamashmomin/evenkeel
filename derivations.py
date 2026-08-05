"""Canonical read-time financial derivations shared by every surface."""

import re
from datetime import date, timedelta


def round_ratio(numerator, denominator):
    """Round a positive rational to nearest integer, ties to even, no floats."""
    quotient, remainder = divmod(numerator, denominator)
    doubled = remainder * 2
    if doubled < denominator:
        return quotient
    if doubled > denominator:
        return quotient + 1
    return quotient + (quotient % 2)


def compute_balance(db, as_of=None):
    """Return the two-member balance in integer cents from normalized splits.

    This preserves the deployed closed-form semantics. General pairwise N-member
    balances remain a separate design increment; this function does not pretend
    the current settlement presentation supports them. ``as_of`` is an optional
    inclusive ISO date used by settle_up so its amount and coverage window agree.

    The direction='out' filter is defense-in-depth, not the only guard:
    inflows never get split rows (INCOME-DESIGN invariant 1), so the join
    already excludes them structurally. INCOME-DESIGN states the rule in
    words too ("the balance computation ignores them entirely") — this
    is that sentence enforced explicitly rather than left as an emergent
    property of a separate table staying empty.
    """
    members = db.execute(
        "SELECT id, username, display_name FROM members WHERE active = 1 ORDER BY id"
    ).fetchall()
    if len(members) < 2:
        return {"state": "waiting", "amount_cents": 0, "members": members}
    first, second = members[0], members[1]
    net_cents = 0  # positive => second owes first
    date_clause = " AND t.txn_date <= ?" if as_of is not None else ""
    params = (as_of,) if as_of is not None else ()
    rows = db.execute(
        """SELECT t.amount_cents, t.paid_by, s.member_id, s.share_bp
           FROM transactions t
           JOIN splits s ON s.transaction_id = t.id
           WHERE s.member_id != t.paid_by AND t.direction = 'out'""" + date_clause,
        params).fetchall()
    for row in rows:
        owed_cents = round_ratio(row["amount_cents"] * row["share_bp"], 10000)
        if row["paid_by"] == first["id"] and row["member_id"] == second["id"]:
            net_cents += owed_cents
        elif row["paid_by"] == second["id"] and row["member_id"] == first["id"]:
            net_cents -= owed_cents
    if net_cents == 0:
        return {"state": "settled", "amount_cents": 0, "members": members}
    ower, owed = (second, first) if net_cents > 0 else (first, second)
    return {
        "state": "owing",
        "amount_cents": abs(net_cents),
        "ower": ower,
        "owed": owed,
        "members": members,
    }


def spending_summary(db, month=None):
    """Net spend by month/category: outflows minus the refunds that reverse
    them, excluding settlement transactions.

    Two legs, netted in SQL. Every non-settlement direction='out' row adds
    to its category's spend; every direction='in' income_type='refund' row
    subtracts from its category's spend in the month it lands. INCOME-DESIGN
    decided refunds are cost reversals, not income — a returned air fryer
    un-spends its category. The dip is deliberately NOT clamped: a refund
    landing a month after the purchase can push a category (or the month
    total) negative, which is true and occasionally surprising, and honest
    category analytics is the whole point.

    Only income_type='refund' nets. This is why spending_summary is EXEMPT
    in the derivation tripwire — it is the one *spend* derivation that reads
    inflows on purpose — but the exemption is bounded: a paycheck, gift,
    transfer, or unclassified inflow must never touch spend. The tripwire
    can no longer prove that for this function (it's exempt), so
    test_income_isolation carries the guard instead, with fixtures that put
    every non-refund inflow type in a spent category and assert the total
    doesn't move. Every other inflow still contributes nothing here.
    """
    month_clause = " AND substr(txn_date, 1, 7) = ?" if month is not None else ""
    params = []
    if month is not None:
        params.extend([month, month])  # one per leg of the UNION
    rows = db.execute(
        f"""SELECT month, category, SUM(signed_cents) AS total_cents FROM (
                SELECT substr(txn_date, 1, 7) AS month, category,
                       amount_cents AS signed_cents
                  FROM transactions
                 WHERE source != 'settlement' AND direction = 'out'{month_clause}
                UNION ALL
                SELECT substr(txn_date, 1, 7) AS month, category,
                       -amount_cents AS signed_cents
                  FROM transactions
                 WHERE direction = 'in' AND income_type = 'refund'{month_clause}
            )
            GROUP BY month, category
            ORDER BY month, total_cents DESC, category""",
        params,
    ).fetchall()
    summaries = {}
    if month is not None:
        summaries[month] = {"total_cents": 0, "by_category": []}
    for row in rows:
        entry = summaries.setdefault(
            row["month"], {"total_cents": 0, "by_category": []})
        entry["total_cents"] += row["total_cents"]
        entry["by_category"].append({
            "category": row["category"],
            "amount_cents": row["total_cents"],
        })
    return summaries


def income_summary(db, month=None):
    """Income and cash-flow aggregates — the ONE derivation that counts
    inflows (every other filters them out; this is where they belong).

    Each field declares which types it counts (INCOME-DESIGN invariant 3):
    gross_inflows is every 'in' row; true_income is paycheck rows only —
    refunds, transfers, gifts, and unclassified are money in but not income
    earned; net_cash_flow is true_income minus the same spend total
    spending_summary computes (one named function, every surface); the
    unclassified count is the tag-me nudge. All integer cents. savings_rate
    is the single intentional exception — a display ratio, not money, so a
    float is idiomatic here — guarded to None when there's no income to
    divide by.

    `month` None means all-time (no clock read, so this stays deterministic
    and callable as income_summary(db)); a YYYY-MM string scopes every
    field to that month, which is what the dashboard card passes. A
    trailing-window form (`months_back`, for the scenarios "measured
    income" average) is deferred to the increment that needs it.
    """
    clause = " AND substr(txn_date, 1, 7) = ?" if month is not None else ""
    params = (month,) if month is not None else ()
    row = db.execute(
        """SELECT
               COALESCE(SUM(amount_cents), 0) AS gross,
               COALESCE(SUM(CASE WHEN income_type = 'paycheck'
                                 THEN amount_cents ELSE 0 END), 0) AS true_income,
               COALESCE(SUM(CASE WHEN income_type = 'unclassified'
                                 THEN 1 ELSE 0 END), 0) AS unclassified
           FROM transactions
           WHERE direction = 'in'""" + clause, params).fetchone()
    gross_cents = row["gross"]
    true_income_cents = row["true_income"]

    if month is not None:
        spend_cents = spending_summary(db, month)[month]["total_cents"]
    else:
        spend_cents = sum(m["total_cents"] for m in spending_summary(db).values())
    net_cash_flow_cents = true_income_cents - spend_cents

    savings_rate = (None if true_income_cents == 0
                    else round(net_cash_flow_cents / true_income_cents, 4))
    return {
        "gross_inflows_cents": gross_cents,
        "true_income_cents": true_income_cents,
        "month_spend_cents": spend_cents,
        "net_cash_flow_cents": net_cash_flow_cents,
        "savings_rate": savings_rate,
        "unclassified_count": row["unclassified"],
    }


def _month_window(anchor, months_back):
    """The list of 'YYYY-MM' strings ending at `anchor` (inclusive), going
    back `months_back` months, in chronological order. Pure integer math on
    a months-since-year-zero index, so it crosses year boundaries correctly
    and reads no clock — the same window every run, which is what keeps the
    trend derivations deterministic and gate-stable."""
    year, month = (int(part) for part in anchor.split("-"))
    end = year * 12 + (month - 1)
    return [f"{i // 12:04d}-{i % 12 + 1:02d}"
            for i in range(end - months_back + 1, end + 1)]


def _latest_data_month(db):
    """The most recent 'YYYY-MM' present in transactions, or None when there
    are none. A plumbing helper (leading underscore) — deliberately not a
    tripwire-checked 'aggregate': its answer legitimately depends on data
    extent, inflows included."""
    row = db.execute(
        "SELECT MAX(substr(txn_date, 1, 7)) AS month FROM transactions").fetchone()
    return row["month"]


def _monthly_series(db, metric_fn, months_back=6, anchor=None):
    """Map a per-month derivation over a trailing window — the shared trend
    engine every *_trend derivation (income now, category/savings-rate/…
    later) is built on, so month bucketing lives and is tested in one place.

    `metric_fn(db, month)` is called for each of the `months_back` months
    ending at `anchor`, chronological order, its dict merged under a 'month'
    key. `anchor` defaults to the latest month with data (clock-free and
    deterministic; the endpoint passes the month the client is viewing).
    Empty months are included — metric_fn returns its own zero-state — so a
    chart gets a continuous monthly axis rather than gaps. Private (leading
    underscore) on purpose: it takes a callable, not just `db`, so it is not
    one of the tripwire's income-ignoring aggregates; the *_trend functions
    that wrap it declare their own tripwire status.
    """
    if anchor is None:
        anchor = _latest_data_month(db)
    if anchor is None:
        return []  # no data at all — an empty series, not a window of zeros
    return [{"month": month, **metric_fn(db, month)}
            for month in _month_window(anchor, months_back)]


def income_trend(db, months_back=6, anchor=None):
    """Per-month income vs. net spend over a trailing window — the analytics
    chart's data source (INCOME-DESIGN). Each entry is `income_summary` for
    that month, so every field means exactly what it does on the dashboard
    card and refund netting flows through `month_spend_cents`/
    `net_cash_flow_cents` identically. EXEMPT in the derivation tripwire for
    the same reason income_summary is — counting inflows is its whole job."""
    return _monthly_series(db, income_summary, months_back, anchor)


def category_trend(db, category, months_back=6, anchor=None):
    """Per-month NET spend for one category over a trailing window, with a
    trailing 3-month rolling average and month-over-month delta (increment
    8, the first deeper-analytics brick).

    Rides `_monthly_series` (increment 6's engine) over `spending_summary`
    — the "pass a different metric_fn" pattern — so refund netting applies
    to this category exactly as it does everywhere else: a refund tagged to
    this category dips this line in the month it lands, and the dip is not
    clamped. Rolling average uses `round_ratio` (integer cents, ties to
    even) over the trailing up-to-3 in-window months, so it warms up over
    the first two; MoM delta is the exact difference from the prior in-window
    month (None for the first, which has no in-window predecessor).

    EXEMPT in the tripwire for the same reason `spending_summary` is — it
    reads inflows on purpose (refund netting, via that function), bounded to
    refunds only; and it needs a `category` argument, so it is not a bare
    db-aggregate the tripwire could call with just `db`.
    """
    def month_metric(conn, month):
        by_cat = spending_summary(conn, month)[month]["by_category"]
        entry = next((c for c in by_cat if c["category"] == category), None)
        return {"spend_cents": entry["amount_cents"] if entry else 0}

    series = _monthly_series(db, month_metric, months_back, anchor)
    for i, entry in enumerate(series):
        window = [e["spend_cents"] for e in series[max(0, i - 2):i + 1]]
        entry["rolling_avg_cents"] = round_ratio(sum(window), len(window))
        entry["mom_delta_cents"] = (
            None if i == 0 else entry["spend_cents"] - series[i - 1]["spend_cents"])
    return series


def anomaly_flags(db, month=None, threshold_pct=50, min_delta_cents=2000):
    """Categories spending unusually high THIS month vs their recent norm
    (analytics Tier B #15): flag a category whose `month` spend is at least
    `threshold_pct`% above its trailing 3-month average (the 3 months BEFORE
    `month` — an EXCLUSIVE baseline, so it's 'vs recent norm', not a dampened
    self-average). A passive 'heads-up', not an authority.

    Two guards against noise: the baseline must be positive, and the absolute
    jump must clear `min_delta_cents` (default $20) — so a tiny category going
    $2→$8 (300%!) doesn't spam a flag. pct_over is integer, ties-even
    (`round_ratio`), float-free.

    Reads spending_summary, so category spend is refund-NETTED — EXEMPT in the
    tripwire for the same bounded reason category_trend/spending_summary are (it
    reads refund inflows on purpose; a refund can legitimately clear an anomaly).
    `month` defaults to the latest data month (deterministic, clock-free, so
    it's callable bare); the endpoint passes the viewed month. Integer cents;
    biggest overage first."""
    if month is None:
        month = _latest_data_month(db)
    if month is None:
        return []
    window = _month_window(month, 4)            # 3 baseline months + `month`
    baseline_months, current_month = window[:3], window[3]
    spend = {}                                  # category -> {ym: cents}
    for m in window:
        for c in spending_summary(db, m)[m]["by_category"]:
            spend.setdefault(c["category"], {})[m] = c["amount_cents"]
    out = []
    for category, by_month in spend.items():
        current = by_month.get(current_month, 0)
        baseline = round_ratio(
            sum(by_month.get(m, 0) for m in baseline_months), len(baseline_months))
        delta = current - baseline
        if baseline <= 0 or delta < min_delta_cents:
            continue
        pct_over = round_ratio(delta * 100, baseline)
        if pct_over < threshold_pct:
            continue
        out.append({
            "category": category, "month": current_month,
            "current_cents": current, "baseline_cents": baseline,
            "delta_cents": delta, "pct_over": pct_over,
        })
    out.sort(key=lambda a: a["pct_over"], reverse=True)
    return out


def bill_variance(db, period=None):
    """Defined bill amount vs what was actually paid, per active bill, for a
    period (analytics #12): each bill's defined amount, the actual amount of
    the transaction that paid it this period (bill_payments → transactions),
    and the variance (actual − defined; positive = over budget). Unpaid bills
    report actual/variance = None. Surfaces 'Electric was $12 over its usual
    $90'.

    Bill payments are OUTFLOWS, so this never reads inflows — NOT
    tripwire-exempt. `period` defaults to None (which matches no payment, so
    every bill reads unpaid) purely so the tripwire can call it with just
    `db`; real callers always pass a period."""
    rows = db.execute(
        """SELECT b.id, b.name, b.amount_cents AS defined_cents, b.due_day,
                  b.category, t.amount_cents AS actual_cents
           FROM bills b
           LEFT JOIN bill_payments bp ON bp.bill_id = b.id AND bp.period = ?
           LEFT JOIN transactions t ON t.id = bp.txn_id
           WHERE b.active = 1
           ORDER BY b.due_day, b.name""", (period,)).fetchall()
    out = []
    for r in rows:
        actual = r["actual_cents"]
        out.append({
            "bill_id": r["id"], "name": r["name"], "due_day": r["due_day"],
            "category": r["category"],
            "defined_cents": r["defined_cents"],
            "actual_cents": actual,
            "variance_cents": None if actual is None else actual - r["defined_cents"],
            "paid": actual is not None,
        })
    return out


def cash_flow_forecast(db, period=None):
    """Projected month-end NET CASH FLOW for `period` — a conservative floor
    (analytics Tier B #14): where this month lands if the only thing left to
    happen is the bills still unpaid.

    net_so_far = income_summary(period).net_cash_flow (true income − spend,
    month-to-date, refund-netted). bills_remaining = this period's active bills
    NOT yet paid (from bill_variance) — the known upcoming OUTFLOWS.
    projected_net = net_so_far − bills_remaining.

    Deliberately a FLOOR, and honest about its limits:
    - It does NOT assume any not-yet-landed paycheck (income cadence isn't
      modelled — income_rules don't encode it), so real month-end is usually
      BETTER than this. Bills-only was the design call.
    - It is NET FLOW (in − out), NOT an account balance — the app tracks no cash
      balance.
    - Goals are excluded: contributions aren't scheduled, so nothing to project.

    EXEMPT in the derivation tripwire like income_summary — net cash flow counts
    income by construction; the bills half is separately guarded (bill_variance
    is itself tripwire-checked). Integer cents; the endpoint renders
    {cents, display}. `period` defaults to None (all-time, matching
    income_summary) purely so it's callable bare; real callers pass a month."""
    inc = income_summary(db, period)
    net_so_far = inc["net_cash_flow_cents"]
    remaining = sorted((b for b in bill_variance(db, period) if not b["paid"]),
                       key=lambda b: (b["due_day"], b["name"]))
    bills_remaining_cents = sum(b["defined_cents"] for b in remaining)
    return {
        "period": period,
        "income_so_far_cents": inc["true_income_cents"],
        "spend_so_far_cents": inc["month_spend_cents"],
        "net_so_far_cents": net_so_far,
        "bills_remaining": [{"bill_id": b["bill_id"], "name": b["name"],
                             "due_day": b["due_day"],
                             "amount_cents": b["defined_cents"]} for b in remaining],
        "bills_remaining_cents": bills_remaining_cents,
        "projected_net_cents": net_so_far - bills_remaining_cents,
    }


def goal_pace(db, as_of=None):
    """Per-goal completion-date projection at the LIFETIME-AVERAGE contribution
    rate (analytics Tier B #16): net saved ÷ days since the first contribution
    (measured to `as_of`) → a daily rate → the date the remaining amount is
    covered, compared against the goal's target_date.

    `amount_cents` is signed, so `saved` is NET of withdrawals and the rate
    reflects real progress. Float-free: days-to-go and the monthly rate use
    `round_ratio`. Status per goal:
      - complete   — saved ≥ target already
      - on_track   — has a target_date and projected ≤ it
      - behind     — has a target_date and projected is later
      - projected  — no target_date, just a projected date
      - no_pace    — can't project (no net progress, no contributions, or the
                     contribution span is under a day) — projected_date null
    `as_of` defaults to the latest contribution date (deterministic, clock-free,
    so it's callable bare and the tripwire can run it); the endpoint passes
    today. Reads goals + goal_contributions only — never transactions — so an
    inflow can't touch it (NOT tripwire-exempt; it passes trivially). Money is
    integer cents; the endpoint renders {cents, display}."""
    if as_of is None:
        row = db.execute("SELECT MAX(c_date) AS d FROM goal_contributions").fetchone()
        as_of = (row["d"] or "")[:10] or None
    ref = date.fromisoformat(as_of) if as_of else None
    goals = db.execute(
        "SELECT id, name, target_cents, target_date FROM goals "
        "ORDER BY created_at, id").fetchall()
    out = []
    for g in goals:
        contribs = db.execute(
            "SELECT amount_cents, c_date FROM goal_contributions "
            "WHERE goal_id = ? ORDER BY c_date", (g["id"],)).fetchall()
        saved = sum(c["amount_cents"] for c in contribs)
        target = g["target_cents"]
        remaining = target - saved
        entry = {
            "goal_id": g["id"], "name": g["name"], "target_cents": target,
            "saved_cents": saved, "remaining_cents": max(0, remaining),
            "target_date": g["target_date"], "monthly_rate_cents": None,
            "projected_date": None, "status": None,
        }
        if remaining <= 0:
            entry["status"] = "complete"
        elif saved <= 0 or not contribs or ref is None:
            entry["status"] = "no_pace"          # no net progress to extrapolate
        else:
            span_days = (ref - date.fromisoformat(contribs[0]["c_date"][:10])).days
            if span_days < 1:
                entry["status"] = "no_pace"       # not a measurable period yet
            else:
                days_to_go = round_ratio(remaining * span_days, saved)
                entry["monthly_rate_cents"] = round_ratio(saved * 30, span_days)
                entry["projected_date"] = (ref + timedelta(days=days_to_go)).isoformat()
                entry["status"] = (
                    "projected" if not g["target_date"]
                    else "on_track" if entry["projected_date"] <= g["target_date"][:10]
                    else "behind")
        out.append(entry)
    return out


def member_breakdown(db, month=None):
    """Per-member shared-expense breakdown for a month (analytics #11): how
    much each person PAID for shared expenses (fronted the money) versus
    their OWED fair share (from basis-point splits), and the net
    (paid − owed; positive = they're owed). Complements `compute_balance`,
    which gives only the single net pair, with the per-person composition —
    and the nets sum to zero, same conservation the balance rests on.

    Shared OUTFLOWS only: inflows never carry split rows and the query
    filters `direction='out'`, so income can't touch this — NOT
    tripwire-exempt, and the tripwire proves an added inflow leaves it
    unchanged (`month` defaults so it's callable with just `db`). Uses
    `round_ratio` per row, matching `compute_balance`'s exact cent rounding.
    Active members only, like the balance."""
    members = db.execute(
        "SELECT id, username, display_name FROM members WHERE active = 1 ORDER BY id"
    ).fetchall()
    paid = {m["id"]: 0 for m in members}
    owed = {m["id"]: 0 for m in members}
    clause = " AND substr(t.txn_date, 1, 7) = ?" if month is not None else ""
    params = (month,) if month is not None else ()
    rows = db.execute(
        """SELECT t.id AS tid, t.amount_cents, t.paid_by, s.member_id, s.share_bp
           FROM transactions t JOIN splits s ON s.transaction_id = t.id
           WHERE t.direction = 'out'""" + clause, params).fetchall()
    counted = set()
    for r in rows:
        if r["member_id"] in owed:
            owed[r["member_id"]] += round_ratio(r["amount_cents"] * r["share_bp"], 10000)
        if r["tid"] not in counted:
            counted.add(r["tid"])
            if r["paid_by"] in paid:
                paid[r["paid_by"]] += r["amount_cents"]
    return [{
        "member_id": m["id"], "username": m["username"], "name": m["display_name"],
        "paid_cents": paid[m["id"]], "owed_cents": owed[m["id"]],
        "net_cents": paid[m["id"]] - owed[m["id"]],
    } for m in members]


def top_merchants(db, month=None, limit=10):
    """Top spending destinations by description (merchant), largest first —
    the axis category totals don't give you ('who did we pay the most?').
    Outflows only, settlements excluded; `month` None means all-time.

    Deliberately NOT netted against refunds: merchant grouping is a
    different axis than category, and a refund's bank description ('Amazon
    refund') rarely matches its purchase ('AMZN Mktp'), so netting here
    would mislead. Because it reads outflows ONLY — no inflow of any type
    touches it — it is NOT tripwire-exempt: adding an inflow must leave it
    unchanged, and the tripwire proves it (which is why `month`/`limit` have
    defaults — the tripwire calls it with just `db`)."""
    clause = " AND substr(txn_date, 1, 7) = ?" if month is not None else ""
    params = ([month] if month is not None else []) + [limit]
    rows = db.execute(
        f"""SELECT description, SUM(amount_cents) AS total, COUNT(*) AS n
            FROM transactions
            WHERE direction = 'out' AND source != 'settlement'{clause}
            GROUP BY description
            ORDER BY total DESC, description
            LIMIT ?""", params).fetchall()
    return [{"description": r["description"], "amount_cents": r["total"],
             "count": r["n"]} for r in rows]


# ---- recurring-charge / subscription detection (analytics Tier B #13) -------
# Bank descriptions wrap the real merchant in noise that VARIES between two
# charges from the same place — auth codes, store numbers, card masks, ACH
# metadata, a trailing city/state. These strip that so the same merchant lands
# on one key. Order matters (masks/urls before the generic digit/punct sweep).
_MERCHANT_ACH_TAIL = re.compile(r"\b(?:DES|INDN)\b.*$")          # "DES:.. INDN:.." ACH metadata (+ payer name)
_MERCHANT_MASK = re.compile(r"X{3,}[\dX#]*")                     # XXXXX3462XXXX card masks
_MERCHANT_URL = re.compile(r"\b(?:WWW\.|HTTPS?://)\S+")          # WWW.* / http* (NOT bare NETFLIX.COM — that's the merchant)
_MERCHANT_STARID = re.compile(r"\*\s*[A-Z0-9]{5,}")             # "* O21CE1G63" txn ids
_MERCHANT_AUTH = re.compile(r"\bAP\s+\d+\b")                     # "AP 405524" auth codes
_MERCHANT_LEAD = re.compile(
    r"^(?:VISA\s+DDA\s+PUR|DDA\s+PURCHASE|CHECKCARD|CHECK\s?CARD|"
    r"PURCHASE|POS\s+DEBIT|DEBIT|VISA|DD)\b")                    # leading transaction-type prefix
_MERCHANT_STATE_TAIL = re.compile(r"\s\*?\s*[A-Z]{2}\s*$")       # trailing "* NJ" / " NY"
_MERCHANT_FILLER = {"AP", "PUR", "DDA", "POS", "DEBIT", "CO", "COM", "NET", "ORG"}


def _normalize_merchant(description):
    """Reduce a noisy bank description to a stable merchant key for clustering.

    Conservative by design: it errs toward UNDER-merging (two same-merchant
    descriptions may fail to collapse if they differ a lot) rather than
    OVER-merging (fabricating a shared merchant), because a false merge is what
    would invent a phantom 'subscription'. Clean recurring descriptors
    (NETFLIX.COM, SPOTIFY) normalize reliably; very noisy retail may not — fine,
    retail isn't a subscription. Returns '' when nothing meaningful survives."""
    s = (description or "").upper()
    s = _MERCHANT_ACH_TAIL.sub(" ", s)
    s = _MERCHANT_MASK.sub(" ", s)
    s = _MERCHANT_URL.sub(" ", s)
    s = _MERCHANT_STARID.sub(" ", s)
    s = _MERCHANT_AUTH.sub(" ", s)
    s = _MERCHANT_LEAD.sub(" ", s)
    s = _MERCHANT_STATE_TAIL.sub(" ", s)
    s = re.sub(r"[^A-Z ]+", " ", s)   # drop digits & punctuation
    tokens = [t for t in s.split() if len(t) > 1 and t not in _MERCHANT_FILLER]
    return " ".join(tokens)


def _gaps_regular(gaps, median):
    """True when every consecutive gap is within ±40% of the median — i.e. the
    charge recurs on a genuine rhythm, not three coincidental hits. Integer /
    float-free: 0.6·median ≤ g ≤ 1.4·median ⇔ 3·median ≤ 5·g ≤ 7·median."""
    if median < 3:                     # sub-3-day "cadence" is a flurry, not a subscription
        return False
    return all(3 * median <= 5 * g <= 7 * median for g in gaps)


def _cadence_label(days):
    """A human name for a repeat interval, or 'every ~N days' when it fits no
    common band. Bands are generous so real-world jitter still reads naturally."""
    for lo, hi, name in ((5, 9, "weekly"), (12, 16, "biweekly"), (26, 35, "monthly"),
                         (58, 66, "bimonthly"), (84, 96, "quarterly"),
                         (170, 195, "semiannually"), (350, 385, "yearly")):
        if lo <= days <= hi:
            return name
    return f"every ~{days} days"


def recurring_charges(db, min_occurrences=3):
    """Likely recurring charges / subscriptions (analytics Tier B #13): outflows
    that repeat at a stable AMOUNT on a regular CADENCE. A *suggestion* surface,
    never an authority — a coincidence must not silently become a "subscription"
    (the same honesty as INCOME-DESIGN's refused transfer auto-pairing), so the
    bar is deliberately high: same normalized merchant + identical amount + ≥
    min_occurrences (default 3) charges on distinct days + gaps regular to ±40%.

    Clustering by (merchant, EXACT amount) is the conservative choice — a
    merchant with variable amounts (retail) never forms a same-amount cluster,
    and a price change simply splits into two clusters rather than smearing one.
    Reports the detected interval and a cadence label (any rhythm, not just
    monthly) plus the next expected date. Clock-free like restock_forecast — no
    "today"; days-until framing is the view layer's job.

    OUTFLOWS ONLY, settlements excluded (like top_merchants) — so it's NOT
    tripwire-exempt: an inflow must leave it unchanged, and the tripwire proves
    it. Reads transactions only; never touches money. Most-recent activity
    first."""
    rows = db.execute(
        "SELECT txn_date, amount_cents, description FROM transactions "
        "WHERE direction = 'out' AND source != 'settlement'").fetchall()
    buckets = {}
    for r in rows:
        key = _normalize_merchant(r["description"])
        if not key:
            continue
        buckets.setdefault((key, r["amount_cents"]), []).append(r)
    out = []
    for (key, amount_cents), occ in buckets.items():
        days = sorted({o["txn_date"][:10] for o in occ})   # distinct days
        if len(days) < min_occurrences:
            continue
        dates = [date.fromisoformat(d) for d in days]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        interval = _median_int(gaps)
        if not _gaps_regular(gaps, interval):
            continue
        out.append({
            "merchant": key.title(),
            "example_description": occ[-1]["description"],
            "amount_cents": amount_cents,
            "occurrences": len(days),
            "interval_days": interval,
            "cadence": _cadence_label(interval),
            "first_charge": dates[0].isoformat(),
            "last_charge": dates[-1].isoformat(),
            "predicted_next": (dates[-1] + timedelta(days=interval)).isoformat(),
        })
    out.sort(key=lambda c: (c["last_charge"], c["amount_cents"]), reverse=True)
    return out


def savings_rate_trend(db, months_back=6, anchor=None):
    """Savings rate over a trailing window (analytics #9). Two rates per
    month: the raw single-month `savings_rate` (net_cash_flow / true_income,
    straight from `income_summary`) and a trailing 3-month
    `rolling_savings_rate` that smooths the noise — one big purchase tanks a
    single month but shouldn't read as "we stopped saving." The rolling rate
    is cumulative, not an average of ratios: Σ net_cash_flow ÷ Σ true_income
    over the up-to-3 in-window months, which weights months by income the
    way a household actually experiences it.

    Reuses `income_trend` (which rides `_monthly_series` over
    `income_summary`), so no aggregate is recomputed — the per-month rate is
    byte-identical to the dashboard card's. Both rates are display ratios
    (the documented float exception), None when the relevant income is 0.
    EXEMPT in the tripwire, like `income_trend`."""
    series = income_trend(db, months_back, anchor)
    out = []
    for i, entry in enumerate(series):
        window = series[max(0, i - 2):i + 1]
        income_sum = sum(x["true_income_cents"] for x in window)
        netflow_sum = sum(x["net_cash_flow_cents"] for x in window)
        out.append({
            "month": entry["month"],
            "savings_rate": entry["savings_rate"],
            "rolling_savings_rate": (None if income_sum == 0
                                     else round(netflow_sum / income_sum, 4)),
        })
    return out


# ─────────────────────── inventory (INVENTORY-DESIGN, the pantry) ─────────
# Read-time views over the items table. They never read transactions, so they
# are trivially inflow-invariant (the derivation tripwire covers them for free)
# and never touch money.

_ITEM_STATUS_ORDER = "CASE status WHEN 'out' THEN 0 WHEN 'low' THEN 1 ELSE 2 END"


def shopping_list(db):
    """Everything that needs buying: active staples at 'low'/'out', plus every
    active one-off need. The "what do we need?" read the SPA and the assistant
    both call. Ordered most-urgent (out) first, then by name."""
    rows = db.execute(
        "SELECT * FROM items WHERE active = 1 AND ("
        "  (kind = 'staple' AND status IN ('low', 'out')) OR kind = 'oneoff'"
        f") ORDER BY {_ITEM_STATUS_ORDER}, name COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


def low_stock(db):
    """Active staples running low or out — the nudge / badge source (distinct
    from shopping_list, which also includes one-off needs)."""
    rows = db.execute(
        "SELECT * FROM items WHERE active = 1 AND kind = 'staple' "
        f"AND status IN ('low', 'out') ORDER BY {_ITEM_STATUS_ORDER}, name COLLATE NOCASE"
    ).fetchall()
    return [dict(r) for r in rows]


def _item_match_phrase(item):
    """The phrase a staple's purchases are matched against: its explicit
    restock_match override, else its own name (the auto-guess). '' if neither —
    the caller skips those (nothing to match on)."""
    return (item["restock_match"] or item["name"] or "").strip()


def _matching_purchases(db, item, since=None):
    """Every OUTFLOW purchase whose description contains the item's match phrase
    (restock_match, or the name when unset), most-recent first. `since` (a
    'YYYY-MM-DD') limits to purchases dated on/after it. Empty when the item has
    no match phrase. OUTFLOWS ONLY (direction='out') — the mandatory filter both
    restock derivations share, so an inflow that happens to name the item never
    counts. A plumbing helper (leading underscore): it takes an `item`, not just
    `db`, so it is not one of the tripwire's income-ignoring aggregates."""
    phrase = _item_match_phrase(item)
    if not phrase:
        return []
    sql = ("SELECT txn_date, description, amount_cents FROM transactions "
           "WHERE direction = 'out' AND instr(lower(description), lower(?)) > 0")
    params = [phrase]
    if since:
        sql += " AND txn_date >= ?"
        params.append(since)
    sql += " ORDER BY txn_date DESC, id DESC"
    return db.execute(sql, params).fetchall()


def _median_int(values):
    """Median of a non-empty list of non-negative ints, as an int. Even count:
    the two middle averaged, ties-to-even, float-free (round_ratio) — same
    rounding discipline as the money derivations."""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return round_ratio(ordered[mid - 1] + ordered[mid], 2)


def restock_suggestions(db):
    """Purchase-feed restock hints (INVENTORY-DESIGN step 5): each staple that
    is low or out AND has a matching purchase since it ran low — a strong hint
    you restocked, offered to CONFIRM (never auto-applied: buying it after it
    ran low is evidence, not proof — "suggest, don't assert").

    A staple matches a purchase when the purchase's description contains the
    staple's restock_match phrase, or — when that's unset — the staple's own
    name (the auto-guess), case-insensitive. "Since it ran low" = the purchase
    dated on/after the day the item last changed (its updated_at), so a purchase
    that predates the item going low doesn't count (you bought it, used it, THEN
    it ran low). One suggestion per staple, carrying the most recent matching
    purchase as evidence, most-urgent (out before low) first.

    OUTFLOWS ONLY (via _matching_purchases), so an inflow whose description
    happens to contain an item name never fabricates a suggestion — a mandatory
    filter the derivation tripwire covers. Reads items + transactions; never
    touches money.
    """
    staples = db.execute(
        "SELECT * FROM items WHERE active = 1 AND kind = 'staple' "
        "AND status IN ('low', 'out')").fetchall()
    out = []
    for it in staples:
        since = (it["updated_at"] or "")[:10]  # the day it last changed
        rows = _matching_purchases(db, it, since=since)
        if not rows:
            continue
        row = rows[0]  # most recent matching purchase (helper orders DESC)
        out.append({
            "item_id": it["id"], "name": it["name"], "status": it["status"],
            "matched_by": "phrase" if it["restock_match"] else "name",
            "purchase": {"date": row["txn_date"], "description": row["description"],
                         "amount_cents": row["amount_cents"]},
        })
    out.sort(key=lambda s: s["purchase"]["date"], reverse=True)  # recent first
    out.sort(key=lambda s: 0 if s["status"] == "out" else 1)     # out before low
    return out


def restock_forecast(db, min_purchases=3):
    """Cadence-based restock prediction (INVENTORY-DESIGN step 5, second half):
    for each tracked staple with a repeat purchase history, project WHEN it will
    next be needed from the typical gap between its purchases — a forward
    heads-up beyond the reactive low/out hint restock_suggestions gives.

    A staple qualifies only with >= min_purchases (default 3) matching purchases
    on DISTINCT days (i.e. 2+ intervals), so a single coincidental gap can't
    drive a forecast — the conservative "suggest, don't assert" bar. The
    interval is the MEDIAN gap (in days) between consecutive purchase dates
    (robust to one unusually early/late buy); predicted_date = the last purchase
    + that interval.

    Deliberately CLOCK-FREE: it returns only facts derived from the purchase
    history (interval, last_purchase, predicted_date) and never reads a "today".
    The days-until / overdue framing is a presentation concern computed at the
    view layer against the client's real date — which also keeps this an inflow-
    INSENSITIVE aggregate (its answer can't move when an inflow lands), so the
    derivation tripwire covers it with no exemption. (This is why it doesn't ride
    _monthly_series as the design doc's rough sketch guessed: cadence is about
    intervals between individual purchases, not monthly buckets.)

    OUTFLOWS ONLY via _matching_purchases. Includes staples of any status (the
    UI decides what to surface — low/out ones already appear in the shopping
    list); soonest-due first. Reads items + transactions; never touches money.
    """
    staples = db.execute(
        "SELECT * FROM items WHERE active = 1 AND kind = 'staple'").fetchall()
    out = []
    for it in staples:
        rows = _matching_purchases(db, it)
        # distinct purchase DAYS, chronological — two buys on one day are one
        # restock event, not a zero-length interval.
        days = sorted({r["txn_date"][:10] for r in rows})
        if len(days) < min_purchases:
            continue
        dates = [date.fromisoformat(d) for d in days]
        interval = _median_int([(b - a).days for a, b in zip(dates, dates[1:])])
        predicted = dates[-1] + timedelta(days=interval)
        out.append({
            "item_id": it["id"], "name": it["name"], "status": it["status"],
            "purchases_seen": len(days),
            "interval_days": interval,
            "last_purchase": dates[-1].isoformat(),
            "predicted_date": predicted.isoformat(),
        })
    out.sort(key=lambda f: f["predicted_date"])  # soonest-due first
    return out


def new_staple_suggestions(db, min_purchases=3):
    """New-staple suggestions (INVENTORY-DESIGN step 5 sibling): frequently-bought
    merchants you don't yet track — an offer to START tracking one as a staple,
    the discovery counterpart to restock_suggestions/restock_forecast (which act
    on staples you ALREADY track).

    Clusters outflows by normalized merchant (the same _normalize_merchant the
    subscription detector uses) and surfaces a cluster bought on >= min_purchases
    (default 3) DISTINCT days — the conservative "suggest, don't assert" bar, so
    a one-off spending spree never becomes a suggestion. Two exclusions keep the
    list honest and short:
      * merchants already represented by an active tracked item (staple OR
        one-off) — matched on the item's restock_match phrase, or its name when
        unset, normalized the same way — so nothing you already track is
        re-offered; and
      * merchants recurring_charges already flags as fixed-amount recurring
        subscriptions (a streaming service, a gym) — those are surfaced as
        subscriptions and aren't pantry staples.

    Clock-free like restock_forecast: it reports only history-derived facts
    (first/last purchase, count, total spent) and never reads a "today", so it
    stays an inflow-insensitive aggregate the derivation tripwire covers with no
    exemption. OUTFLOWS ONLY, settlements excluded (like recurring_charges /
    top_merchants) — an inflow that happens to name a merchant never fabricates a
    suggestion. Reads transactions + items; never touches money. Most-bought
    first, then most-recent.

    Honest caveat inherited from step 5: SimpleFIN gives the MERCHANT, not the
    product, so this finds merchant-identifiable habits (a pet store, a coffee
    shop) and can't isolate one grocery item inside a supermarket run.
    """
    rows = db.execute(
        "SELECT txn_date, amount_cents, description FROM transactions "
        "WHERE direction = 'out' AND source != 'settlement'").fetchall()
    buckets = {}
    for r in rows:
        key = _normalize_merchant(r["description"])
        if not key:
            continue
        buckets.setdefault(key, []).append(r)

    # Merchants already tracked (any active item) or already known to be a
    # fixed-amount subscription — excluded so we only ever offer genuinely-new
    # pantry staples.
    tracked = {
        _normalize_merchant(_item_match_phrase(it))
        for it in db.execute(
            "SELECT name, restock_match FROM items WHERE active = 1").fetchall()
    }
    tracked.discard("")
    subscriptions = {c["merchant"] for c in recurring_charges(db)}

    out = []
    for key, occ in buckets.items():
        if key in tracked or key.title() in subscriptions:
            continue
        days = sorted({o["txn_date"][:10] for o in occ})
        if len(days) < min_purchases:
            continue
        # most-recent purchase as the example (deterministic tiebreak on desc)
        example = sorted(occ, key=lambda o: (o["txn_date"], o["description"]))[-1]
        out.append({
            "merchant": key.title(),
            "example_description": example["description"],
            "purchases_seen": len(days),
            "first_purchase": days[0],
            "last_purchase": days[-1],
            "total_spent_cents": sum(o["amount_cents"] for o in occ),
            "suggested_match": key.lower(),
        })
    out.sort(key=lambda s: (s["purchases_seen"], s["last_purchase"]), reverse=True)
    return out


def unmatched_staples(db):
    """Broken-match detector (INVENTORY-DESIGN step 5 sibling): tracked staples
    whose match phrase has NEVER matched a single purchase in the feed.

    The match phrase (restock_match, or the item name when unset) is the linchpin
    of every purchase inference — restock_suggestions, restock_forecast, and the
    predicted-low nudge all bottom out in _matching_purchases — so a staple with a
    wrong or too-specific phrase silently gets no hints, forever, and nobody would
    know to look. This surfaces those staples so the human can fix (or clear) the
    phrase, making the whole matching layer self-correcting.

    A prompt to REVIEW, not an assertion the phrase is wrong: a staple can
    legitimately have zero matches because it's bought inside grocery runs where
    SimpleFIN sees the supermarket, not the product (the step-5 merchant-not-
    product limit). The UI copy invites a look; the fix is optional.

    Clock-free like restock_forecast: it reports each zero-match staple with the
    date tracking began (created_at) and never reads a "today"; the "tracked long
    enough to expect a match" grace period is a view-layer concern applied against
    the client's date. Counting matching OUTFLOWS via _matching_purchases, it is
    inflow-insensitive — an inflow whose description happens to contain the phrase
    never counts as a match, so a broken staple stays surfaced — a property the
    derivation tripwire covers with no exemption. Reads items + transactions;
    never touches money. Oldest-tracked first (longest unmatched needs it most).
    """
    staples = db.execute(
        "SELECT * FROM items WHERE active = 1 AND kind = 'staple'").fetchall()
    out = []
    for it in staples:
        if _matching_purchases(db, it):     # any matching purchase ever → it works
            continue
        out.append({
            "item_id": it["id"], "name": it["name"],
            "restock_match": it["restock_match"],   # None → matching on the name
            "matched_by": "phrase" if it["restock_match"] else "name",
            "tracked_since": (it["created_at"] or "")[:10],
        })
    out.sort(key=lambda s: s["tracked_since"])
    return out
