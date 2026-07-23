#!/usr/bin/env python3
"""Ledger's immutable old-code/old-DB vs new-code/migrated-DB gate.

Checks the who-owes-whom balance to the cent, each monthly spending total,
and every application-table row count. New code is evaluated through its
canonical ``derivations.py``. The frozen v1 reference predates that module,
so its totals are obtained by invoking its own dashboard for each month.

Usage:
    python gate.py snapshot --db DB [--code DIR] [--out FILE]
    python gate.py compare OLD.json NEW.json [--expect FILE]
    python gate.py run --db DB --old REF --new REF [--expect FILE]

``run`` makes independent old/new copies of DB, applies the new ref's
migrations only to the new copy, snapshots both, and compares them. Neither
the supplied DB nor any database named finance.db is ever mutated.
"""
import argparse
import importlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

REPO_DIR = os.path.dirname(os.path.abspath(__file__))


def fail(msg):
    sys.exit(f"error: {msg}")


def check_db_path(path):
    if os.path.basename(path) == "finance.db":
        fail("refusing to touch finance.db — run against a copy (CLAUDE.md rule 6)")
    if not os.path.isfile(path):
        fail(f"database not found: {path}")


def read_only_connection(path):
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def cents_from_display(value):
    return int((Decimal(str(value)) * 100).to_integral_value())


