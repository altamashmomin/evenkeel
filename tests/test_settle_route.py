"""POST /api/transactions with source=settlement routes through the
settle_up verb: unchanged response shape, plus links and an audit row."""
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class SettleRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="ledger-route-test-")
        cls.db_path = Path(cls.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(cls.db_path),
             "--seed", "7", "--months", "2"],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(cls.db_path)],
            check=True, capture_output=True, text=True)
        os.environ["DATABASE_PATH"] = str(cls.db_path)
        os.environ.setdefault("SECRET_KEY", "route-test-secret")
        spec = importlib.util.spec_from_file_location("app_route_test", REPO / "app.py")
        cls.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.app_module)
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_settlement_post_returns_v1_shape_and_writes_links_audit(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
        response = client.post("/api/transactions", json={
            "date": "2026-07-02", "amount": 12.34,
            "description": "Settlement — Avery → Blake",
            "category": "Settlement", "paid_by": 1,
            "is_shared": True, "payer_share_pct": 0,
            "source": "settlement",
        })
        self.assertEqual(201, response.status_code)
        body = response.get_json()
        self.assertEqual(
            ["amount", "category", "date", "description", "id", "is_shared",
             "paid_by", "payer_share_pct", "source"], sorted(body))
        self.assertEqual("settlement", body["source"])
        self.assertEqual(0.0, body["payer_share_pct"])

        conn = sqlite3.connect(self.db_path)
        try:
            links = conn.execute(
                "SELECT COUNT(*) FROM links WHERE link_type='settles' AND from_id=?",
                (body["id"],)).fetchone()[0]
            audit = conn.execute(
                "SELECT actor, action FROM audit_log").fetchall()
        finally:
            conn.close()
        self.assertGreater(links, 0)
        self.assertEqual([("ui:avery", "settle_up")], audit)


if __name__ == "__main__":
    unittest.main()
