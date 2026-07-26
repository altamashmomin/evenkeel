"""GET /api/income/trend: presents the income_trend derivation as dollars
at the JSON edge over a trailing window, on a new endpoint that leaves the
parity-pinned surface untouched. anchor defaults to the current period;
months_back is validated and clamped."""
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
B, C = "2026-06", "2026-07"


class IncomeTrendRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-inc-trend-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "94", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM splits")
        conn.execute("DELETE FROM transactions")
        # C: spend 5000, paycheck 300000, refund 2000 -> net spend 3000.
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, 5000, 'spend', 'Groceries', 1, 0, 'manual', 'out')""",
            (f"{C}-10",))
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, 300000, 'pay', 'Other', 1, 0, 'simplefin', 'in', 'paycheck')""",
            (f"{C}-05",))
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, 2000, 'ret', 'Groceries', 1, 0, 'simplefin', 'in', 'refund')""",
            (f"{C}-07",))
        conn.commit()
        conn.close()

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "inc-trend-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_inc_trend_route_test", REPO / "app.py")
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

    def test_trend_shape_and_dollar_conversion(self):
        resp = self.client().get(f"/api/income/trend?anchor={C}&months_back=2")
        self.assertEqual(200, resp.status_code)
        body = resp.get_json()
        self.assertEqual(["anchor", "months_back", "series"], sorted(body))
        self.assertEqual(C, body["anchor"])
        self.assertEqual(2, body["months_back"])
        self.assertEqual([B, C], [e["month"] for e in body["series"]])

        empty, latest = body["series"]
        self.assertEqual(
            ["gross_inflows", "month", "month_spend", "net_cash_flow",
             "savings_rate", "true_income", "unclassified_count"], sorted(latest))
        self.assertEqual(0.0, empty["true_income"])          # B is a zero month
        self.assertIsNone(empty["savings_rate"])
        self.assertEqual(3000.0, latest["true_income"])
        self.assertEqual(30.0, latest["month_spend"])        # 5000 - 2000 refund = 3000c
        self.assertEqual(2970.0, latest["net_cash_flow"])
        self.assertEqual(3020.0, latest["gross_inflows"])    # refund still money in

    def test_anchor_defaults_to_current_period(self):
        # No anchor passed -> server uses current_period(); the window still
        # returns months_back entries in chronological order.
        resp = self.client().get("/api/income/trend?months_back=3")
        self.assertEqual(200, resp.status_code)
        body = resp.get_json()
        self.assertEqual(self.app_module.current_period(), body["anchor"])
        self.assertEqual(3, len(body["series"]))

    def test_months_back_defaults_to_six_and_is_clamped(self):
        self.assertEqual(6, len(self.client().get(
            f"/api/income/trend?anchor={C}").get_json()["series"]))
        self.assertEqual(24, self.client().get(
            f"/api/income/trend?anchor={C}&months_back=999").get_json()["months_back"])
        self.assertEqual(1, self.client().get(
            f"/api/income/trend?anchor={C}&months_back=0").get_json()["months_back"])

    def test_bad_anchor_is_400(self):
        for bad in ("2026", "2026-13", "not-a-month"):
            resp = self.client().get(f"/api/income/trend?anchor={bad}")
            self.assertEqual(400, resp.status_code, bad)

    def test_bad_months_back_is_400(self):
        resp = self.client().get(f"/api/income/trend?anchor={C}&months_back=abc")
        self.assertEqual(400, resp.status_code)

    def test_requires_login(self):
        resp = self.client(logged_in=False).get("/api/income/trend")
        self.assertEqual(401, resp.status_code)


if __name__ == "__main__":
    unittest.main()
