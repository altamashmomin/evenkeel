#!/usr/bin/env python3
"""Look for rent and utility payments that Ledger may be counting twice.
READ-ONLY: it never writes to the database. It prints what it found and the
fix to make in the app; nothing changes until someone makes that fix.

Usage:
    venv/bin/python find_doubles.py                         # last month to today
    venv/bin/python find_doubles.py --from 2026-10-01 --to 2026-11-02

What it looks for, inside the date window:

  1. A bill paid twice. The bill was marked paid with Mark paid (which logs
     its own transaction), and the bank's own charge for it was also imported
     by sync. Both count as spending. Fix: Calendar, that bill's month, Undo
     (removes only the transaction Mark paid logged; the bank's copy stays),
     then link the bank charge (the "Looks paid" chip, or Mark paid > "Paid
     from the bank? Pick the charge"), so the bill reads paid again.

  2. A person-to-person payment for rent (Venmo, Zelle, Cash App, PayPal)
     that counts as spending. If the rent it covers is already in Ledger (a
     rent payment is listed for that month), the money was counted once there
     and again here. Fix: open it in Activity and turn on Mark as transfer.
     If that month's rent isn't in Ledger any other way, leave it alone.

  3. The same amount twice within a few days on a rent or utility charge,
     where neither is explained by (1). Usually a manual entry next to the
     bank's copy. Listed to look at; there is no automatic fix.

For (1) and (2) it also shows what the fix would do to the money numbers, by
making the fix on a throwaway in-memory copy through the app's own verbs
(unmark_bill_paid + link_bill_payment, or set_transfer) and comparing the
who-owes-whom balance and the monthly spend before and after. The real
database is opened read-only.

Environment (.env), the same as the app and the sync:
    DATABASE_PATH   path to the app's SQLite file (default: ./finance.db)
"""
import argparse
import os
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

import actions
import derivations
from schema_runtime import SchemaNotReady, require_current_schema

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

ACTOR = "cli:find_doubles(dry-run copy)"
SAME_AMOUNT_DAYS = 3          # check 3: same amount within this many days
RENT_SHARE_TOLERANCE_BP = 1000  # check 2: within ±10% of the rent or half of it

P2P = re.compile(r"\b(venmo|zelle|cash\s?app|paypal)\b", re.I)
RENT_WORDS = re.compile(r"\b(rent|avalon|lease|apartment)\b", re.I)
UTILITY_WORDS = re.compile(
    r"\b(public\s?grid|pseg|cottage\s?energy|electric|gas|water|sewer|"
    r"internet|optimum|utilit\w*)\b", re.I)
WATCHED_CATEGORIES = {"rent", "utilities"}


def money(cents):
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def open_readonly(path):
    resolved = Path(path).expanduser().resolve()
    db = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def periods_between(start, end):
    """Every YYYY-MM month the window touches."""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def is_rent_bill(bill):
    return bool(RENT_WORDS.search(bill["name"])) or bill["category"].casefold() == "rent"


def watched(txn):
    """A rent or utility-looking transaction."""
    return (txn["category"].casefold() in WATCHED_CATEGORIES
            or bool(RENT_WORDS.search(txn["description"]))
            or bool(UTILITY_WORDS.search(txn["description"])))


# ---------------------------------------------------------------- check 1

