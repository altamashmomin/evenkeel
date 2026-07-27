"""savings_rate_trend derivation (analytics #9): per-month savings rate plus
a trailing 3-month ROLLING rate that smooths single-month noise. The rolling
rate is cumulative (Σ net_cash_flow ÷ Σ true_income), not an average of
ratios. Reuses income_trend, so the per-month rate is exactly the card's.
Hand-built fixtures make every expected number exact."""
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

A, B, C = "2026-05", "2026-06", "2026-07"


class SavingsRateTrendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-savings-trend-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "66", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def outflow(self, month, amount_cents):
        self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, ?, 'spend', 'Groceries', 1, 0, 'manual', 'out')""",
            (f"{month}-10", amount_cents))
        self.db.commit()

    def paycheck(self, month, amount_cents):
        self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, ?, 'pay', 'Other', 1, 0, 'simplefin', 'in', 'paycheck')""",
            (f"{month}-05", amount_cents))
        self.db.commit()

    def build_three_months(self):
        # A: income 100000, spend 40000 -> net 60000, rate 0.60
        self.paycheck(A, 100000); self.outflow(A, 40000)
        # B: income 200000, spend 50000 -> net 150000, rate 0.75
        self.paycheck(B, 200000); self.outflow(B, 50000)
        # C: income 100000, spend 90000 -> net 10000, rate 0.10 (a bad month)
        self.paycheck(C, 100000); self.outflow(C, 90000)

    def test_per_month_rate_matches_income_summary(self):
        self.build_three_months()
        series = derivations.savings_rate_trend(self.db, months_back=3, anchor=C)
        self.assertEqual([A, B, C], [e["month"] for e in series])
        self.assertEqual([0.6, 0.75, 0.1], [e["savings_rate"] for e in series])

    def test_rolling_rate_is_cumulative_and_smooths_the_bad_month(self):
        self.build_three_months()
        series = derivations.savings_rate_trend(self.db, months_back=3, anchor=C)
        # A alone: 60000/100000 = 0.60
        # A+B:     210000/300000 = 0.70
        # A+B+C:   220000/400000 = 0.55  (raw C is 0.10, rolling stays healthy)
        self.assertEqual([0.6, 0.7, 0.55],
                         [e["rolling_savings_rate"] for e in series])

    def test_rates_are_none_when_a_month_has_no_income(self):
        self.outflow(C, 5000)   # spend, no paycheck anywhere
        series = derivations.savings_rate_trend(self.db, months_back=1, anchor=C)
        self.assertIsNone(series[-1]["savings_rate"])
        self.assertIsNone(series[-1]["rolling_savings_rate"])

    def test_rolling_survives_a_zero_income_month_in_the_window(self):
        # A has income, B has none: rolling at B = (netA+netB)/(incA+0).
        self.paycheck(A, 100000); self.outflow(A, 40000)   # net 60000
        self.outflow(B, 20000)                             # net -20000, income 0
        series = derivations.savings_rate_trend(self.db, months_back=2, anchor=B)
        self.assertIsNone(series[1]["savings_rate"])       # B alone: no income
        # rolling at B: (60000 + -20000) / (100000 + 0) = 0.40
        self.assertEqual(0.4, series[1]["rolling_savings_rate"])

    def test_default_anchor_and_window(self):
        self.build_three_months()
        series = derivations.savings_rate_trend(self.db, months_back=2)
        self.assertEqual([B, C], [e["month"] for e in series])


if __name__ == "__main__":
    unittest.main()
