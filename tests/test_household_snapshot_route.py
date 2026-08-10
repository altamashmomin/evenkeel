"""GET /api/household_snapshot: the assistant layer's one-call overview.
Composes balance + spending (net of refunds) + income + goals + bills, every
money field as {cents, display}, running no math of its own. Fixtures are
hand-built so every number is exact — and, because the endpoint only
re-presents the canonical derivations, matching those numbers proves it
can't disagree with the dashboard."""
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


class HouseholdSnapshotRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-snapshot-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=96, months=1)
        conn = sqlite3.connect(self.db_path)
        for t in ("splits", "transactions", "goal_contributions", "goals",
                  "bill_payments", "bills"):
            conn.execute(f"DELETE FROM {t}")
        # A shared Rent outflow (6000, 50/50) -> member 2 owes member 1 3000.
        cur = conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, 6000, 'Rent', 'Rent', 1, 1, 'manual', 'out')""",
            (f"{M}-01",))
        rent_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO splits (transaction_id, member_id, share_bp) VALUES (?, ?, ?)",
            [(rent_id, 1, 5000), (rent_id, 2, 5000)])
        # A personal Groceries outflow (5000) with a 2000 refund -> nets to 3000.
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, 5000, 'Groceries', 'Groceries', 1, 0, 'manual', 'out')""",
            (f"{M}-10",))
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, 2000, 'Grocery refund', 'Groceries', 1, 0,
                       'simplefin', 'in', 'refund')""",
            (f"{M}-12",))
        # A paycheck (300000) and nothing else uncategorized.
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, 300000, 'ADP PAYROLL', 'Other', 1, 0,
                       'simplefin', 'in', 'paycheck')""",
            (f"{M}-05",))
        # A goal (target 100000, saved 25000) and a bill.
        cur = conn.execute(
            "INSERT INTO goals (name, target_cents, created_at) VALUES ('Vacation', 100000, ?)",
            (f"{M}-01",))
        conn.execute(
            "INSERT INTO goal_contributions (goal_id, user_id, amount_cents, c_date) "
            "VALUES (?, 1, 25000, ?)", (cur.lastrowid, f"{M}-02"))
        conn.execute(
            "INSERT INTO bills (name, amount_cents, due_day, category, active) "
            "VALUES ('Internet', 8000, 15, 'Utilities', 1)")
        conn.commit()
        conn.close()

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "snapshot-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_snapshot_route_test", REPO / "app.py")
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

    def test_snapshot_composes_every_section_in_cents_and_display(self):
        body = self.client().get(f"/api/household_snapshot?month={M}").get_json()
        self.assertEqual(["balance", "bills", "goals", "income", "month", "spend"],
                         sorted(body))
        self.assertEqual(M, body["month"])

        # Spend: Rent 6000 + Groceries (5000 - 2000 refund) 3000 = 9000, netted.
        self.assertEqual(9000, body["spend"]["total"]["cents"])
        self.assertEqual("$90.00", body["spend"]["total"]["display"])
        by_cat = {c["category"]: c["amount"]["cents"] for c in body["spend"]["by_category"]}
        self.assertEqual({"Rent": 6000, "Groceries": 3000}, by_cat)

        # Balance: member 2 (Blake) owes member 1 (Avery) 3000 from the split.
        self.assertEqual("owing", body["balance"]["state"])
        self.assertEqual(3000, body["balance"]["cents"])
        self.assertEqual("$30.00", body["balance"]["display"])
        self.assertEqual("Blake", body["balance"]["ower"])
        self.assertEqual("Avery", body["balance"]["owed"])

        # Income: refund is gross but not true income; net = 300000 - 9000.
        self.assertEqual(302000, body["income"]["gross_inflows"]["cents"])
        self.assertEqual(300000, body["income"]["true_income"]["cents"])
        self.assertEqual(291000, body["income"]["net_cash_flow"]["cents"])
        self.assertEqual("$2,910.00", body["income"]["net_cash_flow"]["display"])
        self.assertEqual(round(291000 / 300000, 4), body["income"]["savings_rate"])
        self.assertEqual(0, body["income"]["unclassified_count"])

        # Goals and bills.
        self.assertEqual(1, len(body["goals"]))
        self.assertEqual(("Vacation", 100000, 25000, 0.25),
                         (body["goals"][0]["name"], body["goals"][0]["target"]["cents"],
                          body["goals"][0]["saved"]["cents"], body["goals"][0]["progress"]))
        self.assertEqual(1, len(body["bills"]))
        self.assertEqual(("Internet", 8000, 15, False),
                         (body["bills"][0]["name"], body["bills"][0]["amount"]["cents"],
                          body["bills"][0]["due_day"], body["bills"][0]["paid_this_period"]))

    def test_negative_net_cash_flow_displays_with_leading_minus(self):
        # Overspend month: wipe income, keep the outflows -> net cash flow < 0.
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM transactions WHERE direction = 'in'")
        conn.commit(); conn.close()
        body = self.client().get(f"/api/household_snapshot?month={M}").get_json()
        # Deleting inflows also removes the refund, so Groceries un-nets to
        # 5000: spend total is 6000 + 5000 = 11000, income 0 -> net -11000.
        self.assertEqual(-11000, body["income"]["net_cash_flow"]["cents"])
        self.assertEqual("-$110.00", body["income"]["net_cash_flow"]["display"])
        self.assertIsNone(body["income"]["savings_rate"])   # no income to divide by

    def test_default_month_is_current_period(self):
        body = self.client().get("/api/household_snapshot").get_json()
        self.assertEqual(self.app_module.current_period(), body["month"])

    def test_requires_login(self):
        self.assertEqual(401, self.client(logged_in=False)
                         .get("/api/household_snapshot").status_code)


if __name__ == "__main__":
    unittest.main()