def bill_doubles(db, periods):
    """Bill payments logged by Mark paid (source 'bill') that also have a
    matching imported charge. The match rules are the "Looks paid" chip's
    (derivations.bill_payment_candidates), plus an exact match to what Mark
    paid logged and a ±LOOKS_PAID_DAYS window around the logged date as well
    as the due date (Mark paid dates its transaction the day it's tapped)."""
    out = []
    for period in periods:
        rows = db.execute(
            """SELECT bp.bill_id, bp.period, b.name, b.amount_cents AS usual_cents,
                      b.due_day, t.id AS txn_id, t.txn_date, t.amount_cents,
                      t.description
               FROM bill_payments bp
               JOIN bills b ON b.id = bp.bill_id
               JOIN transactions t ON t.id = bp.txn_id
               WHERE bp.period = ? AND t.source = 'bill' AND b.active = 1
               ORDER BY b.due_day, b.name""", (period,)).fetchall()
        for p in rows:
            logged_day = date.fromisoformat(p["txn_date"])
            due = derivations._clamped_due_date(period, p["due_day"])
            last = db.execute(
                """SELECT t.description FROM bill_payments bp
                   JOIN transactions t ON t.id = bp.txn_id
                   WHERE bp.bill_id = ? AND bp.period < ? AND t.source != 'bill'
                     AND t.direction = 'out'
                   ORDER BY bp.period DESC LIMIT 1""",
                (p["bill_id"], period)).fetchone()
            last_key = derivations._normalize_merchant(last["description"]) if last else ""
            matches = []
            for o in derivations.bill_link_options(db, p["bill_id"], period, limit=None):
                if P2P.search(o["description"]):
                    continue  # a Venmo to a person is check 2, never the bill's charge
                day = date.fromisoformat(o["txn_date"])
                if (abs((day - due).days) > derivations.LOOKS_PAID_DAYS
                        and abs((day - logged_day).days) > derivations.LOOKS_PAID_DAYS):
                    continue
                key = derivations._normalize_merchant(o["description"])
                same = bool(last_key) and key == last_key
                named = bool(key) and derivations._name_overlap(p["name"], key)
                exact = o["amount_cents"] in (p["usual_cents"], p["amount_cents"])
                near = (abs(o["amount_cents"] - p["amount_cents"]) * 10000
                        <= p["amount_cents"] * derivations.LOOKS_PAID_TOLERANCE_BP)
                if not (exact or ((same or named) and near)):
                    continue
                reason = ("same amount" if exact else
                          "same merchant as last time" if same else "name match")
                rank = (0 if exact else 1 if same else 2,
                        abs(o["amount_cents"] - p["amount_cents"]),
                        abs((day - logged_day).days), -o["txn_id"])
                matches.append((rank, o, reason))
            if matches:
                matches.sort(key=lambda m: m[0])
                out.append({"payment": dict(p),
                            "charges": [(o, r) for _, o, r in matches]})
    return out


# ---------------------------------------------------------------- check 2

def rent_payments(db, periods):
    """Every rent bill payment Ledger has for these months: how rent is
    already counted, which decides whether a Venmo for rent is a double."""
    rows = db.execute(
        f"""SELECT bp.period, b.id AS bill_id, b.name, b.category, t.id AS txn_id,
                   t.txn_date, t.amount_cents, t.source
            FROM bill_payments bp JOIN bills b ON b.id = bp.bill_id
            LEFT JOIN transactions t ON t.id = bp.txn_id
            WHERE bp.period IN ({",".join("?" * len(periods))})
            ORDER BY bp.period, b.name""", periods).fetchall()
    return [dict(r) for r in rows if is_rent_bill(r)]


