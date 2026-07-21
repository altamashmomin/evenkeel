"""POST /api/transactions with source=settlement routes through the
settle_up verb: unchanged response shape, plus links and an audit row."""
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"


class SettleRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-route-test-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "7", "--months", "2", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "route-test-secret")
        spec = importlib.util.spec_from_file_location("app_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def settlement_payload(self, client):
        balance = client.get("/api/balance").get_json()
        self.assertFalse(balance["settled"])
        return {
            "date": date.today().isoformat(), "amount": balance["amount"],
            "description": "legacy client settlement", "category": "Settlement",
            "paid_by": balance["owes"]["id"], "is_shared": True,
            "payer_share_pct": 0, "source": "settlement",
        }

    def test_settlement_post_returns_v1_shape_and_writes_links_audit(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
        response = client.post(
            "/api/transactions", json=self.settlement_payload(client))
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
                "SELECT actor, action FROM audit_log WHERE target = ?",
                (f"transaction:{body['id']}",)).fetchall()
        finally:
            conn.close()
        self.assertGreater(links, 0)
        self.assertEqual([("ui:avery", "settle_up")], audit)

    def test_deleting_a_covered_transaction_also_removes_its_links(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
        settled = client.post(
            "/api/transactions", json=self.settlement_payload(client)).get_json()
        conn = sqlite3.connect(self.db_path)
        try:
            covered_id = conn.execute(
                "SELECT to_id FROM links WHERE link_type='settles' AND from_id=? "
                "ORDER BY to_id LIMIT 1", (settled["id"],)).fetchone()[0]
        finally:
            conn.close()
        response = client.delete(f"/api/transactions/{covered_id}")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"ok": True}, response.get_json())
        conn = sqlite3.connect(self.db_path)
        try:
            leftover = conn.execute(
                "SELECT COUNT(*) FROM links WHERE from_id=? OR to_id=?",
                (covered_id, covered_id)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(0, leftover)


if __name__ == "__main__":
    unittest.main()
