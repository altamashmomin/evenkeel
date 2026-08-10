"""classify_inflow verb: the direction='in' submission criterion, the
income_type vocabulary, atomic edit-or-nothing, and audit coverage.

No verb creates direction='in' rows yet (the sync flip is a later
increment), so tests seed an inflow row directly via SQL -- ordinary
test-fixture setup, not an application write path."""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
sys.path.insert(0, str(REPO))

import _seedbase

import actions


class ClassifyInflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-classify-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=41, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def count(self, sql, *params):
        return self.db.execute(sql, params).fetchone()[0]

    def an_inflow(self, income_type=None):
        cur = self.db.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source, direction, income_type)
               VALUES (?, 320000, 'ADP PAYROLL 8842', 'Other', 1, 0,
                       'simplefin', 'in', ?)""",
            (date.today().isoformat(), income_type))
        self.db.commit()
        return cur.lastrowid

    def an_outflow(self):
        cur = self.db.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source)
               VALUES (?, 5000, 'Groceries', 'Groceries', 1, 0, 'manual')""",
            (date.today().isoformat(),))
        self.db.commit()
        return cur.lastrowid

    def test_classifies_an_unclassified_inflow_and_audits_before_after(self):
        txn_id = self.an_inflow(income_type="unclassified")
        row = actions.classify_inflow(self.db, "ui:avery", txn_id, "paycheck")
        self.assertEqual("paycheck", row["income_type"])

        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'classify_inflow'").fetchone()
        self.assertEqual("ui:avery", audit["actor"])
        self.assertEqual(f"transaction:{txn_id}", audit["target"])
        detail = json.loads(audit["detail_json"])
        self.assertEqual("unclassified", detail["before"])
        self.assertEqual("paycheck", detail["after"])

    def test_reclassifying_an_already_classified_row_is_allowed(self):
        txn_id = self.an_inflow(income_type="paycheck")
        row = actions.classify_inflow(self.db, "ui:avery", txn_id, "gift")
        self.assertEqual("gift", row["income_type"])

    def test_outflow_cannot_be_classified(self):
        txn_id = self.an_outflow()
        with self.assertRaisesRegex(
                actions.ActionError, "only inflows can be classified"):
            actions.classify_inflow(self.db, "ui:avery", txn_id, "paycheck")
        self.assertFalse(self.db.in_transaction)

    def test_invalid_income_type_rejected(self):
        txn_id = self.an_inflow(income_type="unclassified")
        with self.assertRaisesRegex(
                actions.ActionError, "income_type must be one of"):
            actions.classify_inflow(self.db, "ui:avery", txn_id, "bogus")

    def test_missing_transaction_raises_not_found(self):
        with self.assertRaises(actions.NotFound):
            actions.classify_inflow(self.db, "ui:avery", 999999, "paycheck")

    def test_every_documented_income_type_is_accepted(self):
        for income_type in actions.INCOME_TYPES:
            txn_id = self.an_inflow(income_type="unclassified")
            row = actions.classify_inflow(self.db, "ui:avery", txn_id, income_type)
            self.assertEqual(income_type, row["income_type"])

    def test_is_atomic_audit_or_nothing(self):
        txn_id = self.an_inflow(income_type="unclassified")
        self.db.execute("DROP TABLE audit_log")
        self.db.commit()
        with self.assertRaises(sqlite3.OperationalError):
            actions.classify_inflow(self.db, "ui:avery", txn_id, "paycheck")
        self.assertFalse(self.db.in_transaction)
        row = self.db.execute(
            "SELECT income_type FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        self.assertEqual("unclassified", row["income_type"])


if __name__ == "__main__":
    unittest.main()
