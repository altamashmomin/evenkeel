import contextlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
sys.path.insert(0, str(REPO))

import migrate
from schema_runtime import REQUIRED_SCHEMA_VERSION


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-migrations-test-")
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, check=True):
        return subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), *map(str, args)],
            check=check, capture_output=True, text=True)

    def test_init_builds_current_schema_and_apply_is_noop(self):
        db_path = self.tmp_path / "fresh.db"
        first = self.run_cli("init", db_path)
        second = self.run_cli("apply", db_path)
        self.assertIn("applied 003", first.stdout)
        self.assertIn("applied 005", first.stdout)
        self.assertIn("nothing to apply", second.stdout)
        conn = sqlite3.connect(db_path)
        try:
            versions = [row[0] for row in conn.execute(
                "SELECT version FROM schema_version ORDER BY version")]
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            txn_columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(transactions)")}
        finally:
            conn.close()
        self.assertEqual(list(range(1, REQUIRED_SCHEMA_VERSION + 1)), versions)
        self.assertIn("members", tables)
        self.assertIn("splits", tables)
        self.assertIn("links", tables)
        self.assertIn("income_rules", tables)
        self.assertNotIn("users", tables)
        self.assertIn("direction", txn_columns)
        self.assertIn("income_type", txn_columns)

    def test_income_migration_defaults_existing_rows_to_out(self):
        db_path = self.tmp_path / "income.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(db_path),
             "--seed", "3", "--months", "2", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        self.run_cli("apply", db_path)
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT DISTINCT direction, income_type FROM transactions").fetchall()
        finally:
            conn.close()
        self.assertEqual([("out", None)], rows)

    def test_migration_005_apply_is_idempotent(self):
        # Every migration must be re-runnable (CLAUDE.md hard rule 1). 005
        # was the one exception until it became a guarded .py: its raw
        # ALTER TABLE ADD COLUMN statements raised "duplicate column name"
        # on a second run. Call apply() twice directly against a v5 db and
        # require no error — the runner's version tracking prevents a second
        # run in practice, but idempotency is defense-in-depth here, as it
        # is for every other migration in this directory.
        import importlib.util
        db_path = self.tmp_path / "idem.db"
        self.run_cli("init", db_path)
        spec = importlib.util.spec_from_file_location(
            "m005_idem", REPO / "migrations" / "005_income.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db_path)
        try:
            module.apply(conn)  # re-run once
            module.apply(conn)  # re-run twice — the .sql version raised here
            cols = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
        finally:
            conn.close()
        self.assertIn("direction", cols)
        self.assertIn("income_type", cols)

    def test_failed_migration_rolls_back_its_schema_and_version(self):
        migrations_dir = self.tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "001_schema_version.sql").write_text(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, "
            "applied_at TEXT NOT NULL, description TEXT NOT NULL);\n")
        (migrations_dir / "002_breaks.sql").write_text(
            "CREATE TABLE must_roll_back (id INTEGER);\n"
            "INSERT INTO table_that_does_not_exist VALUES (1);\n")
        db_path = self.tmp_path / "rollback.db"
        sqlite3.connect(db_path).close()
        with mock.patch.object(migrate, "MIGRATIONS_DIR", str(migrations_dir)):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(sqlite3.OperationalError):
                    migrate.cmd_apply(str(db_path))
        conn = sqlite3.connect(db_path)
        try:
            versions = [row[0] for row in conn.execute(
                "SELECT version FROM schema_version ORDER BY version")]
            rolled_back = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='must_roll_back'").fetchone()
        finally:
            conn.close()
        self.assertEqual([1], versions)
        self.assertIsNone(rolled_back)

    def test_finance_db_refused_without_live_flag(self):
        # The refusal fires on the name alone, before any existence check,
        # so no file named finance.db is ever created — not even here.
        result = self.run_cli(
            "apply", self.tmp_path / "finance.db", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("refusing to touch finance.db", result.stderr)
        self.assertFalse((self.tmp_path / "finance.db").exists())

    def test_live_flag_lifts_the_name_guard_purely(self):
        # Pure function check, no subprocess and no filesystem: live=True
        # lifts the basename tripwire, live=False keeps it. The guard is a
        # human-error tripwire, not a security boundary — symlinks and
        # aliases mean it cannot prove which physical file is live.
        target = str(self.tmp_path / "finance.db")
        self.assertIsNone(migrate.check_safe_name(target, live=True))
        with self.assertRaises(SystemExit):
            migrate.check_safe_name(target, live=False)
        self.assertFalse((self.tmp_path / "finance.db").exists())

    def test_discovery_rejects_numbering_gaps(self):
        migrations_dir = self.tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "001_first.sql").write_text("SELECT 1;\n")
        (migrations_dir / "003_gap.sql").write_text("SELECT 1;\n")
        with mock.patch.object(migrate, "MIGRATIONS_DIR", str(migrations_dir)):
            with self.assertRaises(SystemExit):
                migrate.discover()

    def test_history_must_be_an_exact_prefix(self):
        migrations = [
            (1, "one", "001_one.sql"),
            (2, "two", "002_two.sql"),
            (3, "three", "003_three.sql"),
        ]
        applied = {
            1: ("2026-01-01T00:00:00+00:00", "one"),
            3: ("2026-01-01T00:00:01+00:00", "three"),
        }
        with self.assertRaises(SystemExit):
            migrate.validate_history(applied, migrations)

    def test_app_rejects_unmigrated_db_without_mutating_it(self):
        db_path = self.tmp_path / "unmigrated.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(db_path), "--months", "1",
             "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        before = db_path.read_bytes()
        env = os.environ.copy()
        env["DATABASE_PATH"] = str(db_path)
        result = subprocess.run(
            [sys.executable, "-c", "import app"], cwd=REPO, env=env,
            capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("SchemaNotReady", result.stderr)
        self.assertEqual(before, db_path.read_bytes())

    def test_runtime_version_matches_latest_migration(self):
        self.assertEqual(REQUIRED_SCHEMA_VERSION, migrate.discover()[-1][0])


if __name__ == "__main__":
    unittest.main()
