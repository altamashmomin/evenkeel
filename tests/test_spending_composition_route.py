"""GET /api/analytics/spending-composition: what made up a month's spend —
per-category NET spend with share-of-total, plus top merchants. Shares are
ratios over spending_summary (refund-netted); merchants are outflows only."""
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


class SpendingCompositionRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-composition-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=99, months=1)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM splits")
        conn.execute("DELETE FROM transactions")
        out = [("Rent", 6000, "Rent"), ("Whole Foods", 3000, "Groceries"),
               ("Amazon", 1000, "Groceries")]
        for desc, cents, cat in out:
            conn.execute(
                """INSERT INTO transactions (txn_date, amount_cents, description,
                       category, paid_by, is_shared, source, direction)
                   VALUES (?, ?, ?, ?, 1, 0, 'manual', 'out')""",
                (f"{M}-10", cents, desc, cat))
        # A refund nets Groceries 4000 -> 3000; total 6000+3000 = 9000.
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, 1000, 'Grocery refund', 'Groceries', 1, 0,
                       'simplefin', 'in', 'refund')""",
            (f"{M}-12",))
        conn.commit()
        conn.close()

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "composition-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_composition_route_test", REPO / "app.py")
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

    def test_category_shares_are_net_of_refunds(self):
        body = self.client().get(
            f"/api/analytics/spending-composition?month={M}").get_json()
        self.assertEqual(["by_category", "month", "top_merchants", "total"],
                         sorted(body))
        self.assertEqual({"cents": 9000, "display": "$90.00"}, body["total"])
        by_cat = {c["category"]: (c["amount"]["cents"], c["share"])
                  for c in body["by_category"]}
        self.assertEqual((6000, round(6000 / 9000, 4)), by_cat["Rent"])
        self.assertEqual((3000, round(3000 / 9000, 4)), by_cat["Groceries"])  # netted

    def test_top_merchants_are_outflows_only_dual_money(self):
        body = self.client().get(
            f"/api/analytics/spending-composition?month={M}").get_json()
        merchants = [(m["description"], m["amount"]["cents"], m["count"])
                     for m in body["top_merchants"]]
        self.assertEqual(
            [("Rent", 6000, 1), ("Whole Foods", 3000, 1), ("Amazon", 1000, 1)],
            merchants)  # the refund inflow is absent
        self.assertEqual("$60.00", body["top_merchants"][0]["amount"]["display"])

    def test_merchant_limit_clamps(self):
        body = self.client().get(
            f"/api/analytics/spending-composition?month={M}&merchant_limit=2").get_json()
        self.assertEqual(2, len(body["top_merchants"]))

    def test_default_month_is_current_period(self):
        body = self.client().get("/api/analytics/spending-composition").get_json()
        self.assertEqual(self.app_module.current_period(), body["month"])

    def test_requires_login(self):
        self.assertEqual(401, self.client(logged_in=False)
                         .get("/api/analytics/spending-composition").status_code)


if __name__ == "__main__":
    unittest.main()
