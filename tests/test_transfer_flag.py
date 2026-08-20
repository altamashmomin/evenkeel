"""transactions.is_transfer (transfer-neutral fix, increment T1): a row flagged
is_transfer=1 must drop out of EVERY money derivation — spend, income, the
who-owes-whom balance, the per-member breakdown, and the settle-up breakdown —
so a mis-signed 'Payment Thank You' can be marked a transfer and stop counting
as income (and its outflow leg stop counting as spend). The verb + UI that set
the flag are increment T2; this proves the derivations honor it."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _seedbase

import actions
import derivations

MONTH = "2026-07"


class TransferFlagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-transfer-flag-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=53, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.commit()
        self.m = derivations.compute_balance(self.db)["members"]

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_column_exists_and_defaults_zero(self):
        cols = {r[1]: r for r in self.db.execute("PRAGMA table_info(transactions)")}
        self.assertIn("is_transfer", cols)
        # NOT NULL DEFAULT 0.
        self.assertEqual(1, cols["is_transfer"][3], "is_transfer should be NOT NULL")
        self.assertEqual("0", str(cols["is_transfer"][4]), "default should be 0")

    def _inflow(self, cents, income_type="paycheck", is_transfer=0):
        self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction, income_type, is_transfer) "
            "VALUES (?, ?, 'Payment Thank You', 'Other', ?, 0, 'simplefin', 'in', ?, ?)",
            (f"{MONTH}-05", cents, self.m[0]["id"], income_type, is_transfer))
        self.db.commit()

    def _shared_out(self, cents, payer_id, is_transfer=0):
        cur = self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction, is_transfer) "
            "VALUES (?, ?, 'x', 'Groceries', ?, 1, 'manual', 'out', ?)",
            (f"{MONTH}-10", cents, payer_id, is_transfer))
        actions.write_legacy_two_member_splits(
            self.db, cur.lastrowid, payer_id, True, 50, self.m)
        self.db.commit()

    def test_transfer_inflow_excluded_from_income(self):
        self._inflow(27036, income_type="unclassified")           # the Chase case
        before = derivations.income_summary(self.db, MONTH)
        self.db.execute("UPDATE transactions SET is_transfer = 1")
        self.db.commit()
        after = derivations.income_summary(self.db, MONTH)
        self.assertEqual(27036, before["gross_inflows_cents"])
        self.assertEqual(0, after["gross_inflows_cents"], "transfer still in gross")
        self.assertEqual(1, before["unclassified_count"])
        self.assertEqual(0, after["unclassified_count"], "transfer still nagging")

    def test_transfer_paycheck_excluded_from_true_income(self):
        self._inflow(300000, income_type="paycheck", is_transfer=1)
        s = derivations.income_summary(self.db, MONTH)
        self.assertEqual(0, s["true_income_cents"])
        self.assertEqual(0, s["gross_inflows_cents"])

    def test_transfer_outflow_excluded_from_spend(self):
        self._shared_out(5000, self.m[0]["id"])                    # normal spend
        self._shared_out(9000, self.m[0]["id"], is_transfer=1)     # a transfer debit
        summary = derivations.spending_summary(self.db, MONTH)[MONTH]
        self.assertEqual(5000, summary["total_cents"], "transfer counted as spend")

    def test_transfer_outflow_excluded_from_balance_and_breakdown(self):
        self._shared_out(10000, self.m[0]["id"])                   # 2nd owes 5000
        self._shared_out(4000, self.m[0]["id"], is_transfer=1)     # would add 2000 if counted
        bal = derivations.compute_balance(self.db)
        self.assertEqual(5000, bal["amount_cents"], "transfer moved the balance")
        bd = derivations.settle_breakdown(self.db)
        self.assertEqual(5000, bd["amount_cents"])
        self.assertEqual(1, len(bd["lines"]), "transfer showed as an owed line")
        self.assertEqual(0, bd["carryover_cents"])

    def test_transfer_excluded_from_member_breakdown(self):
        self._shared_out(10000, self.m[0]["id"], is_transfer=1)
        rows = derivations.member_breakdown(self.db, MONTH)
        # A transfer touches neither member's paid nor owed.
        self.assertTrue(all(r["paid_cents"] == 0 and r["owed_cents"] == 0 for r in rows))

    def test_non_transfer_rows_are_unchanged(self):
        # Sanity: with everything is_transfer=0 (the default), the money numbers
        # are exactly what they were before the column existed.
        self._inflow(300000, income_type="paycheck")
        self._shared_out(6000, self.m[0]["id"])
        s = derivations.income_summary(self.db, MONTH)
        self.assertEqual(300000, s["true_income_cents"])
        self.assertEqual(6000, s["month_spend_cents"])
        self.assertEqual(3000, derivations.compute_balance(self.db)["amount_cents"])


if __name__ == "__main__":
    unittest.main()
