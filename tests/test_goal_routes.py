"""Goal routes stay byte-shaped like v1 while dispatching through the
verbs; negative amounts route to withdraw_from_goal."""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import _seedbase

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"


class GoalRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="ledger-goalroute-test-")
        cls.db_path = Path(cls.tmp.name) / "route.db"
        _seedbase.seed_into(cls.db_path, seed=29, months=2)
        os.environ["DATABASE_PATH"] = str(cls.db_path)
        os.environ.setdefault("SECRET_KEY", "goal-route-test-secret")
        spec = importlib.util.spec_from_file_location(
            "app_goal_route_test", REPO / "app.py")
        cls.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.app_module)
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def client(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
        return client

    def test_create_and_delete_keep_v1_shapes(self):
        client = self.client()
        created = client.post("/api/goals", json={"name": "Ski trip", "target": 900})
        self.assertEqual(201, created.status_code)
        body = created.get_json()
        self.assertEqual(
            ["id", "name", "progress", "saved", "target", "target_date"],
            sorted(body))
        self.assertEqual(900.0, body["target"])

        missing = client.post("/api/goals", json={"target": 900})
        self.assertEqual(400, missing.status_code)
        self.assertEqual({"error": "name is required"}, missing.get_json())

        deleted = client.delete(f"/api/goals/{body['id']}")
        self.assertEqual(200, deleted.status_code)
        self.assertEqual({"ok": True}, deleted.get_json())
        self.assertEqual(
            404, client.delete(f"/api/goals/{body['id']}").status_code)

    def test_contribute_sign_dispatches_to_the_right_verb(self):
        client = self.client()
        goal_id = client.post(
            "/api/goals", json={"name": "Dispatch", "target": 100}).get_json()["id"]

        deposit = client.post(f"/api/goals/{goal_id}/contribute",
                              json={"amount": 40, "note": "in"})
        self.assertEqual(201, deposit.status_code)
        withdrawal = client.post(f"/api/goals/{goal_id}/contribute",
                                 json={"amount": -15.5})
        self.assertEqual(201, withdrawal.status_code)
        zero = client.post(f"/api/goals/{goal_id}/contribute", json={"amount": 0})
        self.assertEqual(400, zero.status_code)
        self.assertEqual({"error": "amount cannot be zero"}, zero.get_json())

        listing = client.get(f"/api/goals/{goal_id}/contributions").get_json()
        self.assertEqual([-15.5, 40.0], sorted(r["amount"] for r in listing))

        conn = sqlite3.connect(self.db_path)
        try:
            actions_seen = [row[0] for row in conn.execute(
                "SELECT action FROM audit_log WHERE target = ? "
                "AND action != 'create_goal' ORDER BY id",
                (f"goal:{goal_id}",)).fetchall()]
            stored = [row[0] for row in conn.execute(
                "SELECT amount_cents FROM goal_contributions WHERE goal_id = ? "
                "ORDER BY id", (goal_id,)).fetchall()]
            details = [json.loads(row[0]) for row in conn.execute(
                "SELECT detail_json FROM audit_log WHERE target = ? "
                "AND action != 'create_goal' ORDER BY id",
                (f"goal:{goal_id}",)).fetchall()]
        finally:
            conn.close()
        self.assertEqual(["contribute_to_goal", "withdraw_from_goal"], actions_seen)
        self.assertEqual([4000, -1550], stored)
        self.assertEqual([4000, 1550], [d["amount_cents"] for d in details])


if __name__ == "__main__":
    unittest.main()
