#!/usr/bin/env python3
"""Real-data money-invariant gate for the Pi migration walk.

The seed-based expectation files in notes/ enumerate row counts for the
synthetic seed (seed=42); on the live database those counts differ (splits =
2x the real shared-transaction count, members = the real member count, ...).
What must NOT differ is the money: the who-owes-whom balance to the cent and
every monthly spending total. This tool proves exactly that on the real data.

It reuses gate.py's own snapshot/diff logic (the same code that gates every
increment in dev), runs it across two git refs against a *copy* of the live
database, and:

  - FAILS loudly if the balance or any monthly total changed (the invariant);
  - otherwise PASSES and prints the structural row-count changes for the
    operator to eyeball against the migration notes (users -> members,
    shared transactions -> splits, new empty tables, schema_version bump).

The old ref is evaluated through its own code (v1.0 predates derivations.py,
so gate.py drives its dashboard directly); the new ref is evaluated through
its derivations.py. The new ref's migrations are applied only to the new copy.
Never touches the supplied database, and refuses anything named finance.db.

Usage (run on the Pi, against a BACKUP copy of the live DB — never finance.db):
    python deploy/verify_live_migration.py --db finance.db.bak-<ts> \
        --old v1.0 --new rework
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

# gate.py lives at the repo root, one level up from deploy/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gate  # noqa: E402 — reuse its proven snapshot + diff internals

MONEY_PREFIXES = ("balance", "monthly_totals")


def is_money(path):
    return path == "balance" or path.split(".", 1)[0] in MONEY_PREFIXES


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True,
                    help="a COPY/backup of the live DB (never finance.db itself)")
    ap.add_argument("--old", default="v1.0", help="pre-migration ref (default v1.0)")
    ap.add_argument("--new", default="rework", help="target ref (default rework)")
    args = ap.parse_args()

    gate.check_db_path(args.db)  # refuses finance.db, requires the file to exist

    with tempfile.TemporaryDirectory(prefix="verify-live-") as tmp:
        worktrees, added = {}, []
        try:
            for side, ref in (("old", args.old), ("new", args.new)):
                worktrees[side] = os.path.join(tmp, f"code-{side}")
                gate.add_worktree(ref, worktrees[side])
                added.append(worktrees[side])

            old_db = os.path.join(tmp, "old.db")
            new_db = os.path.join(tmp, "new.db")
            shutil.copy2(args.db, old_db)
            shutil.copy2(args.db, new_db)

            new_migrate = os.path.join(worktrees["new"], "migrate.py")
            if not os.path.isfile(new_migrate):
                sys.exit(f"error: {args.new} has no migrate.py — cannot migrate the new side")
            subprocess.run([sys.executable, new_migrate, "apply", new_db], check=True)

            old_snap = gate.code_snapshot(worktrees["old"], old_db)
            new_snap = gate.code_snapshot(worktrees["new"], new_db)
        finally:
            for worktree in reversed(added):
                gate.remove_worktree(worktree)

    diffs = gate.diff_snapshots(old_snap, new_snap)
    money = [d for d in diffs if is_money(d["path"])]
    structural = [d for d in diffs if not is_money(d["path"])]

    print(f"old={args.old} (its own code)   new={args.new} (migrated copy)   "
          f"source={args.db}")
    if money:
        print("\nGATE FAIL — the migration MOVED money. Do NOT apply to the live "
              "database.", file=sys.stderr)
        for d in sorted(money, key=lambda d: d["path"]):
            print(f"  {d['path']}: {d['old']} -> {d['new']}", file=sys.stderr)
        sys.exit(1)

    print("\nGATE PASS — balance and every monthly total are unchanged to the cent.")
    if structural:
        print("Structural changes (confirm against the migration notes):")
        for d in sorted(structural, key=lambda d: d["path"]):
            print(f"  {d['path']}: {d['old']} -> {d['new']}")
    else:
        print("No structural changes either (zero diff).")


if __name__ == "__main__":
    main()
