#!/usr/bin/env python3
"""Re-credit past bank charges to the person whose account they came from.

Until migration #020, the SimpleFIN sync credited EVERY imported charge to one
person (SYNC_PAID_BY), so a charge on Charlee's card showed "Altamash paid" and
the balance said Charlee owed half of it back. Once you've said whose each
bank account is (Activity > Whose accounts?), new charges land right. This
script fixes the old ones, through the app's own edit verb (edit_transaction):
only "who paid" changes. Amounts, dates, categories and the split stay exactly
as they are, and each change gets its audit row.

A joint account (Whose accounts? > Joint, migration #021) is fixed the joint
way instead: its charges become shared with the payer's share at 100%, so they
count as household spending and move nobody's balance. Who paid stays as is.

Usage:
    venv/bin/python fix_payers.py                     # dry run (default)
    venv/bin/python fix_payers.py --apply             # really fix them
    venv/bin/python fix_payers.py --include-settled   # also rows a settle-up closed
    venv/bin/python fix_payers.py --account ACCOUNT_ID

What it touches: synced money-out rows (source 'simplefin') from an account
that has an owner set, currently credited to someone else, or from a joint
account, not yet recorded as joint. Never manual rows,
bills, settlements, deposits, or accounts with no owner set.

By default it leaves alone charges a settle-up already closed: re-crediting
those reopens a period you two already squared and moves the balance for it.
Add --include-settled if those were wrong too and you want them counted again.

Dry run: copies the database to a temp file, runs the real fix against the
copy, prints every row it would change and the balance before and after, checks
that no amount or monthly spend total moved, and throws the copy away. The real
database is only read.

--apply: first writes a backup next to the database
(<db>.bak-fixpayers-<timestamp>, gitignored), then fixes, then repeats the
checks against the real database. Undo: stop the app and copy the backup back.

Environment (.env), the same as the app and the sync:
    DATABASE_PATH   path to the app's SQLite file (default: ./finance.db)
"""
import argparse
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

ACTOR = "cli:fix_payers"


