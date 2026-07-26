"""GET /api/income/summary: presents income_summary cents as dollars at
the JSON edge, on a new endpoint that leaves the parity-pinned
/api/dashboard shape untouched."""
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


class IncomeSummaryRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-inc-sum-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "93", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM splits")
        conn.execute("DELETE FROM transactions")
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, 5000, 'spend', 'Groceries', 1, 0, 'manual', 'out')""",
            (f"{M}-10",))
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, 300000, 'pay', 'Other', 1, 0, 'simplefin', 'in', 'paycheck')""",
            (f"{M}-05",))
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, 1500, 'misc', 'Other', 1, 0, 'simplefin', 'in', 'unclassified')""",
            (f"{M}-06",))
        conn.commit()
        conn.close()

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "inc-sum-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_inc_sum_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, logged_in=True):
        client = self.app_module.app.test_client()
        if logged_in:
            with client.session_transaction() as session:
                session["user_id"] = 1
        return client

    def test_summary_shape_and_dollar_conversion(self):
        resp = self.client().get(f"/api/income/summary?month={M}")
        self.assertEqual(200, resp.status_code)
        body = resp.get_json()
        self.assertEqual(
            ["gross_inflows", "month", "month_spend", "net_cash_flow",
             "savings_rate", "true_income", "unclassified_count"], sorted(body))
        self.assertEqual(M, body["month"])
        self.assertEqual(3015.00, body["gross_inflows"])   # 300000 + 1500 cents
        self.assertEqual(3000.00, body["true_income"])
        self.assertEqual(50.00, body["month_spend"])
        self.assertEqual(2950.00, body["net_cash_flow"])
        self.assertEqual(round(295000 / 300000, 4), body["savings_rate"])
        self.assertEqual(1, body["unclassified_count"])

    def test_requires_login(self):
        resp = self.client(logged_in=False).get(f"/api/income/summary?month={M}")
        self.assertEqual(401, resp.status_code)


if __name__ == "__main__":
    unittest.main()
