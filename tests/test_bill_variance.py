"""bill_variance derivation (analytics #12): defined bill amount vs what was
actually paid (bill_payments -> transactions) vs variance, per active bill,
for a period. Unpaid bills report None. Hand-built for exact numbers."""
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
sys.path.insert(0, str(REPO))

import derivations

P = "2026-06"


class BillVarianceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-bill-var-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "69", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        for t in ("splits", "transactions", "bill_payments", "bills"):
            self.db.execute(f"DELETE FROM {t}")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def a_bill(self, name, defined_cents, due_day=1, active=1):
        cur = self.db.execute(
            "INSERT INTO bills (name, amount_cents, due_day, category, active) "
            "VALUES (?, ?, ?, 'Utilities', ?)", (name, defined_cents, due_day, active))
        self.db.commit()
        return cur.lastrowid

    def pay(self, bill_id, actual_cents, period=P):
        cur = self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, ?, 'bill', 'Utilities', 1, 0, 'manual', 'out')""",
            (f"{period}-05", actual_cents))
        self.db.execute(
            "INSERT INTO bill_payments (bill_id, period, paid_on, txn_id) "
            "VALUES (?, ?, ?, ?)", (bill_id, period, f"{period}-05", cur.lastrowid))
        self.db.commit()

    def by_name(self, period=P):
        return {b["name"]: b for b in derivations.bill_variance(self.db, period)}

    def test_over_budget_is_positive_variance(self):
        bid = self.a_bill("Electric", 9000)
        self.pay(bid, 10200)                 # $12 over
        e = self.by_name()["Electric"]
        self.assertEqual((9000, 10200, 1200, True),
                         (e["defined_cents"], e["actual_cents"],
                          e["variance_cents"], e["paid"]))

    def test_under_budget_is_negative_variance(self):
        bid = self.a_bill("Water", 6000)
        self.pay(bid, 5500)
        self.assertEqual(-500, self.by_name()["Water"]["variance_cents"])

    def test_unpaid_bill_reports_none(self):
        self.a_bill("Internet", 6500)
        i = self.by_name()["Internet"]
        self.assertIsNone(i["actual_cents"])
        self.assertIsNone(i["variance_cents"])
        self.assertFalse(i["paid"])

    def test_inactive_bill_excluded(self):
        self.a_bill("Old Gym", 4000, active=0)
        self.assertNotIn("Old Gym", self.by_name())

    def test_period_scoping(self):
        bid = self.a_bill("Electric", 9000)
        self.pay(bid, 10000, period="2026-05")   # paid in a DIFFERENT period
        self.assertIsNone(self.by_name("2026-06")["Electric"]["actual_cents"])
        self.assertEqual(10000, self.by_name("2026-05")["Electric"]["actual_cents"])


if __name__ == "__main__":
    unittest.main()