def open_db(path):
    db = connect_existing(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def names(db):
    return {r["id"]: r["display_name"] for r in db.execute(
        "SELECT id, display_name FROM members")}


def money_snapshot(db):
    """What re-crediting must never move (every amount and monthly spend
    total, the row count) plus the one number it is meant to move (the
    balance), kept apart so the check can tell them apart."""
    bal = derivations.compute_balance(db)
    spend = derivations.spending_summary(db)
    return {
        "fixed": {
            "months": {m: v["total_cents"] for m, v in spend.items()},
            "transactions": db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
            "amount_sum": db.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions").fetchone()[0],
        },
        "balance": bal,
    }


def fmt_money(cents):
    return f"${cents / 100:,.2f}"


def balance_text(bal):
    if bal["state"] != "owing":
        return "all settled up"
    return (f"{bal['ower']['display_name']} owes {bal['owed']['display_name']} "
            f"{fmt_money(bal['amount_cents'])}")


def print_accounts(db):
    who = names(db)
    accts = derivations.bank_accounts(db)
    print("Bank accounts Ledger knows:")
    if not accts:
        print("  none yet (no synced charges)")
    for a in accts:
        owner = ("JOINT" if a["joint"] else
                 who.get(a["member_id"], "?") if a["member_id"] else "NOT SET")
        credited = ", ".join(f"{who.get(m, '?')} {n}" for m, n in sorted(a["by_payer"].items()))
        print(f"  {a['label'][:34]:<34} owner: {owner:<10} {a['charges']:>4} charges "
              f"(credited: {credited or '-'})  id {a['account_id']}")
    unset = [a for a in accts if a["member_id"] is None and not a["joint"] and a["charges"]]
    if unset:
        print("\n  Accounts with no owner are skipped. Set them in Ledger: "
              "Activity > Whose accounts?")


def run_fix(db, include_settled, account_id):
    fixes = derivations.payer_fixes(db, include_settled=include_settled,
                                    account_id=account_id)
    for f in fixes:
        if f["joint"]:
            # Joint account: record it the joint way (shared, payer's share
            # 100%), so it moves nobody's balance. paid_by stays as it is.
            change = {"is_shared": True, "payer_share_pct": 100}
        else:
            change = {"paid_by": f["owner"]}
            # Keep each person's share as it was: edit_transaction carries the
            # OLD payer's percentage over to the new payer, so a 20/80 split
            # would flip to 80/20. Hand it the new payer's own share instead.
            row = db.execute(
                "SELECT share_bp FROM splits WHERE transaction_id = ? AND member_id = ?",
                (f["transaction_id"], f["owner"])).fetchone()
            if row is not None:
                change["payer_share_pct"] = row["share_bp"] / 100
        actions.edit_transaction(db, ACTOR, f["transaction_id"], change)
    return fixes


def report(db, fixes, applied, skipped_settled):
    who = names(db)
    verb = "Re-credited" if applied else "Would re-credit"
    print(f"\n{verb} {len(fixes)} charge(s):")
    for f in fixes:
        tail = "  (was settled; reopens it)" if f["settled"] else ""
        share = "" if f["is_shared"] else "  (personal)"
        print(f"  {f['date']}  {fmt_money(f['amount_cents']):>10}  "
              f"{f['description'][:36]:<36} {who.get(f['paid_by'], '?')} -> "
              f"{'joint (nobody owes)' if f['joint'] else who.get(f['owner'], '?')}"
              f"{share}{tail}")
    if skipped_settled:
        print(f"\nLeft alone: {skipped_settled} charge(s) a settle-up already closed. "
              "Add --include-settled to fix those too.")


def check(before, after):
    if before["fixed"] != after["fixed"]:
        print("\nSTOP: an amount, a monthly total or the row count changed, "
              "which re-crediting must never do.")
        print(f"  before: {before['fixed']}\n  after:  {after['fixed']}")
        return False
    print(f"\nMoney check OK: every amount, {len(after['fixed']['months'])} monthly "
          f"totals and {after['fixed']['transactions']} transactions unchanged.")
    print(f"Balance before: {balance_text(before['balance'])}")
    print(f"Balance after:  {balance_text(after['balance'])}")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Re-credit past bank charges to the account's owner")
    ap.add_argument("--apply", action="store_true",
                    help="really fix them (default is a dry run on a temp copy)")
    ap.add_argument("--include-settled", action="store_true",
                    help="also fix charges a settle-up already closed (reopens them)")
    ap.add_argument("--account", help="only this SimpleFIN account id")
    ap.add_argument("--db", default=os.environ.get(
        "DATABASE_PATH", os.path.join(BASE_DIR, "finance.db")),
        help="database path (default: DATABASE_PATH or ./finance.db)")
    args = ap.parse_args(argv)

    try:
        require_current_schema(args.db)
    except Exception as e:  # SchemaNotReady, sqlite errors
        print(f"error: {e}", file=sys.stderr)
        return 2

    def count_skipped(db):
        if args.include_settled:
            return 0
        every = derivations.payer_fixes(db, include_settled=True, account_id=args.account)
        return sum(1 for f in every if f["settled"])

    if not args.apply:
        with tempfile.TemporaryDirectory(prefix="ledger-fixpayers-dry-") as tmp:
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
                print(f"DRY RUN against a copy of {args.db}. Nothing was written.\n")
                print_accounts(db)
                skipped = count_skipped(db)
                before = money_snapshot(db)
                try:
                    fixes = run_fix(db, args.include_settled, args.account)
                except (ValueError, actions.NotFound) as e:
                    print(f"error: Ledger refused a change: {e}", file=sys.stderr)
                    return 2
                after = money_snapshot(db)
                report(db, fixes, applied=False, skipped_settled=skipped)
            finally:
                db.close()
        ok = check(before, after)
        if ok and fixes:
            print("\nTo make these changes for real, run the same command with --apply.")
        return 0 if ok else 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = f"{args.db}.bak-fixpayers-{stamp}"
    if os.path.exists(backup):
        print(f"error: backup {backup} already exists; try again", file=sys.stderr)
        return 2
    src = connect_existing(args.db)
    dst = sqlite3.connect(backup)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    print(f"Backup written: {backup}\n")
    db = open_db(args.db)
    try:
        print_accounts(db)
        skipped = count_skipped(db)
        before = money_snapshot(db)
        try:
            fixes = run_fix(db, args.include_settled, args.account)
        except (ValueError, actions.NotFound) as e:
            print(f"error: Ledger refused a change: {e}. Changes before it were "
                  f"made; re-running picks up the rest.", file=sys.stderr)
            return 2
        after = money_snapshot(db)
        report(db, fixes, applied=True, skipped_settled=skipped)
    finally:
        db.close()
    ok = check(before, after)
    if not ok:
        print(f"Restore with: cp {backup} {args.db}  (stop the app first)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
