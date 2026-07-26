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
