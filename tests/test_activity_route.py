"""GET /api/activity — the income-aware Activity read surface: the
spending/income filter, the extended txn shape (direction/income_type on
top of the frozen v1 fields), and the household-wide unclassified count.
/api/transactions stays byte-frozen — that guarantee is the parity test's
job, not repeated here."""
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
M = "2026-06"
OTHER = "2026-05"


class ActivityRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-activity-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=97, months=1)
        # Known slate: wipe the seed's rows so the fixture is exact.
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM splits")
        conn.execute("DELETE FROM transactions")
        # M: two outflows (different categories), one paycheck inflow, one
        # unclassified inflow.
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, 5000, 'Groceries', 'Groceries', 1, 0, 'manual', 'out')""",
            (f"{M}-10",))
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, 3200, 'Dinner out', 'Dining', 1, 0, 'manual', 'out')""",
            (f"{M}-11",))
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, 300000, 'ADP PAYROLL', 'Other', 1, 0, 'simplefin',
                       'in', 'paycheck')""",
            (f"{M}-05",))
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, 4700, 'Venmo', 'Other', 1, 0, 'simplefin',
                       'in', 'unclassified')""",
            (f"{M}-06",))
        # OTHER month: another unclassified inflow (proves the count is global).
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, 8000, 'Refund?', 'Other', 1, 0, 'simplefin',
                       'in', 'unclassified')""",
            (f"{OTHER}-20",))
        conn.commit()
        conn.close()

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "activity-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_activity_route_test", REPO / "app.py")
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

    def get(self, query=""):
        return self.client().get(f"/api/activity{query}")

    def test_all_filter_returns_both_directions_in_month(self):
        body = self.get(f"?month={M}").get_json()
        self.assertEqual(M, body["month"])
        self.assertEqual("all", body["filter"])
        dirs = sorted(t["direction"] for t in body["transactions"])
        self.assertEqual(["in", "in", "out", "out"], dirs)

    def test_spending_filter_returns_only_outflows(self):
        body = self.get(f"?month={M}&filter=spending").get_json()
        self.assertTrue(body["transactions"])
        self.assertTrue(all(t["direction"] == "out" for t in body["transactions"]))

    def test_income_filter_returns_only_inflows(self):
        body = self.get(f"?month={M}&filter=income").get_json()
        self.assertTrue(body["transactions"])
        self.assertTrue(all(t["direction"] == "in" for t in body["transactions"]))

    def test_extended_shape_adds_direction_and_income_type_over_v1_fields(self):
        body = self.get(f"?month={M}&filter=income").get_json()
        row = next(t for t in body["transactions"] if t["income_type"] == "paycheck")
        self.assertEqual(
            ["amount", "category", "date", "description", "direction", "id",
             "income_type", "is_shared", "is_transfer", "paid_by",
             "payer_share_pct", "source"],
            sorted(row))
        self.assertEqual("in", row["direction"])
        self.assertEqual("paycheck", row["income_type"])

    def test_unclassified_count_is_global_not_month_scoped(self):
        # Two unclassified inflows exist total (one in M, one in OTHER).
        # The count must be 2 regardless of which month is requested.
        self.assertEqual(2, self.get(f"?month={M}").get_json()["unclassified_count"])
        self.assertEqual(2, self.get(f"?month={OTHER}").get_json()["unclassified_count"])
        self.assertEqual(2, self.get("").get_json()["unclassified_count"])

    def test_month_filter_scopes_the_listing(self):
        other = self.get(f"?month={OTHER}").get_json()
        self.assertTrue(all(t["date"].startswith(OTHER) for t in other["transactions"]))
        self.assertEqual(1, len(other["transactions"]))

    def test_category_filter_narrows_to_that_tag(self):
        # The recategorize drill-in: spending in M tagged exactly 'Groceries'.
        body = self.get(f"?month={M}&filter=spending&category=Groceries").get_json()
        self.assertEqual("Groceries", body["category"])
        self.assertEqual(1, len(body["transactions"]))
        self.assertEqual("Groceries", body["transactions"][0]["category"])

    def test_category_filter_is_exact_not_substring(self):
        # 'Din' must not match 'Dining' — the filter matches the whole tag.
        body = self.get(f"?month={M}&filter=spending&category=Din").get_json()
        self.assertEqual([], body["transactions"])

    def test_absent_category_filter_returns_all_and_echoes_null(self):
        body = self.get(f"?month={M}&filter=spending").get_json()
        self.assertIsNone(body["category"])
        self.assertEqual(2, len(body["transactions"]))

    def test_invalid_filter_is_400(self):
        resp = self.get("?filter=bogus")
        self.assertEqual(400, resp.status_code)
        self.assertEqual(
            {"error": "filter must be all, spending, or income"}, resp.get_json())

    def test_requires_login(self):
        resp = self.client(logged_in=False).get("/api/activity")
        self.assertEqual(401, resp.status_code)


if __name__ == "__main__":
    unittest.main()
