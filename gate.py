#!/usr/bin/env python3
"""The balance gate (CORE-DESIGN "The balance gate", sequence step 4).

Shadow-compares two code versions against a copy of the database. Old code
and new code must agree on:

  - the who-owes-whom balance, to the cent
  - monthly spend totals for every month present in the data
  - per-table row counts

An increment that intends to change a number declares the exact expected
diff in a JSON file; the gate passes only if the observed diff equals the
enumerated diff — nothing more, nothing less. With no expectation file,
only a zero diff passes.

Usage:
    python gate.py snapshot --db DB [--code DIR] [--out FILE]
        Compute the gate numbers for DB using the code checkout at DIR
        (default: this repo). Writes canonical JSON.

    python gate.py compare OLD.json NEW.json [--expect FILE]
        Diff two snapshots. Exit 0 iff the diff exactly matches the
        expectation file (default expectation: no diff).

    python gate.py run --db DB --old REF --new REF [--expect FILE]
        Convenience: git-worktree checkouts of both refs, snapshot each,
        compare. REF is any git commit-ish.

Expectation file format:
    {"note": "why these numbers move",
     "diffs": [{"path": "row_counts.splits", "old": null, "new": 340}, ...]}

The balance is taken from the code version's own compute_balance (app.py
today; the named derivation function once it exists). Monthly totals and
row counts are computed by SQL directly against the database — they are
data facts, identical semantics for both sides. DB must never be
finance.db; run against a copy.
"""
import argparse
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

REPO_DIR = os.path.dirname(os.path.abspath(__file__))


def fail(msg):
    sys.exit(f"error: {msg}")


def check_db_path(path):
    if os.path.basename(path) == "finance.db":
        fail("refusing to touch finance.db — run against a copy (CLAUDE.md rule 6)")
    if not os.path.exists(path):
        fail(f"database not found: {path}")


# ---------------------------------------------------------------- snapshot

def balance_cents(code_dir, db_path):
    """The who-owes-whom number as the code at code_dir computes it.

    Canonical form: {"cents": int >= 0, "ower_id": id|None, "owed_id": id|None}.
    Settled (or <2 members) is cents 0 with null ids.
    """
    sys.path.insert(0, code_dir)
    os.environ["DATABASE_PATH"] = os.path.abspath(db_path)
    try:
        app = importlib.import_module("app")
    finally:
        sys.path.pop(0)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = app.compute_balance(conn)
    finally:
        conn.close()
    if result.get("settled") or not result.get("owes"):
        return {"cents": 0, "ower_id": None, "owed_id": None}
    return {
        "cents": int(round(result["amount"] * 100)),
        "ower_id": result["owes"]["id"],
        "owed_id": result["owed"]["id"],
    }


def monthly_totals(conn):
    """Spend total in cents per month present, dashboard semantics
    (settlements excluded)."""
    rows = conn.execute(
        """SELECT substr(txn_date, 1, 7) AS month, SUM(amount_cents) AS total
           FROM transactions WHERE source != 'settlement'
           GROUP BY month ORDER BY month"""
    ).fetchall()
    return {month: total for month, total in rows}


def row_counts(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
            for t in tables}


