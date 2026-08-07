"""The ops panel (Agents tab foot): read-only in-app views — the Pi guardian's
heartbeat report and recent audit activity. (An on-demand sync button was
considered and dropped — SimpleFIN refreshes ~daily and disables tokens past
its request budget, so a one-tap button was redundant risk; sync stays
scheduled, guarded in simplefin_sync.py.)"""
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"


class OpsPanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-ops-panel-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "61", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "ops-panel-secret")
        # sync subprocess: no access file -> fast, deterministic, offline exit
        os.environ["SIMPLEFIN_ACCESS_FILE"] = str(Path(self.tmp.name) / "absent.url")
        # guardian heartbeat: a controlled temp path (written per-test)
        self.status_file = Path(self.tmp.name) / "ops-status.txt"
        os.environ["OPS_STATUS_FILE"] = str(self.status_file)
        spec = importlib.util.spec_from_file_location("app_ops_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, user_id=1):
        c = self.app_module.app.test_client()
        if user_id is not None:
            with c.session_transaction() as s:
                s["user_id"] = user_id
        return c

    def db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_no_sync_endpoint_exists(self):
        # The on-demand sync trigger was deliberately removed; only read-only
        # ops views remain. (405 not 404: the static catch-all matches the GET
        # path, so a POST there is "method not allowed" — either way, no
        # working sync endpoint.)
        self.assertIn(self.client().post("/api/ops/sync").status_code, (404, 405))
        self.assertNotIn("record_sync_run", (REPO / "app.py").read_text())

    def test_health_reads_the_heartbeat_and_absent_is_normal(self):
        no_file = self.client().get("/api/ops/health").get_json()
        self.assertEqual({"available": False, "report": None, "age_hours": None},
                         no_file)
        self.status_file.write_text("Ledger Pi Ops — 🟢 GREEN — all systems healthy\n  ✓ ok\n")
        with_file = self.client().get("/api/ops/health").get_json()
        self.assertTrue(with_file["available"])
        self.assertIn("GREEN", with_file["report"])
        self.assertEqual(0, with_file["age_hours"])

    def test_audit_lists_newest_first_and_clamps_limit(self):
        c = self.client()
        c.post("/api/inventory", json={"name": "Coffee"})   # a real audited write
        body = c.get("/api/ops/audit?limit=5").get_json()
        self.assertLessEqual(len(body["entries"]), 5)
        self.assertEqual("add_item", body["entries"][0]["action"])   # newest
        self.assertEqual({"id", "at", "actor", "action", "target"},
                         set(body["entries"][0]))
        ids = [e["id"] for e in body["entries"]]
        self.assertEqual(sorted(ids, reverse=True), ids)  # newest first
        self.assertEqual(401, self.client(user_id=None).get("/api/ops/audit").status_code)


if __name__ == "__main__":
    unittest.main()
