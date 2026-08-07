"""The ops panel (Agents tab foot): run-sync-now, guardian heartbeat, recent
audit activity. The sync trigger runs the REAL simplefin_sync.py as a
subprocess (pointed at a missing access file here, so it exits fast and
deterministically with no network) — proving the plumbing: exit relayed,
summary surfaced, and the trigger audited as the person via record_sync_run."""
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

    def test_sync_runs_script_relays_failure_and_audits_the_trigger(self):
        r = self.client().post("/api/ops/sync")
        self.assertEqual(200, r.status_code)
        body = r.get_json()
        self.assertFalse(body["ok"])                    # no access file
        self.assertIn("error:", body["summary"])        # the script's own reason
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT actor, detail_json FROM audit_log "
                "WHERE action = 'record_sync_run'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "the trigger must be audited")
        self.assertEqual("ui:avery", row["actor"])      # who pressed it
        self.assertIn("error:", row["detail_json"])

    def test_sync_is_session_only(self):
        self.assertEqual(401, self.client(user_id=None).post("/api/ops/sync").status_code)

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
        c.post("/api/ops/sync")                          # guarantees >=1 row
        body = c.get("/api/ops/audit?limit=5").get_json()
        self.assertLessEqual(len(body["entries"]), 5)
        self.assertEqual("record_sync_run", body["entries"][0]["action"])
        self.assertEqual({"id", "at", "actor", "action", "target"},
                         set(body["entries"][0]))
        ids = [e["id"] for e in body["entries"]]
        self.assertEqual(sorted(ids, reverse=True), ids)  # newest first
        self.assertEqual(401, self.client(user_id=None).get("/api/ops/audit").status_code)


if __name__ == "__main__":
    unittest.main()