def row_counts(db_path):
    conn = read_only_connection(db_path)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {table: conn.execute(
            f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
    finally:
        conn.close()


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_new_snapshot(code_dir, db_path):
    derivations = load_module(
        os.path.join(code_dir, "derivations.py"), "ledger_gate_derivations")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        balance = derivations.compute_balance(conn)
        spending = derivations.spending_summary(conn)
    finally:
        conn.close()
    if balance["state"] == "owing":
        balance_snapshot = {
            "cents": balance["amount_cents"],
            "ower_id": balance["ower"]["id"],
            "owed_id": balance["owed"]["id"],
        }
    else:
        balance_snapshot = {"cents": 0, "ower_id": None, "owed_id": None}
    return {
        "balance": balance_snapshot,
        "monthly_totals": {
            month: values["total_cents"] for month, values in spending.items()
        },
    }


def import_legacy_app(code_dir, db_path):
    previous_db = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = os.path.abspath(db_path)
    sys.path.insert(0, code_dir)
    sys.modules.pop("app", None)
    try:
        return importlib.import_module("app")
    finally:
        sys.path.pop(0)
        if previous_db is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = previous_db


def legacy_months(conn):
    return [row[0] for row in conn.execute(
        "SELECT DISTINCT substr(txn_date, 1, 7) FROM transactions ORDER BY 1")]


def canonical_legacy_snapshot(code_dir, db_path):
    """Evaluate v1 through v1's own balance and dashboard code."""
    legacy = import_legacy_app(code_dir, db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        balance = legacy.compute_balance(conn)
        months = legacy_months(conn)
        people_table = "users" if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone() else "members"
        actor = conn.execute(f"SELECT id FROM {people_table} ORDER BY id LIMIT 1").fetchone()
        actor_id = actor["id"] if actor else 0
    finally:
        conn.close()

    if balance.get("settled") or not balance.get("owes"):
        balance_snapshot = {"cents": 0, "ower_id": None, "owed_id": None}
    else:
        balance_snapshot = {
            "cents": cents_from_display(balance["amount"]),
            "ower_id": balance["owes"]["id"],
            "owed_id": balance["owed"]["id"],
        }

    totals = {}
    original_current_period = legacy.current_period
    try:
        for month in months:
            legacy.current_period = lambda value=month: value
            with legacy.app.test_request_context("/api/dashboard"):
                legacy.session["user_id"] = actor_id
                response = legacy.dashboard()
                if isinstance(response, tuple):
                    response = response[0]
                totals[month] = cents_from_display(response.get_json()["month_total"])
    finally:
        legacy.current_period = original_current_period
    return {"balance": balance_snapshot, "monthly_totals": totals}


def code_snapshot(code_dir, db_path):
    """Evaluate code against a disposable copy; never mutate db_path."""
    with tempfile.TemporaryDirectory(prefix="gate-eval-") as tmp:
        evaluation_db = os.path.join(tmp, "evaluation.db")
        shutil.copy2(db_path, evaluation_db)
        if os.path.isfile(os.path.join(code_dir, "derivations.py")):
            values = canonical_new_snapshot(code_dir, evaluation_db)
        else:
            values = canonical_legacy_snapshot(code_dir, evaluation_db)
    values["row_counts"] = row_counts(db_path)
    return values


def write_snapshot(snapshot, out_path=None):
    text = json.dumps(snapshot, indent=2, sort_keys=True)
    if out_path:
        with open(out_path, "w") as output:
            output.write(text + "\n")
    else:
        print(text)


def cmd_snapshot(args):
    check_db_path(args.db)
    code_dir = os.path.abspath(args.code)
    if not os.path.isfile(os.path.join(code_dir, "app.py")):
        fail(f"no app.py in code dir: {code_dir}")
    write_snapshot(code_snapshot(code_dir, args.db), args.out)


def flatten(snapshot):
    flattened = {}
    for section, values in snapshot.items():
        if isinstance(values, dict):
            for key, value in values.items():
                flattened[f"{section}.{key}"] = value
        else:
            flattened[section] = values
    return flattened


def diff_snapshots(old, new):
    old_flat, new_flat = flatten(old), flatten(new)
    return [
        {"path": path, "old": old_flat.get(path), "new": new_flat.get(path)}
        for path in sorted(set(old_flat) | set(new_flat))
        if old_flat.get(path) != new_flat.get(path)
    ]


def canonical(diffs):
    return sorted(json.dumps(diff, sort_keys=True) for diff in diffs)


def cmd_compare(args):
    with open(args.old) as old_file:
        old = json.load(old_file)
    with open(args.new) as new_file:
        new = json.load(new_file)
    expected, note = [], None
    if args.expect:
        with open(args.expect) as expectation_file:
            expectation = json.load(expectation_file)
        expected = expectation.get("diffs", [])
        note = expectation.get("note")
    observed = diff_snapshots(old, new)
    if canonical(observed) == canonical(expected):
        if observed:
            print("GATE PASS — observed diff matches the enumerated expectation "
                  f"exactly ({len(observed)} entries)" + (f": {note}" if note else ""))
        else:
            print("GATE PASS — old and new agree to the cent "
                  f"({len(flatten(old))} values compared, zero diff)")
        return

    expected_set, observed_set = set(canonical(expected)), set(canonical(observed))
    print("GATE FAIL", file=sys.stderr)
    for entry in sorted(observed_set - expected_set):
        diff = json.loads(entry)
        print(f"  unexpected: {diff['path']}  old={diff['old']}  new={diff['new']}",
              file=sys.stderr)
    for entry in sorted(expected_set - observed_set):
        diff = json.loads(entry)
        print(f"  expected but not observed: {diff['path']}  "
              f"old={diff['old']}  new={diff['new']}", file=sys.stderr)
    raise SystemExit(1)


def add_worktree(ref, path):
    subprocess.run(
        ["git", "-C", REPO_DIR, "worktree", "add", "--detach", path, ref],
        check=True, capture_output=True, text=True)


def remove_worktree(path):
    subprocess.run(
        ["git", "-C", REPO_DIR, "worktree", "remove", "--force", path],
        check=True, capture_output=True, text=True)


def cmd_run(args):
    check_db_path(args.db)
    with tempfile.TemporaryDirectory(prefix="gate-run-") as tmp:
        worktrees = {}
        added = []
        try:
            for side, ref in (("old", args.old), ("new", args.new)):
                worktrees[side] = os.path.join(tmp, f"code-{side}")
                add_worktree(ref, worktrees[side])
                added.append(worktrees[side])

            old_db, new_db = os.path.join(tmp, "old.db"), os.path.join(tmp, "new.db")
            shutil.copy2(args.db, old_db)
            shutil.copy2(args.db, new_db)
            new_migrate = os.path.join(worktrees["new"], "migrate.py")
            if os.path.isfile(new_migrate):
                subprocess.run([sys.executable, new_migrate, "apply", new_db], check=True)

            snapshots = {}
            for side, db_path in (("old", old_db), ("new", new_db)):
                snapshots[side] = os.path.join(tmp, f"{side}.json")
                subprocess.run(
                    [sys.executable, os.path.abspath(__file__), "snapshot", "--db", db_path,
                     "--code", worktrees[side], "--out", snapshots[side]], check=True)
            print(f"old={args.old} + pre-migration DB  "
                  f"new={args.new} + migrated copy  source={args.db}")
            cmd_compare(argparse.Namespace(
                old=snapshots["old"], new=snapshots["new"], expect=args.expect))
        finally:
            for worktree in reversed(added):
                remove_worktree(worktree)


def main():
    parser = argparse.ArgumentParser(description="Ledger balance gate")
    sub = parser.add_subparsers(dest="command", required=True)

    snapshot = sub.add_parser("snapshot", help="compute one immutable snapshot")
    snapshot.add_argument("--db", required=True)
    snapshot.add_argument("--code", default=REPO_DIR)
    snapshot.add_argument("--out")
    snapshot.set_defaults(func=cmd_snapshot)

    compare = sub.add_parser("compare", help="compare snapshots with an expectation")
    compare.add_argument("old")
    compare.add_argument("new")
    compare.add_argument("--expect")
    compare.set_defaults(func=cmd_compare)

    run = sub.add_parser("run", help="compare old DB/code to migrated new DB/code")
    run.add_argument("--db", required=True)
    run.add_argument("--old", required=True)
    run.add_argument("--new", required=True)
    run.add_argument("--expect")
    run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
