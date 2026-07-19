"""Read-only runtime checks for Ledger's migration-owned schema."""
import os
import sqlite3
from pathlib import Path


REQUIRED_SCHEMA_VERSION = 3


class SchemaNotReady(RuntimeError):
    """The configured database is absent, unmigrated, or the wrong version."""


def connect_existing(path, **kwargs):
    """Open an existing SQLite database read/write without creating a file."""
    resolved = Path(path).expanduser().resolve()
    try:
        return sqlite3.connect(f"{resolved.as_uri()}?mode=rw", uri=True, **kwargs)
    except sqlite3.OperationalError as exc:
        raise SchemaNotReady(
            f"database not found or not writable: {resolved}; initialize it with "
            f"'python migrate.py init <db>'"
        ) from exc


def require_current_schema(path, required=REQUIRED_SCHEMA_VERSION):
    """Verify migration history without changing the database."""
    conn = connect_existing(path)
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if table is None:
            raise SchemaNotReady(
                "database has no schema_version table; run "
                f"'python migrate.py apply {os.path.abspath(path)}' before starting Ledger"
            )
        versions = [row[0] for row in conn.execute(
            "SELECT version FROM schema_version ORDER BY version")]
        expected = list(range(1, required + 1))
        if versions != expected:
            raise SchemaNotReady(
                f"database migration history is {versions}, expected {expected}; run "
                f"'python migrate.py apply {os.path.abspath(path)}'"
            )
    finally:
        conn.close()
