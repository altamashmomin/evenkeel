"""category_trend derivation (increment 8): per-month NET spend for one
category over a trailing window, plus a trailing 3-month rolling average
and month-over-month delta. Rides the _monthly_series engine, so refund
netting flows through exactly as it does for spending_summary. Fixtures are
hand-built so every expected number is exact."""
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

A, B, C = "2026-05", "2026-06", "2026-07"   # C is the anchor month


class CategoryTrendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-cat-trend-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=65, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def outflow(self, month, amount_cents, category="Groceries"):
        self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, ?, 'spend', ?, 1, 0, 'manual', 'out')""",
            (f"{month}-10", amount_cents, category))
        self.db.commit()

    def refund(self, month, amount_cents, category="Groceries"):
        self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, ?, 'refund', ?, 1, 0, 'simplefin', 'in', 'refund')""",
            (f"{month}-05", amount_cents, category))
        self.db.commit()

    def spends(self, series):
        return [e["spend_cents"] for e in series]

    def test_per_month_spend_only_for_the_named_category(self):
        self.outflow(A, 3000, "Groceries")
        self.outflow(A, 9999, "Dining")       # other category — must not count
        self.outflow(B, 6000, "Groceries")
        # C: no Groceries spend at all -> a zero month.
        series = derivations.category_trend(self.db, "Groceries", months_back=3, anchor=C)
        self.assertEqual([A, B, C], [e["month"] for e in series])
        self.assertEqual([3000, 6000, 0], self.spends(series))

    def test_rolling_average_warms_up_over_three_months(self):
        self.outflow(A, 3000); self.outflow(B, 6000)   # C left at 0
        series = derivations.category_trend(self.db, "Groceries", months_back=3, anchor=C)
        self.assertEqual([3000, 4500, 3000],
                         [e["rolling_avg_cents"] for e in series])  # self, avg2, avg3

    def test_rolling_average_rounds_ties_to_even_integer_cents(self):
        self.outflow(A, 100); self.outflow(B, 100); self.outflow(C, 101)
        series = derivations.category_trend(self.db, "Groceries", months_back=3, anchor=C)
        self.assertEqual(100, series[-1]["rolling_avg_cents"])  # round_ratio(301,3)=100

    def test_mom_delta_is_none_first_then_exact_difference(self):
        self.outflow(A, 3000); self.outflow(B, 6000)   # C at 0
        series = derivations.category_trend(self.db, "Groceries", months_back=3, anchor=C)
        self.assertEqual([None, 3000, -6000],
                         [e["mom_delta_cents"] for e in series])

    def test_refund_nets_the_category_line_in_the_month_it_lands(self):
        self.outflow(C, 5000, "Groceries")
        self.refund(C, 2000, "Groceries")   # nets down to 3000
        series = derivations.category_trend(self.db, "Groceries", months_back=1, anchor=C)
        self.assertEqual(3000, series[-1]["spend_cents"])

    def test_default_anchor_is_latest_data_month(self):
        self.outflow(B, 4000)
        series = derivations.category_trend(self.db, "Groceries", months_back=2)
        self.assertEqual([A, B], [e["month"] for e in series])
        self.assertEqual(4000, series[-1]["spend_cents"])

    def test_empty_database_yields_empty_series(self):
        self.assertEqual([], derivations.category_trend(self.db, "Groceries"))


if __name__ == "__main__":
    unittest.main()
