"""Bill CRUD routes stay byte-shaped like v1 while dispatching through
create_bill / update_bill / delete_bill — closing the last write path
that previously wrote no audit row at all."""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import _seedbase

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"


class BillRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-billroute-test-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=19, months=1)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "bill-route-test-secret")
        spec = importlib.util.spec_from_file_location(
            "app_bill_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def client(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
        return client

    def test_create_keeps_v1_shape_and_writes_audit(self):
        client = self.client()
        created = client.post("/api/bills", json={
            "name": "Streaming", "amount": 15, "due_day": 3, "category": "Subscriptions"})
        self.assertEqual(201, created.status_code)
        body = created.get_json()
        self.assertEqual(
            ["amount", "category", "due_day", "id", "name",
             "paid_on", "paid_this_period", "period"], sorted(body))
        self.assertEqual(15.0, body["amount"])

        missing = client.post("/api/bills", json={"amount": 15, "due_day": 3})
        self.assertEqual(400, missing.status_code)
        self.assertEqual({"error": "name is required"}, missing.get_json())

        conn = sqlite3.connect(self.db_path)
        try:
            audit = conn.execute(
                "SELECT actor, action FROM audit_log WHERE target = ?",
                (f"bill:{body['id']}",)).fetchall()
        finally:
            conn.close()
        self.assertEqual([("ui:avery", "create_bill")], audit)

    def test_update_missing_bill_is_404(self):
        client = self.client()
        response = client.put("/api/bills/999999", json={"amount": 5})
        self.assertEqual(404, response.status_code)
        self.assertEqual({"error": "not found"}, response.get_json())

    def test_update_keeps_v1_shape_and_writes_audit(self):
        client = self.client()
        bill_id = client.get("/api/bills").get_json()[0]["id"]
        response = client.put(f"/api/bills/{bill_id}", json={"amount": 42})
        self.assertEqual(200, response.status_code)
        self.assertEqual(42.0, response.get_json()["amount"])

        conn = sqlite3.connect(self.db_path)
        try:
            audit = conn.execute(
                "SELECT actor, action FROM audit_log WHERE target = ?",
                (f"bill:{bill_id}",)).fetchall()
        finally:
            conn.close()
        self.assertEqual([("ui:avery", "update_bill")], audit)

    def test_delete_keeps_v1_shape_and_writes_audit(self):
        client = self.client()
        created = client.post("/api/bills", json={
            "name": "Gym", "amount": 30, "due_day": 10}).get_json()
        response = client.delete(f"/api/bills/{created['id']}")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"ok": True}, response.get_json())
        self.assertEqual(404, client.delete(f"/api/bills/{created['id']}").status_code)

        conn = sqlite3.connect(self.db_path)
        try:
            audit = conn.execute(
                "SELECT actor, action FROM audit_log WHERE target = ? ORDER BY id",
                (f"bill:{created['id']}",)).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [("ui:avery", "create_bill"), ("ui:avery", "delete_bill")], audit)


if __name__ == "__main__":
    unittest.main()
