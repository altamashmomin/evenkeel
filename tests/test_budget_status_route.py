"""GET /api/analytics/budget-status: budget_status with money as {cents, display}
at the JSON edge; pct/over pass through; unbudgeted_spend included."""
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
M = "2026-06"


class BudgetStatusRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-budget-status-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "61", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("DELETE FROM splits")
        conn.execute("DELETE FROM transactions")
        conn.commit()                       # close the txn before the verb runs
        import actions
        actions.set_budget(conn, "ui:avery", {"category": "Groceries", "amount": "500"})
        conn.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, category, "
            "paid_by, is_shared, source, direction) "
            "VALUES (?, 30000, 'spend', 'Groceries', 1, 0, 'manual', 'out')", (f"{M}-05",))
        conn.commit(); conn.close()

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "budget-status-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_budget_status_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, logged_in=True):
        c = self.app_module.app.test_client()
        if logged_in:
            with c.session_transaction() as s:
                s["user_id"] = 1
        return c

    def test_shape_and_dollars_at_the_edge(self):
        body = self.client().get(f"/api/analytics/budget-status?period={M}").get_json()
        self.assertEqual(M, body["period"])
        g = next(b for b in body["budgets"] if b["category"] == "Groceries")
        self.assertEqual({"cents": 50000, "display": "$500.00"}, g["budgeted"])
        self.assertEqual({"cents": 30000, "display": "$300.00"}, g["actual"])
        self.assertEqual({"cents": 20000, "display": "$200.00"}, g["remaining"])
        self.assertFalse(g["over"])
        self.assertEqual(60, g["pct"])                     # 30000 / 50000
        self.assertEqual(0, body["unbudgeted_spend"]["cents"])

    def test_requires_login(self):
        self.assertEqual(401, self.client(logged_in=False).get(
            "/api/analytics/budget-status").status_code)


if __name__ == "__main__":
    unittest.main()