def p2p_rent(db, start, end, members):
    """Venmo/Zelle/Cash App/PayPal rows in the window that still count (not a
    transfer, not a settlement). "Likely rent" when the description says rent
    or names another member, or the amount is within ±10% of a rent bill or
    half of it."""
    rent_amounts = [b["amount_cents"] for b in db.execute(
        "SELECT * FROM bills WHERE active = 1") if is_rent_bill(b)]
    targets = [a for r in rent_amounts for a in (r, r // 2)]
    names = {w.casefold() for m in members
             for w in re.findall(r"[A-Za-z]{3,}", m["display_name"] or "")}
    rows = db.execute(
        """SELECT * FROM transactions
           WHERE txn_date BETWEEN ? AND ? AND is_transfer = 0
             AND source != 'settlement'
           ORDER BY txn_date, id""", (start.isoformat(), end.isoformat())).fetchall()
    out = []
    for t in rows:
        if not P2P.search(t["description"]):
            continue
        words = {w.casefold() for w in re.findall(r"[A-Za-z]{3,}", t["description"])}
        why = []
        if RENT_WORDS.search(t["description"]):
            why.append("says rent")
        if words & names:
            why.append("names a member")
        if any(abs(t["amount_cents"] - a) * 10000 <= a * RENT_SHARE_TOLERANCE_BP
               for a in targets):
            why.append("amount close to rent or half of it")
        out.append({"txn": dict(t), "why": why})
    return out


# ---------------------------------------------------------------- check 3

def same_amount_pairs(db, start, end, skip_ids):
    rows = db.execute(
        """SELECT * FROM transactions
           WHERE direction = 'out' AND is_transfer = 0 AND source != 'settlement'
             AND txn_date BETWEEN ? AND ?
           ORDER BY amount_cents, txn_date, id""",
        ((start - timedelta(days=SAME_AMOUNT_DAYS)).isoformat(),
         (end + timedelta(days=SAME_AMOUNT_DAYS)).isoformat())).fetchall()
    out = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if b["amount_cents"] != a["amount_cents"]:
                break
            gap = abs((date.fromisoformat(b["txn_date"])
                       - date.fromisoformat(a["txn_date"])).days)
            if gap > SAME_AMOUNT_DAYS:
                continue
            if {a["id"], b["id"]} & skip_ids:
                continue
            if not (watched(a) or watched(b)):
                continue
            if not (start.isoformat() <= max(a["txn_date"], b["txn_date"])
                    and min(a["txn_date"], b["txn_date"]) <= end.isoformat()):
                continue
            # Two imports with distinct bank ids on the same account are two
            # real charges; still listed, but say so.
            out.append({"a": dict(a), "b": dict(b), "gap": gap})
    return out


# ---------------------------------------------------------------- what-if

def snapshot(db):
    bal = derivations.compute_balance(db)
    if bal["state"] == "owing":
        b = f"{bal['ower']['display_name']} owes {bal['owed']['display_name']} " \
            f"{money(bal['amount_cents'])}"
    else:
        b = bal["state"]
    months = {m: v["total_cents"] for m, v in derivations.spending_summary(db).items()}
    return b, months


def what_if(src, fix):
    """Run `fix(db)` through the app's verbs on an in-memory copy and return
    the money numbers that move. The copy is thrown away."""
    mem = sqlite3.connect(":memory:")
    src.backup(mem)
    mem.row_factory = sqlite3.Row
    mem.execute("PRAGMA foreign_keys = ON")
    try:
        before = snapshot(mem)
        try:
            fix(mem)
        except (ValueError, actions.NotFound) as e:
            return [f"the app would refuse this fix: {e}"]
        after = snapshot(mem)
    finally:
        mem.close()
    lines = []
    if before[0] != after[0]:
        lines.append(f"balance: {before[0]}  ->  {after[0]}")
    for m in sorted(set(before[1]) | set(after[1])):
        x, y = before[1].get(m, 0), after[1].get(m, 0)
        if x != y:
            lines.append(f"{m} spending: {money(x)}  ->  {money(y)}")
    return lines or ["no money number moves"]


# ---------------------------------------------------------------- report

def describe(t):
    shared = "shared" if t.get("is_shared") else "personal"
    src = t.get("source", "")
    return (f"#{t['id']}  {t['txn_date']}  {money(t['amount_cents']):>11}  "
            f"{t['description'][:48]}  [{t.get('category', '')}, {shared}, {src}]")


def main(argv=None):
    today = date.today()
    first_this = today.replace(day=1)
    default_from = (first_this - timedelta(days=1)).replace(day=1)
    ap = argparse.ArgumentParser(description="Find rent and utility payments "
                                 "Ledger may be counting twice (read-only)")
    ap.add_argument("--from", dest="start", default=default_from.isoformat(),
                    help="first day to check, YYYY-MM-DD (default: first of last month)")
    ap.add_argument("--to", dest="end", default=today.isoformat(),
                    help="last day to check, YYYY-MM-DD (default: today)")
    ap.add_argument("--db", default=os.environ.get(
        "DATABASE_PATH", os.path.join(BASE_DIR, "finance.db")),
        help="database path (default: DATABASE_PATH or ./finance.db)")
    args = ap.parse_args(argv)
    try:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if start > end:
        print("error: --from is after --to", file=sys.stderr)
        return 2
    try:
        require_current_schema(args.db)
        db = open_readonly(args.db)
    except (SchemaNotReady, sqlite3.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        periods = periods_between(start, end)
        members = [dict(m) for m in db.execute(
            "SELECT id, display_name FROM members WHERE active = 1")]
        found = 0
        print(f"Checking {start} to {end} (bill months {', '.join(periods)}). "
              "Read-only: nothing is written.")

        # 1
        doubles = bill_doubles(db, periods)
        print(f"\n1. Bills marked paid that the bank also imported: {len(doubles)}")
        flagged = set()
        for d in doubles:
            found += 1
            p, (charge, reason) = d["payment"], d["charges"][0]
            flagged.update({p["txn_id"], charge["txn_id"]})
            print(f"\n  {p['name']}, {p['period']}")
            print(f"    Mark paid logged: #{p['txn_id']}  {p['txn_date']}  "
                  f"{money(p['amount_cents'])}  {p['description'][:48]}")
            print(f"    Bank charge ({reason}): #{charge['txn_id']}  "
                  f"{charge['txn_date']}  {money(charge['amount_cents'])}  "
                  f"{charge['description'][:48]}")
            for o, r in d["charges"][1:3]:
                print(f"    Also possible ({r}): #{o['txn_id']}  {o['txn_date']}  "
                      f"{money(o['amount_cents'])}  {o['description'][:48]}")
            print(f"    Fix: Calendar > {p['name']} > {p['period']} > Undo, then "
                  f"link #{charge['txn_id']} (Looks paid chip, or Mark paid > "
                  "Pick the charge).")

            def fix(m, p=p, c=charge):
                actions.unmark_bill_paid(m, ACTOR, p["bill_id"], p["period"])
                actions.link_bill_payment(m, ACTOR, p["bill_id"],
                                          {"period": p["period"], "txn_id": c["txn_id"]})
            for line in what_if(db, fix):
                print(f"    What the fix does: {line}")

        # 2
        # Include the month after the window: rent for the 1st is often paid
        # (or reimbursed) in the last days of the month before.
        y, m = map(int, periods[-1].split("-"))
        after = f"{y + (m == 12):04d}-{(m % 12) + 1:02d}"
        rents = rent_payments(db, periods + [after])
        p2p = p2p_rent(db, start, end, members)
        print(f"\n2. Person-to-person payments that still count as money: {len(p2p)}")
        print("   Rent already in Ledger for these months:")
        if not rents:
            print("     none (no rent bill is marked paid or linked for these months)")
        for r in rents:
            how = ("logged by Mark paid" if r["source"] == "bill"
                   else f"linked bank charge #{r['txn_id']}" if r["txn_id"]
                   else "marked paid, no transaction")
            amt = money(r["amount_cents"]) if r["amount_cents"] is not None else "-"
            print(f"     {r['period']}  {r['name']}  {amt}  {how}")
        for item in p2p:
            t = item["txn"]
            likely = "LIKELY RENT" if item["why"] else "probably not rent"
            print(f"\n  {describe(t)}")
            print(f"    {t['direction']}, {likely}"
                  + (f" ({'; '.join(item['why'])})" if item["why"] else ""))
            if item["why"]:
                found += 1
                flagged.add(t["id"])
                if rents:
                    print("    Fix, if this is your share of rent already listed "
                          "above: Activity > open it > Mark as transfer.")
                else:
                    print("    No rent is in Ledger for these months, so this may be "
                          "the only record of it. Leave it unless rent gets added.")
                for line in what_if(db, lambda m, i=t["id"]: actions.set_transfer(
                        m, ACTOR, i, True)):
                    print(f"    What Mark as transfer does: {line}")

        # 3
        pairs = same_amount_pairs(db, start, end, flagged)
        print(f"\n3. Same amount twice within {SAME_AMOUNT_DAYS} days on a rent or "
              f"utility charge: {len(pairs)}")
        for pr in pairs:
            found += 1
            print(f"\n  {describe(pr['a'])}\n  {describe(pr['b'])}")
            both_synced = pr["a"]["source"] == pr["b"]["source"] == "simplefin"
            print("    Both came from the bank, so they may be two real charges."
                  if both_synced else
                  "    Look at both in Activity; if one is a copy, delete that one.")

        print(f"\n{found} thing(s) to look at." if found
              else "\nNothing counted twice found in this window.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
