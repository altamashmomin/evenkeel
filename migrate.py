#!/usr/bin/env python3
"""Migration runner for Ledger (CORE-DESIGN invariant 7, sequence step 1).

Migrations live in migrations/ as NNN_description.sql or NNN_description.py,
numbered, idempotent, applied strictly in order. Each one runs inside a
single transaction together with the row that records it in schema_version
(a table migration #001 itself creates), so a migration and its bookkeeping
land atomically or not at all.

Usage:
    python migrate.py status  <db>     current version + applied migrations
    python migrate.py pending <db>     migrations not yet applied
    python migrate.py apply   <db>     apply all pending migrations, in order

The database file must already exist (the runner migrates databases, it
does not create them), and may never be the live finance.db.

A .py migration must define  apply(conn)  taking an open sqlite3 connection;
it runs inside the runner's transaction and must not commit or rollback.
"""
import argparse
import importlib.util
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
FILENAME_RE = re.compile(r"^(\d{3})_([A-Za-z0-9_]+)\.(sql|py)$")


def fail(msg):
    sys.exit(f"error: {msg}")


def check_db_path(path):
    if os.path.basename(path) == "finance.db":
        fail("refusing to touch finance.db — run against a copy (CLAUDE.md rule 6)")
    if not os.path.exists(path):
        fail(f"database not found: {path} (the runner does not create databases)")


def discover():
    """Return migrations as [(number, description, path)], ordered, validated."""
    if not os.path.isdir(MIGRATIONS_DIR):
        fail(f"migrations directory not found: {MIGRATIONS_DIR}")
    found = {}
    for name in sorted(os.listdir(MIGRATIONS_DIR)):
        if name.startswith(".") or name == "__pycache__":
            continue
        m = FILENAME_RE.match(name)
        if not m:
            fail(f"bad migration filename: {name} (expected NNN_description.sql|.py)")
        number = int(m.group(1))
        if number in found:
            fail(f"duplicate migration number {number:03d}: "
                 f"{os.path.basename(found[number][2])} and {name}")
        found[number] = (number, m.group(2), os.path.join(MIGRATIONS_DIR, name))
    return [found[n] for n in sorted(found)]


def applied_versions(conn):
    """Versions recorded in schema_version; empty if the table doesn't exist yet."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if row is None:
        return {}
    return {v: (at, desc) for v, at, desc in
            conn.execute("SELECT version, applied_at, description FROM schema_version")}


def run_sql_file(conn, path):
    """Execute a SQL file statement by statement inside the open transaction.

    (executescript would commit the transaction first, which is exactly what
    must not happen — so statements are split with sqlite3.complete_statement.)
    """
    with open(path) as f:
        text = f.read()
    statement = ""
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not statement and (not stripped or stripped.startswith("--")):
            continue
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        fail(f"{os.path.basename(path)} ends with an incomplete SQL statement")


def run_py_file(conn, path):
    spec = importlib.util.spec_from_file_location(
        f"migration_{os.path.basename(path)[:-3]}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "apply"):
        fail(f"{os.path.basename(path)} defines no apply(conn) function")
    module.apply(conn)


def connect(path, enforce_fk=True):
    conn = sqlite3.connect(path)
    conn.isolation_level = None  # explicit BEGIN/COMMIT, no autocommit surprises
    # apply runs with foreign keys OFF — the documented SQLite procedure for
    # table rebuilds (create new, copy, drop old, rename), which would
    # otherwise fire implicit deletes on child rows. Integrity is enforced
    # instead by a whole-database foreign_key_check before every COMMIT.
    conn.execute(f"PRAGMA foreign_keys = {'ON' if enforce_fk else 'OFF'}")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def cmd_status(db_path):
    conn = connect(db_path)
    applied = applied_versions(conn)
    if not applied:
        print("current version: none (schema_version table not present)")
    else:
        current = max(applied)
        print(f"current version: {current:03d}")
        for v in sorted(applied):
            at, desc = applied[v]
            print(f"  {v:03d}  {desc}  (applied {at})")
    conn.close()


def cmd_pending(db_path):
    conn = connect(db_path)
    applied = applied_versions(conn)
    conn.close()
    pending = [(n, d, p) for n, d, p in discover() if n not in applied]
    if not pending:
        print("no pending migrations")
    for n, d, p in pending:
        print(f"  {n:03d}  {d}  ({os.path.basename(p)})")


def check_foreign_keys(conn):
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        detail = "; ".join(f"{table} rowid={rowid} -> {parent}"
                           for table, rowid, parent, _fkid in violations[:10])
        raise RuntimeError(f"foreign key violations after migration: {detail}")


def cmd_apply(db_path):
    conn = connect(db_path, enforce_fk=False)
    applied = applied_versions(conn)
    migrations = discover()
    for n in sorted(applied):
        if n not in {m[0] for m in migrations}:
            fail(f"database has migration {n:03d} applied but no such file exists")
    pending = [(n, d, p) for n, d, p in migrations if n not in applied]
    if not pending:
        print("nothing to apply — database is up to date")
        conn.close()
        return
    for number, description, path in pending:
        conn.execute("BEGIN")
        try:
            if path.endswith(".sql"):
                run_sql_file(conn, path)
            else:
                run_py_file(conn, path)
            check_foreign_keys(conn)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at, description) "
                "VALUES (?, ?, ?)",
                (number, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 description),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            conn.close()
            print(f"FAILED at {number:03d} {description} — rolled back, "
                  "nothing from this migration was kept", file=sys.stderr)
            raise
        print(f"applied {number:03d}  {description}")
    conn.close()


def main():
    ap = argparse.ArgumentParser(description="Ledger migration runner")
    ap.add_argument("command", choices=["status", "pending", "apply"])
    ap.add_argument("db", help="path to the SQLite database (never finance.db)")
    args = ap.parse_args()
    check_db_path(args.db)
    {"status": cmd_status, "pending": cmd_pending, "apply": cmd_apply}[args.command](args.db)


if __name__ == "__main__":
    main()
