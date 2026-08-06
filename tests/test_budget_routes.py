"""Budget routes: thin callers over set_budget / remove_budget, the active-list
GET, money at the JSON edge, and bearer write-scope gating."""
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


class BudgetRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-budget-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "61", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "budget-route-secret")
        spec = importlib.util.spec_from_file_location("app_budget_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def client(self):
        c = self.app_module.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = 1
        return c

    def test_set_list_remove(self):
        c = self.client()
        r = c.post("/api/budgets", json={"category": "Groceries", "amount": 400})
        self.assertEqual(201, r.status_code)
        body = r.get_json()
        self.assertEqual("Groceries", body["category"])
        self.assertEqual({"cents": 40000, "display": "$400.00"}, body["amount"])  # dollars at the edge
        bid = body["id"]

        lst = c.get("/api/budgets").get_json()
        self.assertEqual(1, len(lst))
        self.assertEqual("Groceries", lst[0]["category"])

        self.assertEqual(200, c.delete(f"/api/budgets/{bid}").status_code)
        self.assertEqual([], c.get("/api/budgets").get_json())   # soft-deleted → gone from the list

    def test_post_upserts_not_duplicates(self):
        c = self.client()
        c.post("/api/budgets", json={"category": "Dining", "amount": 200})
        c.post("/api/budgets", json={"category": "Dining", "amount": 250})
        lst = c.get("/api/budgets").get_json()
        self.assertEqual(1, len(lst))
        self.assertEqual(25000, lst[0]["amount"]["cents"])

    def test_validation_is_400(self):
        c = self.client()
        self.assertEqual(400, c.post("/api/budgets", json={"amount": 100}).status_code)
        self.assertEqual(400, c.post("/api/budgets", json={"category": "Gas", "amount": 0}).status_code)

    def test_remove_missing_is_404(self):
        self.assertEqual(404, self.client().delete("/api/budgets/999").status_code)

    def test_requires_auth_and_write_scope(self):
        anon = self.app_module.app.test_client()
        self.assertEqual(401, anon.get("/api/budgets").status_code)

        import actions
        conn = sqlite3.connect(self.db_path); conn.row_factory = sqlite3.Row
        read_tok = actions.create_api_token(
            conn, "ui:avery", {"label": "r", "user_id": 1, "scopes": "read"})["token"]
        write_tok = actions.create_api_token(
            conn, "ui:avery", {"label": "w", "user_id": 1, "scopes": "read,write"})["token"]
        conn.close()
        # a read token can list but not set
        self.assertEqual(200, anon.get(
            "/api/budgets", headers={"Authorization": f"Bearer {read_tok}"}).status_code)
        self.assertEqual(403, anon.post(
            "/api/budgets", json={"category": "Gas", "amount": 50},
            headers={"Authorization": f"Bearer {read_tok}"}).status_code)
        self.assertEqual(201, anon.post(
            "/api/budgets", json={"category": "Gas", "amount": 50},
            headers={"Authorization": f"Bearer {write_tok}"}).status_code)


if __name__ == "__main__":
    unittest.main()
