import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import gate


class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("SECRET_KEY", "synthetic-test-secret")
        cls.tmp = tempfile.TemporaryDirectory(prefix="ledger-gate-test-")
        cls.tmp_path = Path(cls.tmp.name)
        cls.legacy_code = cls.tmp_path / "legacy-code"
        cls.legacy_code.mkdir()
        with (cls.legacy_code / "app.py").open("w") as app_file:
            subprocess.run(
                ["git", "-C", str(REPO), "show", "main:app.py"],
                check=True, stdout=app_file, text=True)

        cls.old_db = cls.tmp_path / "old.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(cls.old_db),
             "--seed", "42", "--months", "8"],
            check=True, capture_output=True, text=True)
        cls.new_db = cls.tmp_path / "new.db"
        shutil.copy2(cls.old_db, cls.new_db)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(cls.new_db)],
            check=True, capture_output=True, text=True)
        cls.old_snapshot = gate.code_snapshot(str(cls.legacy_code), str(cls.old_db))
        cls.new_snapshot = gate.code_snapshot(str(REPO), str(cls.new_db))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_migration_002_preserves_balance_and_every_month(self):
        self.assertEqual(self.old_snapshot["balance"], self.new_snapshot["balance"])
        self.assertEqual(
            self.old_snapshot["monthly_totals"], self.new_snapshot["monthly_totals"])

    def test_structural_differences_remain_visible(self):
        old_counts, new_counts = (
            self.old_snapshot["row_counts"], self.new_snapshot["row_counts"])
        self.assertEqual(2, old_counts["users"])
        self.assertNotIn("users", new_counts)
        self.assertEqual(2, new_counts["members"])
        self.assertEqual(352, new_counts["splits"])
        self.assertEqual(3, new_counts["schema_version"])
        self.assertEqual(0, new_counts["links"])

    def test_snapshot_does_not_mutate_source_database(self):
        before = self.old_db.read_bytes()
        gate.code_snapshot(str(self.legacy_code), str(self.old_db))
        self.assertEqual(before, self.old_db.read_bytes())

    def test_one_cent_perturbation_fails_the_gate(self):
        perturbed = self.tmp_path / "perturbed.db"
        shutil.copy2(self.new_db, perturbed)
        conn = sqlite3.connect(perturbed)
        try:
            conn.execute(
                "UPDATE transactions SET amount_cents=amount_cents+1 "
                "WHERE id=(SELECT MIN(id) FROM transactions WHERE source!='settlement')")
            conn.commit()
        finally:
            conn.close()
        changed = gate.code_snapshot(str(REPO), str(perturbed))
        diffs = gate.diff_snapshots(self.new_snapshot, changed)
        self.assertTrue(any(diff["path"].startswith("monthly_totals.") for diff in diffs))

    def test_derivation_regression_changes_gate_snapshot(self):
        broken_code = self.tmp_path / "broken-code"
        broken_code.mkdir(exist_ok=True)
        shutil.copy2(REPO / "app.py", broken_code / "app.py")
        source = (REPO / "derivations.py").read_text()
        (broken_code / "derivations.py").write_text(
            source.replace("source != 'settlement'", "source = 'settlement'"))
        broken = gate.code_snapshot(str(broken_code), str(self.new_db))
        self.assertNotEqual(
            self.new_snapshot["monthly_totals"], broken["monthly_totals"])

    def test_expected_diff_must_match_exactly(self):
        old_path = self.tmp_path / "old.json"
        new_path = self.tmp_path / "new.json"
        expected_path = self.tmp_path / "expected.json"
        old_path.write_text(json.dumps(self.old_snapshot))
        new_path.write_text(json.dumps(self.new_snapshot))
        expected_path.write_text(json.dumps({"diffs": []}))
        args = type("Args", (), {
            "old": str(old_path), "new": str(new_path),
            "expect": str(expected_path)})()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                gate.cmd_compare(args)


if __name__ == "__main__":
    unittest.main()
