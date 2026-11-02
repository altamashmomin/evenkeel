#!/usr/bin/env python3
"""Add a list of recurring bills to Ledger in one go, through the app's own
verbs (create_bill, then set_bill_pay_details). No raw SQL: every row lands
exactly as if it had been typed into the Calendar tab's Add-bill dialog, with
its audit row.

Usage:
    venv/bin/python import_bills.py BILLS.json            # dry run (default)
    venv/bin/python import_bills.py BILLS.json --apply    # really add them

BILLS.json is a list of bills (household data: keep it out of git):
    [{"name": "Internet", "amount": 60.00, "due_day": 15,
      "category": "Internet", "pay_url": "example.com", "autopay": true}, ...]
name, amount and due_day are required; category defaults to "Bills";
pay_url and autopay are optional.

Safe to run twice: a bill whose name matches an ACTIVE bill (trimmed,
case-insensitive) is skipped, never added again or changed. Two entries with
the same name in one file is an error.

Dry run: copies the database to a temp file, runs the real import against the
copy, prints what it would add, checks that no money number moved (the
who-owes-whom balance and every monthly spend total), and throws the copy
away. The real database is only read.

--apply: first writes a backup next to the database
(<db>.bak-import-<timestamp>, gitignored), then imports, then repeats the
money-number check against the real database. Adding a bill creates no
transaction, so the balance and monthly totals must be byte-identical.

Environment (.env), the same as the app and the sync:
    DATABASE_PATH   path to the app's SQLite file (default: ./finance.db)
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime

from dotenv import load_dotenv

import actions
import derivations
from schema_runtime import connect_existing, require_current_schema

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

ACTOR = "cli:import_bills"
_KEYS = {"name", "amount", "due_day", "category", "pay_url", "autopay"}


class ImportError_(Exception):
    """A problem with the bills file, caught before anything is written."""


def load_specs(path):
    try:
        with open(path) as f:
            specs = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ImportError_(f"can't read {path}: {e}")
    if not isinstance(specs, list) or not specs:
        raise ImportError_("the bills file must be a non-empty JSON list")
    seen = {}
    for i, s in enumerate(specs, 1):
        if not isinstance(s, dict):
            raise ImportError_(f"entry {i} is not an object")
        extra = set(s) - _KEYS
        if extra:
            raise ImportError_(f"entry {i} has unknown keys: {sorted(extra)}")
        name = (s.get("name") or "").strip() if isinstance(s.get("name"), str) else ""
        if not name:
            raise ImportError_(f"entry {i} has no name")
        if len(name) > 100:
            # create_bill stores name[:100]; refuse rather than let the stored
            # name differ from the one the re-run skip check compares.
            raise ImportError_(f"entry {i}: name is over 100 characters")
        key = name.casefold()
        if key in seen:
            raise ImportError_(f"'{name}' is listed twice (entries {seen[key]} and {i})")
        seen[key] = i
        # Pre-check the pay details with the verb's own cleaners, so a bad
        # link can't leave a bill created without them (create_bill and
        # set_bill_pay_details are two verb transactions). create_bill's own
        # validation of amount/due_day runs in the dry run on a copy.
        try:
            if "pay_url" in s:
                actions._clean_pay_url(s["pay_url"])
            if "autopay" in s:
                actions._clean_autopay(s["autopay"])
        except ValueError as e:
            raise ImportError_(f"'{name}': {e}")
    return specs


def money_snapshot(db):
    """The numbers a bill definition must never move: the who-owes-whom
    balance and every month's spend total (the balance gate's own fields)."""
    bal = derivations.compute_balance(db)
    spend = derivations.spending_summary(db)
    return {
        "balance": (bal["state"], bal.get("amount_cents", 0)),
        "months": {m: v["total_cents"] for m, v in spend.items()},
        "transactions": db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
    }


def open_db(path):
    db = connect_existing(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def run_import(db, specs):
    """Add each spec through the verbs. Returns (added, skipped) lists of
    dicts for the report. Each bill is its own verb transaction; a validation
    error on one aborts the run before later bills, and earlier ones stay
    (re-running then skips them by name)."""
    active = {r["name"].strip().casefold(): r for r in db.execute(
        "SELECT * FROM bills WHERE active = 1")}
    added, skipped = [], []
    for s in specs:
        name = s["name"].strip()
        existing = active.get(name.casefold())
        if existing is not None:
            skipped.append({"name": name, "existing": dict(existing)})
            continue
        bill = actions.create_bill(db, ACTOR, {
            "name": name, "amount": s["amount"], "due_day": s["due_day"],
            "category": s.get("category")})
        details = {k: s[k] for k in ("pay_url", "autopay") if k in s}
        if details:
            bill = actions.set_bill_pay_details(db, ACTOR, bill["id"], details)
        added.append(dict(bill))
        active[name.casefold()] = bill
    return added, skipped


def fmt_money(cents):
    return f"${cents / 100:,.2f}"


def report(added, skipped, applied):
    verb = "Added" if applied else "Would add"
    print(f"\n{verb} {len(added)} bill(s):")
    if added:
        print(f"  {'Name':<32} {'Amount':>10}  {'Due':>3}  {'Category':<14} "
              f"{'Autopay':<7} Pay at")
    for b in added:
        print(f"  {b['name'][:32]:<32} {fmt_money(b['amount_cents']):>10}  "
              f"{b['due_day']:>3}  {b['category'][:14]:<14} "
              f"{'on' if b['autopay'] else 'off':<7} {b['pay_url'] or '-'}")
    if skipped:
        print(f"\nSkipped {len(skipped)} already in Ledger (left exactly as they are):")
        for s in skipped:
            e = s["existing"]
            print(f"  {e['name'][:32]:<32} {fmt_money(e['amount_cents']):>10}  "
                  f"due {e['due_day']}")


def check_unmoved(before, after):
    if before != after:
        print("\nSTOP: a money number changed, which adding bills must never do.")
        print(f"  before: {before}\n  after:  {after}")
        return False
    print(f"\nMoney check OK: balance, {len(after['months'])} monthly totals and "
          f"{after['transactions']} transactions unchanged.")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Add recurring bills to Ledger")
    ap.add_argument("bills_file", help="JSON list of bills")
    ap.add_argument("--apply", action="store_true",
                    help="really add them (default is a dry run on a temp copy)")
    ap.add_argument("--db", default=os.environ.get(
        "DATABASE_PATH", os.path.join(BASE_DIR, "finance.db")),
        help="database path (default: DATABASE_PATH or ./finance.db)")
    args = ap.parse_args(argv)

    try:
        specs = load_specs(args.bills_file)
        require_current_schema(args.db)
    except Exception as e:  # ImportError_, SchemaNotReady, sqlite errors
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not args.apply:
        with tempfile.TemporaryDirectory(prefix="ledger-import-dry-") as tmp:
            copy = os.path.join(tmp, "dry-run.db")
            src = connect_existing(args.db)
            dst = sqlite3.connect(copy)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            db = open_db(copy)
            try:
                before = money_snapshot(db)
                try:
                    added, skipped = run_import(db, specs)
                except (ValueError, actions.NotFound) as e:
                    print(f"error: Ledger refused a bill: {e}", file=sys.stderr)
                    return 2
                after = money_snapshot(db)
            finally:
                db.close()
        print(f"DRY RUN against a copy of {args.db}. Nothing was written.")
        report(added, skipped, applied=False)
        ok = check_unmoved(before, after)
        if ok and added:
            print("\nTo add them for real, run the same command with --apply.")
        return 0 if ok else 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = f"{args.db}.bak-import-{stamp}"
    if os.path.exists(backup):
        # Never overwrite an earlier rollback point.
        print(f"error: backup {backup} already exists; try again", file=sys.stderr)
        return 2
    src = connect_existing(args.db)
    dst = sqlite3.connect(backup)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    print(f"Backup written: {backup}")
    db = open_db(args.db)
    try:
        before = money_snapshot(db)
        try:
            added, skipped = run_import(db, specs)
        except (ValueError, actions.NotFound) as e:
            print(f"error: Ledger refused a bill: {e}. Bills before it were "
                  f"added; fix the file and re-run (they'll be skipped).",
                  file=sys.stderr)
            return 2
        after = money_snapshot(db)
    finally:
        db.close()
    report(added, skipped, applied=True)
    ok = check_unmoved(before, after)
    if not ok:
        print(f"Restore with: cp {backup} {args.db}  (stop the app first)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
