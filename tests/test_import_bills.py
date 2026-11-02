"""import_bills.py: a bulk Add-bill that goes through the create_bill and
set_bill_pay_details verbs. A dry run writes nothing to the real database;
--apply adds each bill once (re-runs skip by name), audits every row as
cli:import_bills, takes a backup first, and moves no money number."""
import contextlib
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _seedbase

import import_bills


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ImportBillsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-import-bills-test-")
        self.dir = Path(self.tmp.name)
        self.db_path = self.dir / "test.db"
        _seedbase.seed_into(self.db_path, seed=17, months=2)
        self.bills = [
            {"name": "Streaming", "amount": 13.99, "due_day": 8,
             "category": "Subscriptions", "autopay": True},
            {"name": "Parking", "amount": "16.00", "due_day": 1,
             "category": "Rent", "pay_url": "example.com", "autopay": False},
            {"name": "Car loan", "amount": 575.89, "due_day": 15},
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def write_file(self, specs, name="bills.json"):
        p = self.dir / name
        p.write_text(json.dumps(specs))
        return p

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = import_bills.main([*map(str, args), "--db", str(self.db_path)])
        return code, out.getvalue(), err.getvalue()

    def query(self, sql, *params):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def active_bills(self):
        return {r["name"]: dict(r) for r in self.query(
            "SELECT * FROM bills WHERE active = 1")}

    def test_dry_run_writes_nothing_and_lists_bills(self):
        before = sha(self.db_path)
        code, out, _ = self.run_cli(self.write_file(self.bills))
        self.assertEqual(code, 0)
        self.assertEqual(sha(self.db_path), before)
        self.assertIn("DRY RUN", out)
        self.assertIn("Would add 3 bill(s)", out)
        for b in self.bills:
            self.assertIn(b["name"], out)
        self.assertIn("Money check OK", out)
        self.assertEqual(list(self.dir.glob("*.bak-import-*")), [])

    def test_apply_adds_through_verbs_with_audit_and_pay_details(self):
        code, out, _ = self.run_cli(self.write_file(self.bills), "--apply")
        self.assertEqual(code, 0, out)
        bills = self.active_bills()
        self.assertEqual(bills["Streaming"]["amount_cents"], 1399)
        self.assertEqual(bills["Streaming"]["autopay"], 1)
        self.assertIsNone(bills["Streaming"]["pay_url"])
        self.assertEqual(bills["Parking"]["pay_url"], "https://example.com")
        self.assertEqual(bills["Parking"]["autopay"], 0)
        self.assertEqual(bills["Car loan"]["category"], "Bills")
        self.assertEqual(bills["Car loan"]["amount_cents"], 57589)
        audit = self.query(
            "SELECT action FROM audit_log WHERE actor = 'cli:import_bills'")
        actions_ = sorted(r["action"] for r in audit)
        self.assertEqual(actions_.count("create_bill"), 3)
        # "Car loan" names no pay details, so only two get that verb.
        self.assertEqual(actions_.count("set_bill_pay_details"), 2)
        self.assertEqual(len(list(self.dir.glob("test.db.bak-import-*"))), 1)
        self.assertIn("Money check OK", out)

    def test_rerun_adds_nothing_twice(self):
        f = self.write_file(self.bills)
        self.run_cli(f, "--apply")
        n = len(self.active_bills())
        code, out, _ = self.run_cli(f, "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.active_bills()), n)
        self.assertIn("Added 0 bill(s)", out)
        self.assertIn("Skipped 3", out)

    def test_existing_name_is_skipped_case_insensitively_and_left_alone(self):
        # The seed fixture ships a "Rent" bill; a differently-cased entry
        # must not create a second one or change the first.
        before = self.active_bills()["Rent"]
        code, out, _ = self.run_cli(self.write_file(
            [{"name": "  rent ", "amount": 2800, "due_day": 1}]), "--apply")
        self.assertEqual(code, 0)
        rents = [b for b in self.active_bills().values()
                 if b["name"].strip().lower() == "rent"]
        self.assertEqual(rents, [before])
        self.assertIn("Skipped 1", out)

    def test_duplicate_names_in_file_refused_before_any_write(self):
        before = sha(self.db_path)
        code, _, err = self.run_cli(self.write_file(
            self.bills + [{"name": "STREAMING", "amount": 1, "due_day": 2}]),
            "--apply")
        self.assertEqual(code, 2)
        self.assertIn("listed twice", err)
        self.assertEqual(sha(self.db_path), before)

    def test_bad_pay_link_refused_before_any_write(self):
        before = sha(self.db_path)
        bad = self.bills + [{"name": "X", "amount": 1, "due_day": 2,
                             "pay_url": "javascript:alert(1)"}]
        code, _, err = self.run_cli(self.write_file(bad), "--apply")
        self.assertEqual(code, 2)
        self.assertIn("'X'", err)
        self.assertEqual(sha(self.db_path), before)

    def test_invalid_due_day_caught_by_dry_run(self):
        code, _, err = self.run_cli(self.write_file(
            [{"name": "Y", "amount": 5, "due_day": 32}]))
        self.assertEqual(code, 2)
        self.assertIn("due day", err)

    def test_overlong_name_refused(self):
        code, _, err = self.run_cli(self.write_file(
            [{"name": "x" * 101, "amount": 5, "due_day": 3}]))
        self.assertEqual(code, 2)
        self.assertIn("over 100", err)

    def test_two_applies_keep_separate_backups(self):
        f = self.write_file(self.bills)
        self.run_cli(f, "--apply")
        self.run_cli(f, "--apply")
        self.assertEqual(len(list(self.dir.glob("test.db.bak-import-*"))), 2)

    def test_unknown_key_refused(self):
        code, _, err = self.run_cli(self.write_file(
            [{"name": "Y", "amount": 5, "due_day": 3, "shared": True}]))
        self.assertEqual(code, 2)
        self.assertIn("unknown keys", err)

    def test_refuses_db_on_wrong_schema(self):
        db = sqlite3.connect(self.db_path)
        db.execute("DELETE FROM schema_version WHERE version = "
                   "(SELECT MAX(version) FROM schema_version)")
        db.commit()
        db.close()
        code, _, err = self.run_cli(self.write_file(self.bills))
        self.assertEqual(code, 2)
        self.assertIn("error", err)


if __name__ == "__main__":
    unittest.main()
