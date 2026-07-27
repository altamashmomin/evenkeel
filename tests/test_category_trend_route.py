"""GET /api/analytics/category-trend: presents the category_trend derivation
as dollars at the JSON edge — per-month net spend for one category, plus a
trailing 3-month rolling average and MoM delta. category is required."""
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
A, B, C = "2026-05", "2026-06", "2026-07"


class CategoryTrendRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-cat-trend-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "95", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM splits")
        conn.execute("DELETE FROM transactions")
        for month, cents in ((A, 3000), (B, 6000)):
            conn.execute(
                """INSERT INTO transactions (txn_date, amount_cents, description,
                       category, paid_by, is_shared, source, direction)
                   VALUES (?, ?, 'spend', 'Groceries', 1, 0, 'manual', 'out')""",
                (f"{month}-10", cents))
        conn.commit()
        conn.close()

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "cat-trend-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_cat_trend_route_test", REPO / "app.py")
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

    def test_shape_and_dollar_conversion(self):
        resp = self.client().get(
            f"/api/analytics/category-trend?category=Groceries&anchor={C}&months_back=3")
        self.assertEqual(200, resp.status_code)
        body = resp.get_json()
        self.assertEqual(["anchor", "category", "months_back", "series"], sorted(body))
        self.assertEqual("Groceries", body["category"])
        self.assertEqual([A, B, C], [e["month"] for e in body["series"]])
        a, b, c = body["series"]
        self.assertEqual(["mom_delta", "month", "rolling_avg", "spend"], sorted(c))
        self.assertEqual(30.0, a["spend"])          # 3000c
        self.assertIsNone(a["mom_delta"])           # first month
        self.assertEqual(60.0, b["spend"])
        self.assertEqual(30.0, b["mom_delta"])      # 6000-3000 = 3000c
        self.assertEqual(45.0, b["rolling_avg"])    # avg(3000,6000)=4500c
        self.assertEqual(0.0, c["spend"])           # no Groceries in C

    def test_category_is_required(self):
        resp = self.client().get("/api/analytics/category-trend")
        self.assertEqual(400, resp.status_code)
        self.assertEqual({"error": "category is required"}, resp.get_json())

    def test_bad_anchor_is_400(self):
        resp = self.client().get(
            "/api/analytics/category-trend?category=Groceries&anchor=2026-13")
        self.assertEqual(400, resp.status_code)

    def test_months_back_clamped(self):
        body = self.client().get(
            "/api/analytics/category-trend?category=Groceries&months_back=999").get_json()
        self.assertEqual(24, body["months_back"])

    def test_requires_login(self):
        resp = self.client(logged_in=False).get(
            "/api/analytics/category-trend?category=Groceries")
        self.assertEqual(401, resp.status_code)


if __name__ == "__main__":
    unittest.main()
