"""GET /api/analytics/bill-variance: defined vs actual vs variance per bill
for a period, money as {cents, display}; unpaid bills report null."""
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
P = "2026-06"


class BillVarianceRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-bill-var-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=101, months=1)
        conn = sqlite3.connect(self.db_path)
        for t in ("splits", "transactions", "bill_payments", "bills"):
            conn.execute(f"DELETE FROM {t}")
        # Electric: defined 9000, paid 10200 (over). Internet: defined 6500, unpaid.
        cur = conn.execute(
            "INSERT INTO bills (name, amount_cents, due_day, category, active) "
            "VALUES ('Electric', 9000, 5, 'Utilities', 1)")
        elec = cur.lastrowid
        conn.execute("INSERT INTO bills (name, amount_cents, due_day, category, active) "
                     "VALUES ('Internet', 6500, 18, 'Internet', 1)")
        cur = conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, 10200, 'Electric', 'Utilities', 1, 0, 'manual', 'out')""",
            (f"{P}-05",))
        conn.execute("INSERT INTO bill_payments (bill_id, period, paid_on, txn_id) "
                     "VALUES (?, ?, ?, ?)", (elec, P, f"{P}-05", cur.lastrowid))
        conn.commit()
        conn.close()

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "bill-var-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_bill_var_route_test", REPO / "app.py")
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

    def test_variance_shape_dual_money_and_null_for_unpaid(self):
        body = self.client().get(
            f"/api/analytics/bill-variance?period={P}").get_json()
        self.assertEqual(["bills", "period"], sorted(body))
        bills = {b["name"]: b for b in body["bills"]}
        elec = bills["Electric"]
        self.assertEqual({"cents": 9000, "display": "$90.00"}, elec["defined"])
        self.assertEqual({"cents": 10200, "display": "$102.00"}, elec["actual"])
        self.assertEqual({"cents": 1200, "display": "$12.00"}, elec["variance"])
        self.assertTrue(elec["paid"])
        net = bills["Internet"]
        self.assertIsNone(net["actual"])
        self.assertIsNone(net["variance"])
        self.assertFalse(net["paid"])

    def test_default_period_is_current_period(self):
        body = self.client().get("/api/analytics/bill-variance").get_json()
        self.assertEqual(self.app_module.current_period(), body["period"])

    def test_requires_login(self):
        self.assertEqual(401, self.client(logged_in=False)
                         .get("/api/analytics/bill-variance").status_code)


if __name__ == "__main__":
    unittest.main()
