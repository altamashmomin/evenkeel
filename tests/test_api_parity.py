"""Byte-identical API parity: v1 code on a v1 database vs current code on
the migrated copy of that same database. The frontend must not be able to
tell the schema moved (CLAUDE.md, migration #002 acceptance criterion)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HELPER = Path(__file__).resolve().parent / "api_parity_helper.py"
SEED_AS_OF = "2026-07-19"


def collect(code_dir, db_path):
    env = os.environ.copy()
    env["SECRET_KEY"] = "parity-test-secret"
    result = subprocess.run(
        [sys.executable, str(HELPER), str(code_dir), str(db_path)],
        check=True, capture_output=True, text=True, env=env)
    return json.loads(result.stdout)


class ApiParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="ledger-parity-test-")
        tmp_path = Path(cls.tmp.name)
        legacy_code = tmp_path / "legacy-code"
        legacy_code.mkdir()
        with (legacy_code / "app.py").open("w") as app_file:
            subprocess.run(
                ["git", "-C", str(REPO), "show", "main:app.py"],
                check=True, stdout=app_file, text=True)

        old_db = tmp_path / "old.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(old_db),
             "--seed", "42", "--months", "8", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        new_db = tmp_path / "new.db"
        shutil.copy2(old_db, new_db)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(new_db)],
            check=True, capture_output=True, text=True)

        cls.old_responses = collect(legacy_code, old_db)
        cls.new_responses = collect(REPO, new_db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_covers_every_month_and_core_endpoint(self):
        dashboards = [e for e in self.old_responses if e.startswith("/api/dashboard")]
        self.assertGreaterEqual(len(dashboards), 8)
        self.assertIn("/api/balance", self.old_responses)
        for response in self.old_responses.values():
            self.assertEqual(200, response["status"])

    def test_every_response_is_byte_identical(self):
        self.assertEqual(
            sorted(self.old_responses), sorted(self.new_responses),
            "old and new versions answered different endpoint sets")
        for endpoint in sorted(self.old_responses):
            self.assertEqual(
                self.old_responses[endpoint], self.new_responses[endpoint],
                f"response differs at {endpoint}")


if __name__ == "__main__":
    unittest.main()
