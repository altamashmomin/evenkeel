#!/usr/bin/env python3
"""Synthetic income fixture for Ledger tests and rehearsals — the income
build's counterpart to seed_db.py.

Why this is a separate script rather than a seed_db.py flag: seed_db.py
freezes the deployed v1.0 DDL verbatim (no direction/income_type columns
exist at the point it runs) so it stays an honest mirror of what the Pi
runs today; migrate.py apply is a deliberate separate step that carries a
fixture from that v1.0 shape to the current schema. Income rows can only
be inserted once direction/income_type exist, i.e. after migration — so
this script runs third: seed_db.py, then migrate.py apply, then this.

Usage:
    python seed_db.py dev.db --seed 42 --months 6 --as-of 2026-07-19
    python migrate.py apply dev.db
    python seed_income.py dev.db --seed 42 --months 6 --as-of 2026-07-19

Never real data; deterministic for a given --seed so runs are
reproducible. Rows are inserted directly (bypassing record_transaction),
same as seed_db.py's own philosophy — fixtures are data, not verb calls.
Every row lands income_type='unclassified': that's the honest state of
a freshly-synced inflow before any rule or human has looked at it.

Refuses finance.db and refuses a database that isn't fully migrated
(no direction column to write to).
"""
import argparse
import os
import random
import sys
from datetime import date, timedelta

from schema_runtime import SchemaNotReady, connect_existing, require_current_schema

PAYCHECK_DESCRIPTIONS = ["ADP PAYROLL", "Employer Direct Deposit", "Payroll Co Inc"]
GIFT_DESCRIPTIONS = ["Venmo from Sam", "Birthday gift", "Cash App transfer"]
REFUND_DESCRIPTIONS = ["Amazon Refund", "REI Return Credit", "Airline Travel Credit"]


def month_starts(months_back, as_of):
    first_of_this = as_of.replace(day=1)
    starts = []
    y, m = first_of_this.year, first_of_this.month
    for _ in range(months_back):
        starts.append(date(y, m, 1))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return sorted(starts)


def seed_income(conn, rng, months_back, as_of, rows_per_month):
    member_ids = [r[0] for r in conn.execute(
        "SELECT id FROM members WHERE active = 1 ORDER BY id")]
    if not member_ids:
        sys.exit("error: no active members in this database — seed_db.py's "
                 "fixture (or migrate.py apply) hasn't run yet")

    n = 0
    for start in month_starts(months_back, as_of):
        days_in_month = ((start.replace(day=28) + timedelta(days=4)).replace(day=1)
                         - timedelta(days=1)).day

        # A paycheck or two, split across the household's earners.
        for _ in range(rows_per_month):
            txn_day = start.replace(day=rng.randint(1, min(15, days_in_month)))
            if txn_day > as_of:
                continue
            payer = rng.choice(member_ids)
            desc = rng.choice(PAYCHECK_DESCRIPTIONS)
            cents = rng.randint(240000, 420000)
            external_id = f"simplefin:acct-{payer}:{start.strftime('%Y-%m')}-pay-{n}"
            conn.execute(
                """INSERT INTO transactions
                   (txn_date, amount_cents, description, category, paid_by,
                    is_shared, source, external_id, direction, income_type)
                   VALUES (?, ?, ?, 'Other', ?, 0, 'simplefin', ?, 'in', 'unclassified')""",
                (txn_day.isoformat(), cents, desc, payer, external_id))
            n += 1

        # An occasional smaller gift or refund — the messier, less
        # regular inflow shape that rules are less likely to have caught.
        if rng.random() < 0.3:
            txn_day = start.replace(day=rng.randint(1, days_in_month))
            if txn_day <= as_of:
                payer = rng.choice(member_ids)
                is_refund = rng.random() < 0.5
                desc = rng.choice(REFUND_DESCRIPTIONS if is_refund else GIFT_DESCRIPTIONS)
                cents = rng.randint(1500, 15000)
                source = "simplefin" if is_refund else "manual"
                external_id = (f"simplefin:acct-{payer}:{start.strftime('%Y-%m')}-misc-{n}"
                               if source == "simplefin" else None)
                conn.execute(
                    """INSERT INTO transactions
                       (txn_date, amount_cents, description, category, paid_by,
                        is_shared, source, external_id, direction, income_type)
                       VALUES (?, ?, ?, 'Other', ?, 0, ?, ?, 'in', 'unclassified')""",
                    (txn_day.isoformat(), cents, desc, payer, source, external_id))
                n += 1
    return n


def main():
    ap = argparse.ArgumentParser(
        description="Synthetic income fixture — run after seed_db.py + migrate.py apply")
    ap.add_argument("path")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--months", type=int, default=8)
    ap.add_argument(
        "--as-of", type=date.fromisoformat, default=date.today(), metavar="YYYY-MM-DD",
        help="effective date for the fixture (tests must pass this explicitly)")
    ap.add_argument(
        "--rows-per-month", type=int, default=2,
        help="paycheck-shaped inflows per month, before the occasional gift/refund")
    args = ap.parse_args()

    # Case-insensitive + symlink-resolving (CODE-REVIEW-2026-08-07 #10).
    if os.path.basename(os.path.realpath(args.path)).lower() == "finance.db":
        sys.exit("error: refusing to touch finance.db (CLAUDE.md rule 6)")
    try:
        require_current_schema(args.path)
    except SchemaNotReady as e:
        sys.exit(f"error: {e} — run seed_db.py and migrate.py apply first")

    conn = connect_existing(args.path)
    conn.execute("PRAGMA foreign_keys = ON")
    rng = random.Random(args.seed)
    with conn:
        n = seed_income(conn, rng, args.months, args.as_of, args.rows_per_month)
    conn.close()
    print(f"seeded {args.path}: {n} income rows "
          f"(seed={args.seed}, months={args.months}, as_of={args.as_of.isoformat()})")


if __name__ == "__main__":
    main()
