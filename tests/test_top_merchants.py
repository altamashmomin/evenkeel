"""top_merchants derivation (analytics #10): outflows grouped by description,
largest first — the 'who did we pay the most?' axis. Outflows only (no refund
netting, settlements excluded), so it must ignore inflows entirely."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
sys.path.insert(0, str(REPO))

import _seedbase
import derivations

M = "2026-06"


class TopMerchantsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-merchants-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=67, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def outflow(self, desc, cents, month=M, source="manual"):
        self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, ?, ?, 'X', 1, 0, ?, 'out')""",
            (f"{month}-10", cents, desc, source))
        self.db.commit()

    def inflow(self, desc, cents, month=M):
        self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, ?, ?, 'X', 1, 0, 'simplefin', 'in', 'refund')""",
            (f"{month}-10", cents, desc))
        self.db.commit()

    def test_groups_by_description_largest_first_with_counts(self):
        self.outflow("Amazon", 5000)
        self.outflow("Amazon", 3000)       # same merchant -> summed, count 2
        self.outflow("Whole Foods", 6000)
        self.outflow("Cafe", 1000)
        result = derivations.top_merchants(self.db, M)
        self.assertEqual(
            [("Amazon", 8000, 2), ("Whole Foods", 6000, 1), ("Cafe", 1000, 1)],
            [(m["description"], m["amount_cents"], m["count"]) for m in result])

    def test_limit_caps_the_list(self):
        for name, cents in (("A", 100), ("B", 200), ("C", 300)):
            self.outflow(name, cents)
        result = derivations.top_merchants(self.db, M, limit=2)
        self.assertEqual(["C", "B"], [m["description"] for m in result])

    def test_ignores_inflows_and_settlements(self):
        self.outflow("Amazon", 5000)
        self.inflow("Amazon refund", 999999)          # inflow — must not appear
        self.outflow("Settlement", 888888, source="settlement")  # excluded
        result = derivations.top_merchants(self.db, M)
        self.assertEqual([("Amazon", 5000, 1)],
                         [(m["description"], m["amount_cents"], m["count"]) for m in result])

    def test_month_none_spans_all_months(self):
        self.outflow("Amazon", 5000, month="2026-05")
        self.outflow("Amazon", 3000, month="2026-06")
        self.assertEqual(8000, derivations.top_merchants(self.db)[0]["amount_cents"])
        self.assertEqual(3000, derivations.top_merchants(self.db, "2026-06")[0]["amount_cents"])


if __name__ == "__main__":
    unittest.main()
