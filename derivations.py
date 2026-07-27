"""Canonical read-time financial derivations shared by every surface."""


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