def cmd_snapshot(args):
    check_db_path(args.db)
    code_dir = os.path.abspath(args.code)
    if not os.path.exists(os.path.join(code_dir, "app.py")):
        fail(f"no app.py in code dir: {code_dir}")
    conn = sqlite3.connect(args.db)
    try:
        snap = {
            "balance": balance_cents(code_dir, args.db),
            "monthly_totals": monthly_totals(conn),
            "row_counts": row_counts(conn),
        }
    finally:
        conn.close()
    text = json.dumps(snap, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    else:
        print(text)


# ---------------------------------------------------------------- compare

def flatten(snap):
    out = {}
    for section, values in snap.items():
        if isinstance(values, dict):
            for key, v in values.items():
                out[f"{section}.{key}"] = v
        else:
            out[section] = values
    return out


def diff_snapshots(old, new):
    """List of {path, old, new} for every value that differs. Missing = null."""
    old_flat, new_flat = flatten(old), flatten(new)
    diffs = []
    for path in sorted(set(old_flat) | set(new_flat)):
        o, n = old_flat.get(path), new_flat.get(path)
        if o != n:
            diffs.append({"path": path, "old": o, "new": n})
    return diffs


def canonical(diffs):
    return sorted(json.dumps(d, sort_keys=True) for d in diffs)


def cmd_compare(args):
    with open(args.old) as f:
        old = json.load(f)
    with open(args.new) as f:
        new = json.load(f)
    expected = []
    note = None
    if args.expect:
        with open(args.expect) as f:
            expectation = json.load(f)
        expected = expectation.get("diffs", [])
        note = expectation.get("note")
    observed = diff_snapshots(old, new)

    if canonical(observed) == canonical(expected):
        if observed:
            print(f"GATE PASS — observed diff matches the enumerated "
                  f"expectation exactly ({len(observed)} entries)"
                  + (f": {note}" if note else ""))
        else:
            print("GATE PASS — old and new agree to the cent "
                  f"({len(flatten(old))} values compared, zero diff)")
        return

    expected_set = set(canonical(expected))
    observed_set = set(canonical(observed))
    print("GATE FAIL", file=sys.stderr)
    for entry in sorted(observed_set - expected_set):
        d = json.loads(entry)
        print(f"  unexpected: {d['path']}  old={d['old']}  new={d['new']}",
              file=sys.stderr)
    for entry in sorted(expected_set - observed_set):
        d = json.loads(entry)
        print(f"  expected but not observed: {d['path']}  "
              f"old={d['old']}  new={d['new']}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- run

def cmd_run(args):
    check_db_path(args.db)
    with tempfile.TemporaryDirectory(prefix="gate-") as tmp:
        snaps = {}
        for side, ref in (("old", args.old), ("new", args.new)):
            worktree = os.path.join(tmp, side)
            subprocess.run(
                ["git", "-C", REPO_DIR, "worktree", "add", "--detach",
                 worktree, ref],
                check=True, capture_output=True, text=True)
            try:
                snaps[side] = os.path.join(tmp, f"{side}.json")
                subprocess.run(
                    [sys.executable, os.path.abspath(__file__), "snapshot",
                     "--db", args.db, "--code", worktree, "--out", snaps[side]],
                    check=True)
            finally:
                subprocess.run(
                    ["git", "-C", REPO_DIR, "worktree", "remove", "--force",
                     worktree],
                    check=True, capture_output=True, text=True)
        compare_args = argparse.Namespace(
            old=snaps["old"], new=snaps["new"], expect=args.expect)
        print(f"old={args.old}  new={args.new}  db={args.db}")
        cmd_compare(compare_args)


def main():
    ap = argparse.ArgumentParser(description="Ledger balance gate")
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("snapshot", help="compute gate numbers for one code version")
    s.add_argument("--db", required=True)
    s.add_argument("--code", default=REPO_DIR, help="code checkout to import (default: this repo)")
    s.add_argument("--out", help="write JSON here instead of stdout")
    s.set_defaults(func=cmd_snapshot)

    c = sub.add_parser("compare", help="diff two snapshots against an expectation")
    c.add_argument("old")
    c.add_argument("new")
    c.add_argument("--expect", help="JSON file enumerating the allowed diff")
    c.set_defaults(func=cmd_compare)

    r = sub.add_parser("run", help="snapshot two git refs and compare")
    r.add_argument("--db", required=True)
    r.add_argument("--old", required=True, help="git ref for the old code")
    r.add_argument("--new", required=True, help="git ref for the new code")
    r.add_argument("--expect", help="JSON file enumerating the allowed diff")
    r.set_defaults(func=cmd_run)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
