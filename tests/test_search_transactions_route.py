"""GET /api/transactions/search: the assistant read tier's evidence tool.
Filtered (query/date/direction/income_type/category/paid_by), paginated
(limit/offset with total_matches + has_more), money as {cents, display},
paid_by resolved to a username. Read-only; no totals math here."""
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


class SearchTransactionsRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-search-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=98, months=1)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM splits")
        conn.execute("DELETE FROM transactions")
        rows = [
            # date, desc, cents, category, paid_by, direction, income_type
            ("2026-06-01", "Whole Foods", 5000, "Groceries", 1, "out", None),
            ("2026-06-05", "Trader Joes", 3000, "Groceries", 2, "out", None),
            ("2026-06-10", "Cafe Lumen", 1500, "Dining", 1, "out", None),
            ("2026-06-15", "ADP PAYROLL", 300000, "Other", 1, "in", "paycheck"),
            ("2026-07-01", "Amazon refund", 2000, "Groceries", 1, "in", "refund"),
        ]
        for d, desc, cents, cat, pb, direction, itype in rows:
            conn.execute(
                """INSERT INTO transactions (txn_date, amount_cents, description,
                       category, paid_by, is_shared, source, direction, income_type)
                   VALUES (?, ?, ?, ?, ?, 0, 'manual', ?, ?)""",
                (d, cents, desc, cat, pb, direction, itype))
        conn.commit()
        conn.close()

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "search-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_search_route_test", REPO / "app.py")
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

    def get(self, qs=""):
        return self.client().get(f"/api/transactions/search{qs}").get_json()

    def descs(self, body):
        return [t["description"] for t in body["transactions"]]

    def test_no_filters_returns_all_most_recent_first(self):
        body = self.get()
        self.assertEqual(5, body["total_matches"])
        self.assertFalse(body["has_more"])
        self.assertEqual("Amazon refund", body["transactions"][0]["description"])  # 07-01
        self.assertEqual("Whole Foods", body["transactions"][-1]["description"])   # 06-01

    def test_query_is_case_insensitive_substring(self):
        self.assertEqual(["Whole Foods"], self.descs(self.get("?query=whole")))

    def test_direction_and_income_type_filters(self):
        self.assertEqual(2, self.get("?direction=in")["total_matches"])
        self.assertEqual(["Amazon refund"], self.descs(self.get("?income_type=refund")))

    def test_category_and_paid_by_filters(self):
        self.assertEqual(3, self.get("?category=Groceries")["total_matches"])
        # paid_by is a username; member 2 is 'blake' (seed).
        self.assertEqual(["Trader Joes"], self.descs(self.get("?paid_by=blake")))

    def test_date_range_inclusive(self):
        self.assertEqual(3, self.get("?date_to=2026-06-10")["total_matches"])
        self.assertEqual(["Amazon refund"],
                         self.descs(self.get("?date_from=2026-07-01")))

    def test_filters_combine_with_and(self):
        body = self.get("?category=Groceries&direction=out")
        self.assertEqual(2, body["total_matches"])

    def test_pagination_reports_total_and_has_more(self):
        page1 = self.get("?limit=2")
        self.assertEqual(5, page1["total_matches"])
        self.assertTrue(page1["has_more"])
        self.assertEqual(2, len(page1["transactions"]))
        last = self.get("?limit=2&offset=4")
        self.assertEqual(1, len(last["transactions"]))
        self.assertFalse(last["has_more"])

    def test_money_is_dual_and_paid_by_is_username(self):
        t = self.get("?query=whole")["transactions"][0]
        self.assertEqual({"cents": 5000, "display": "$50.00"}, t["amount"])
        self.assertEqual("avery", t["paid_by"])
        self.assertEqual("out", t["direction"])
        self.assertIsNone(t["income_type"])

    def test_bad_inputs_are_400(self):
        self.assertEqual(400, self.client().get(
            "/api/transactions/search?direction=sideways").status_code)
        self.assertEqual(400, self.client().get(
            "/api/transactions/search?income_type=bogus").status_code)
        self.assertEqual(400, self.client().get(
            "/api/transactions/search?paid_by=nobody").status_code)

    def test_requires_login(self):
        self.assertEqual(401, self.client(logged_in=False)
                         .get("/api/transactions/search").status_code)


if __name__ == "__main__":
    unittest.main()
