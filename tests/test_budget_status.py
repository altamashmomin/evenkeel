"""budget_status derivation (Analytics Tier C, BUDGETS-DESIGN): category limits
vs actual NET spend (refund-netted via spending_summary), remaining/over/pct, and
the unbudgeted-spend total so nothing hides."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _seedbase
import actions       # noqa: E402
import derivations   # noqa: E402

SEED_AS_OF = "2026-07-19"
M = "2026-06"


class BudgetStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-budget-status-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=51, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        # clean slate so spend is exactly what each test inserts
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _out(self, category, cents, day):
        self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction) "
            "VALUES (?, ?, 'spend', ?, 1, 0, 'manual', 'out')",
            (f"{M}-{day}", cents, category))
        self.db.commit()

    def _refund(self, category, cents, day):
        self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction, income_type) "
            "VALUES (?, ?, 'refund', ?, 1, 0, 'simplefin', 'in', 'refund')",
            (f"{M}-{day}", cents, category))
        self.db.commit()

    def _cat(self, status, category):
        return next(b for b in status["budgets"] if b["category"] == category)

    def test_actual_is_refund_netted_against_the_limit(self):
        actions.set_budget(self.db, "ui:avery", {"category": "Groceries", "amount": "500"})
        self._out("Groceries", 30000, "05")     # $300 spent
        self._refund("Groceries", 5000, "10")    # $50 back → net $250
        g = self._cat(derivations.budget_status(self.db, M), "Groceries")
        self.assertEqual(50000, g["budgeted_cents"])
        self.assertEqual(25000, g["actual_cents"])      # refund-netted
        self.assertEqual(25000, g["remaining_cents"])
        self.assertFalse(g["over"])
        self.assertEqual(50, g["pct"])                  # 25000 / 50000

    def test_over_budget_flags_and_negative_remaining(self):
        actions.set_budget(self.db, "ui:avery", {"category": "Dining", "amount": "100"})
        self._out("Dining", 15000, "05")         # $150 on a $100 limit
        d = self._cat(derivations.budget_status(self.db, M), "Dining")
        self.assertTrue(d["over"])
        self.assertEqual(-5000, d["remaining_cents"])
        self.assertEqual(150, d["pct"])

    def test_unbudgeted_spend_is_summed_separately(self):
        actions.set_budget(self.db, "ui:avery", {"category": "Groceries", "amount": "500"})
        self._out("Groceries", 10000, "05")
        self._out("Gas", 8000, "06")             # no budget for Gas
        status = derivations.budget_status(self.db, M)
        self.assertEqual(["Groceries"], [b["category"] for b in status["budgets"]])
        self.assertEqual(8000, status["unbudgeted_spend_cents"])   # Gas, not in a budget

    def test_over_budget_sorts_first(self):
        actions.set_budget(self.db, "ui:avery", {"category": "Groceries", "amount": "500"})
        actions.set_budget(self.db, "ui:avery", {"category": "Dining", "amount": "100"})
        self._out("Groceries", 10000, "05")      # under
        self._out("Dining", 20000, "06")         # over
        cats = [b["category"] for b in derivations.budget_status(self.db, M)["budgets"]]
        self.assertEqual(["Dining", "Groceries"], cats)   # over first


if __name__ == "__main__":
    unittest.main()
