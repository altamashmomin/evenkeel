#!/usr/bin/env python3
"""Synthetic seed database for Ledger tests and rehearsals.

Creates a database with the CURRENT deployed schema (imported straight from
app.py — no duplicated DDL to drift) and fills it with fake people and
plausible fake transactions. Never real data; deterministic for a given
--seed so runs are reproducible.

Usage:
    python seed_db.py <path> [--seed N] [--months N]

Refuses to write to finance.db or over an existing file.
"""
import argparse
import os
import random
import sqlite3
import sys
from datetime import date, timedelta

MEMBERS = [
    ("avery", "Avery"),
    ("blake", "Blake"),
]

MERCHANTS = {
    "Groceries": [("Fresh Mart", 3200, 14500), ("Corner Grocer", 1500, 9000)],
    "Dining": [("Noodle House", 1800, 6500), ("Taqueria Sol", 1200, 4800),
               ("Cafe Lumen", 450, 1600)],
    "Transport": [("Metro Card", 2000, 6000), ("City Fuel", 3000, 7500)],
    "Household": [("Ware & Sons Hardware", 800, 12000), ("SoapBox Supplies", 600, 4500)],
    "Entertainment": [("Cinema Palace", 1400, 4200), ("Vinyl Alley", 900, 5600)],
    "Health": [("Green Cross Pharmacy", 500, 6800)],
    "Pets": [("Whisker Works", 1100, 8900)],
}

BILLS = [
    ("Rent", 185000, 1, "Rent"),
    ("Electric", 9000, 12, "Utilities"),
    ("Internet", 6500, 18, "Internet"),
    ("Phone", 4500, 22, "Phone"),
]


def fake_password_hash(rng):
    # Shaped like a werkzeug hash but pure noise — these accounts can never
    # log in, which is the point of a synthetic fixture.
    salt = "".join(rng.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=16))
    fake = "".join(rng.choices("0123456789abcdef", k=64))
    return f"scrypt:32768:8:1${salt}${fake}"


def month_starts(months_back):
    first_of_this = date.today().replace(day=1)
    starts = []
    y, m = first_of_this.year, first_of_this.month
    for _ in range(months_back):
        starts.append(date(y, m, 1))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return sorted(starts)


def seed(conn, rng, months_back):
    for username, display in MEMBERS:
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash) VALUES (?, ?, ?)",
            (username, display, fake_password_hash(rng)))
    user_ids = [r[0] for r in conn.execute("SELECT id FROM users ORDER BY id")]

    bill_ids = {}
    for name, cents, due_day, category in BILLS:
        cur = conn.execute(
            "INSERT INTO bills (name, amount_cents, due_day, category) VALUES (?, ?, ?, ?)",
            (name, cents, due_day, category))
        bill_ids[name] = cur.lastrowid

    n_txn = 0
    for start in month_starts(months_back):
        period = start.strftime("%Y-%m")
        days_in_month = ((start.replace(day=28) + timedelta(days=4)).replace(day=1)
                         - timedelta(days=1)).day

        # Bills: paid most months, sometimes late, occasionally missed.
        for name, cents, due_day, category in BILLS:
            if rng.random() < 0.05 and name != "Rent":
                continue
            paid_on = start.replace(day=min(due_day + rng.randint(0, 3), days_in_month))
            if paid_on > date.today():
                continue
            payer = rng.choice(user_ids)
            cur = conn.execute(
                """INSERT INTO transactions (txn_date, amount_cents, description,
                       category, paid_by, is_shared, payer_share_pct, source)
                   VALUES (?, ?, ?, ?, ?, 1, 50, 'bill')""",
                (paid_on.isoformat(), cents, f"{name} ({period})", category, payer))
            conn.execute(
                "INSERT INTO bill_payments (bill_id, period, paid_on, txn_id) "
                "VALUES (?, ?, ?, ?)",
                (bill_ids[name], period, paid_on.isoformat(), cur.lastrowid))
            n_txn += 1

        # Card/bank spending, a mix of manual and simplefin rows.
        for _ in range(rng.randint(18, 30)):
            category = rng.choice(list(MERCHANTS))
            merchant, lo, hi = rng.choice(MERCHANTS[category])
            txn_day = start.replace(day=rng.randint(1, days_in_month))
            if txn_day > date.today():
                continue
            cents = rng.randint(lo, hi)
            payer = rng.choice(user_ids)
            shared = 1 if rng.random() < 0.75 else 0
            pct = rng.choice([50, 50, 50, 60, 40, 100]) if shared else 50
            source = "simplefin" if rng.random() < 0.6 else "manual"
            external_id = (f"simplefin:acct-{payer}:{period}-{n_txn}"
                           if source == "simplefin" else None)
            conn.execute(
                """INSERT INTO transactions (txn_date, amount_cents, description,
                       category, paid_by, is_shared, payer_share_pct, source, external_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (txn_day.isoformat(), cents, merchant, category, payer,
                 shared, pct, source, external_id))
            n_txn += 1

        # An occasional settle-up, shaped like the app writes them.
        if rng.random() < 0.4:
            settle_day = start.replace(day=min(27, days_in_month))
            if settle_day <= date.today():
                payer = rng.choice(user_ids)
                conn.execute(
                    """INSERT INTO transactions (txn_date, amount_cents, description,
                           category, paid_by, is_shared, payer_share_pct, source)
                       VALUES (?, ?, 'Settle up', 'Other', ?, 1, 100, 'settlement')""",
                    (settle_day.isoformat(), rng.randint(5000, 60000), payer))
                n_txn += 1

    # Goals with contributions from both people.
    for name, target in (("Emergency fund", 500000), ("Trip to the coast", 220000)):
        cur = conn.execute(
            "INSERT INTO goals (name, target_cents, target_date) VALUES (?, ?, NULL)",
            (name, target))
        goal_id = cur.lastrowid
        for start in month_starts(months_back):
            if rng.random() < 0.7:
                c_day = start.replace(day=min(5, 28))
                if c_day > date.today():
                    continue
                conn.execute(
                    """INSERT INTO goal_contributions
                           (goal_id, user_id, amount_cents, c_date, note)
                       VALUES (?, ?, ?, ?, NULL)""",
                    (goal_id, rng.choice(user_ids), rng.randint(5000, 40000),
                     c_day.isoformat()))
    return n_txn


def main():
    ap = argparse.ArgumentParser(description="Synthetic seed database for Ledger")
    ap.add_argument("path")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--months", type=int, default=8)
    args = ap.parse_args()

    if os.path.basename(args.path) == "finance.db":
        sys.exit("error: refusing to touch finance.db (CLAUDE.md rule 6)")
    if os.path.exists(args.path):
        sys.exit(f"error: {args.path} already exists — delete it first if you "
                 "meant to reseed")

    # Create the schema exactly as the deployed app does: import app.py with
    # DATABASE_PATH pointed at the new file; its init_db() runs on import.
    os.environ["DATABASE_PATH"] = os.path.abspath(args.path)
    import app  # noqa: F401

    conn = sqlite3.connect(args.path)
    conn.execute("PRAGMA foreign_keys = ON")
    rng = random.Random(args.seed)
    with conn:
        n = seed(conn, rng, args.months)
    conn.close()
    print(f"seeded {args.path}: {len(MEMBERS)} users, {n} transactions, "
          f"{len(BILLS)} bills, 2 goals (seed={args.seed}, months={args.months})")


if __name__ == "__main__":
    main()
