"""income_summary derivation: which type each field counts (gross = every
inflow, true_income = paychecks only), net_cash_flow against the shared
spend total, the zero-income savings_rate guard, month scoping, and the
all-time (month=None) form. Fixtures are hand-built so every expected
number is exact rather than seed-dependent."""
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

M = "2026-06"          # the month under test
OTHER = "2026-05"      # a different month, to prove scoping


class IncomeSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-income-summary-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=91, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        # Known slate: every number below is hand-built, not seed-derived.
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def outflow(self, amount_cents, month=M, category="Groceries"):
        self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, ?, 'spend', ?, 1, 0, 'manual', 'out')""",
            (f"{month}-10", amount_cents, category))
        self.db.commit()

    def inflow(self, amount_cents, income_type, month=M):
        self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, ?, 'income', 'Other', 1, 0, 'simplefin', 'in', ?)""",
            (f"{month}-05", amount_cents, income_type))
        self.db.commit()

    def test_field_types_net_and_savings_rate(self):
        self.outflow(5000)                       # spend
        self.inflow(300000, "paycheck")          # true income + gross
        self.inflow(2000, "gift")                # gross only
        self.inflow(1500, "unclassified")        # gross only + the nudge
        s = derivations.income_summary(self.db, M)
        self.assertEqual(303500, s["gross_inflows_cents"])
        self.assertEqual(300000, s["true_income_cents"])
        self.assertEqual(5000, s["month_spend_cents"])
        self.assertEqual(295000, s["net_cash_flow_cents"])
        self.assertEqual(round(295000 / 300000, 4), s["savings_rate"])
        self.assertEqual(1, s["unclassified_count"])

    def test_refunds_transfers_gifts_are_gross_but_not_true_income(self):
        self.inflow(10000, "refund")
        self.inflow(20000, "transfer")
        self.inflow(30000, "gift")
        s = derivations.income_summary(self.db, M)
        self.assertEqual(60000, s["gross_inflows_cents"])
        self.assertEqual(0, s["true_income_cents"])

    def test_savings_rate_is_none_when_no_paycheck_income(self):
        self.outflow(5000)
        self.inflow(4700, "refund")   # money in, but not income to divide by
        s = derivations.income_summary(self.db, M)
        self.assertIsNone(s["savings_rate"])
        # The refund nets spend down to 300 (5000 - 4700), so net cash flow
        # is -300, not -5000 — refund netting flows through income_summary
        # via the shared spend total (increment 5).
        self.assertEqual(-300, s["net_cash_flow_cents"])

    def test_refund_nets_into_month_spend_and_cash_flow(self):
        # Positive statement of the increment-5 interaction: a refund reaches
        # income_summary through the one shared spend total. Spend 5000,
        # paycheck 300000, refund 2000 -> net spend 3000, cash flow 297000.
        self.outflow(5000)
        self.inflow(300000, "paycheck")
        self.inflow(2000, "refund")
        s = derivations.income_summary(self.db, M)
        self.assertEqual(3000, s["month_spend_cents"])
        self.assertEqual(297000, s["net_cash_flow_cents"])
        self.assertEqual(302000, s["gross_inflows_cents"])  # refund is money in
        self.assertEqual(300000, s["true_income_cents"])    # but not income

    def test_negative_net_cash_flow_when_spend_exceeds_income(self):
        self.outflow(400000)
        self.inflow(300000, "paycheck")
        s = derivations.income_summary(self.db, M)
        self.assertEqual(-100000, s["net_cash_flow_cents"])
        self.assertEqual(round(-100000 / 300000, 4), s["savings_rate"])

    def test_month_scoping_excludes_other_months(self):
        self.inflow(300000, "paycheck", month=M)
        self.inflow(999999, "paycheck", month=OTHER)
        self.outflow(5000, month=M)
        self.outflow(111111, month=OTHER)
        s = derivations.income_summary(self.db, M)
        self.assertEqual(300000, s["gross_inflows_cents"])
        self.assertEqual(300000, s["true_income_cents"])
        self.assertEqual(5000, s["month_spend_cents"])

    def test_all_time_form_sums_across_months(self):
        self.inflow(300000, "paycheck", month=M)
        self.inflow(200000, "paycheck", month=OTHER)
        self.outflow(5000, month=M)
        self.outflow(7000, month=OTHER)
        s = derivations.income_summary(self.db)   # month=None -> all-time
        self.assertEqual(500000, s["gross_inflows_cents"])
        self.assertEqual(500000, s["true_income_cents"])
        self.assertEqual(12000, s["month_spend_cents"])
        self.assertEqual(488000, s["net_cash_flow_cents"])

    def test_empty_month_is_all_zeros_and_null_rate(self):
        s = derivations.income_summary(self.db, M)
        self.assertEqual(0, s["gross_inflows_cents"])
        self.assertEqual(0, s["true_income_cents"])
        self.assertEqual(0, s["net_cash_flow_cents"])
        self.assertIsNone(s["savings_rate"])
        self.assertEqual(0, s["unclassified_count"])


if __name__ == "__main__":
    unittest.main()
