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
           WHERE s.member_id != t.paid_by""" + date_clause,
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
    """Outflow totals by month/category, excluding settlement transactions."""
    where = "WHERE source != 'settlement'"
    params = []
    if month is not None:
        where += " AND substr(txn_date, 1, 7) = ?"
        params.append(month)
    rows = db.execute(
        f"""SELECT substr(txn_date, 1, 7) AS month, category,
                   SUM(amount_cents) AS total_cents
            FROM transactions {where}
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
