"""GET /api/analytics/savings-rate-trend: per-month savings rate + trailing
3-month rolling rate. Both are ratios (not money), passed through unconverted;
null when income is 0."""
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
A, B, C = "2026-05", "2026-06", "2026-07"


class SavingsRateTrendRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-sr-trend-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=97, months=1)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM splits")
        conn.execute("DELETE FROM transactions")
        # C: income 100000, spend 40000 -> rate 0.60.
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, 100000, 'pay', 'Other', 1, 0, 'simplefin', 'in', 'paycheck')""",
            (f"{C}-05",))
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, 40000, 'spend', 'Groceries', 1, 0, 'manual', 'out')""",
            (f"{C}-10",))
        conn.commit()
        conn.close()

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "sr-trend-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_sr_trend_route_test", REPO / "app.py")
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

    def test_shape_and_ratios_pass_through(self):
        body = self.client().get(
            f"/api/analytics/savings-rate-trend?anchor={C}&months_back=2").get_json()
        self.assertEqual(["anchor", "months_back", "series"], sorted(body))
        self.assertEqual([B, C], [e["month"] for e in body["series"]])
        empty, latest = body["series"]
        self.assertEqual(["month", "rolling_savings_rate", "savings_rate"], sorted(latest))
        self.assertIsNone(empty["savings_rate"])          # B has no income
        self.assertEqual(0.6, latest["savings_rate"])     # ratio, not dollars
        self.assertEqual(0.6, latest["rolling_savings_rate"])  # only C has income

    def test_bad_anchor_is_400(self):
        self.assertEqual(400, self.client().get(
            "/api/analytics/savings-rate-trend?anchor=nope").status_code)

    def test_months_back_clamped(self):
        body = self.client().get(
            "/api/analytics/savings-rate-trend?months_back=0").get_json()
        self.assertEqual(1, body["months_back"])

    def test_requires_login(self):
        self.assertEqual(401, self.client(logged_in=False)
                         .get("/api/analytics/savings-rate-trend").status_code)


if __name__ == "__main__":
    unittest.main()
