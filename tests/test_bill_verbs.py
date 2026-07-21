"""mark_bill_paid / unmark_bill_paid verbs: validation order, atomic edit
with audit, and full reversal including links."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
sys.path.insert(0, str(REPO))

import actions


class BillVerbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-bill-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "11", "--months", "2", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        # A period no seeded payment can occupy: far future.
        self.period = "2030-01"
        self.bill_id = self.db.execute(
            "SELECT id FROM bills WHERE active = 1 ORDER BY id LIMIT 1"
        ).fetchone()["id"]

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def count(self, sql, *params):
        return self.db.execute(sql, params).fetchone()[0]

    def pay(self, data=None):
        return actions.mark_bill_paid(
            self.db, "ui:avery", self.bill_id,
            dict({"period": self.period}, **(data or {})), default_paid_by=1)

    def test_pay_writes_txn_splits_payment_and_audit(self):
        bill, period = self.pay()
        self.assertEqual(self.period, period)
        payment = self.db.execute(
            "SELECT * FROM bill_payments WHERE bill_id = ? AND period = ?",
            (self.bill_id, self.period)).fetchone()
        txn = self.db.execute(
            "SELECT * FROM transactions WHERE id = ?", (payment["txn_id"],)).fetchone()
        self.assertEqual("bill", txn["source"])
        self.assertEqual(bill["amount_cents"], txn["amount_cents"])
        self.assertEqual(date.today().isoformat(), txn["txn_date"])
        self.assertEqual(2, self.count(
            "SELECT COUNT(*) FROM splits WHERE transaction_id = ?", txn["id"]))
        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'mark_bill_paid'").fetchone()
        self.assertEqual("ui:avery", audit["actor"])
        self.assertEqual(f"bill:{self.bill_id}", audit["target"])
        detail = json.loads(audit["detail_json"])
        self.assertEqual(self.period, detail["period"])
        self.assertEqual(txn["id"], detail["transaction"])

    def test_duplicate_period_and_message_order_are_frozen(self):
        self.pay()
        with self.assertRaisesRegex(ValueError, "already marked paid for this period"):
            self.pay()
        with self.assertRaisesRegex(ValueError, "period must be YYYY-MM"):
            self.pay({"period": "2030/01"})
        with self.assertRaisesRegex(ValueError, "paid_by must be one of the two users"):
            actions.mark_bill_paid(
                self.db, "ui:avery", self.bill_id,
                {"period": "2030-02", "paid_by": 99}, default_paid_by=1)
        with self.assertRaises(actions.NotFound):
            actions.mark_bill_paid(
                self.db, "ui:avery", 9999, {"period": "2030-02"}, default_paid_by=1)

    def test_shared_pay_requires_exactly_two_members_but_unshared_works(self):
        self.db.execute("UPDATE members SET active = 0 WHERE id = 2")
        self.db.commit()
        with self.assertRaisesRegex(
                actions.ActionError, "shared transactions require exactly two"):
            self.pay()
        self.assertFalse(self.db.in_transaction)
        bill, period = self.pay({"is_shared": False})
        self.assertEqual(self.period, period)
        payment = self.db.execute(
            "SELECT txn_id FROM bill_payments WHERE bill_id = ? AND period = ?",
            (bill["id"], period)).fetchone()
        self.assertEqual(0, self.count(
            "SELECT COUNT(*) FROM splits WHERE transaction_id = ?", payment["txn_id"]))

    def test_shared_pay_rejects_three_active_members(self):
        self.db.execute(
            "INSERT INTO members (username, display_name, password_hash, active, created_at) "
            "VALUES ('casey', 'Casey', 'disabled', 1, '2026-07-19T00:00:00+00:00')")
        self.db.commit()
        with self.assertRaisesRegex(
                actions.ActionError, "shared transactions require exactly two"):
            self.pay()
        self.assertFalse(self.db.in_transaction)

    def test_share_percentage_requires_exact_basis_points(self):
        with self.assertRaisesRegex(
                ValueError, "payer_share_pct must use at most two decimal places"):
            self.pay({"payer_share_pct": "33.335"})
        self.assertFalse(self.db.in_transaction)

    def test_unpay_reverts_everything_including_links(self):
        self.pay()
        payment = self.db.execute(
            "SELECT * FROM bill_payments WHERE bill_id = ? AND period = ?",
            (self.bill_id, self.period)).fetchone()
        txn_id = payment["txn_id"]
        # A settlement dated far future covers the bill txn with a link.
        actions.settle_up(self.db, "ui:blake", {
            "date": "2030-02-01", "amount": 5.00,
            "description": "Settlement — link test", "category": "Settlement",
            "paid_by": 2, "is_shared": True, "payer_share_pct": 0,
            "source": "settlement"})
        self.assertEqual(1, self.count(
            "SELECT COUNT(*) FROM links WHERE to_id = ?", txn_id))

        actions.unmark_bill_paid(self.db, "ui:avery", self.bill_id, self.period)
        self.assertEqual(0, self.count(
            "SELECT COUNT(*) FROM transactions WHERE id = ?", txn_id))
        self.assertEqual(0, self.count(
            "SELECT COUNT(*) FROM splits WHERE transaction_id = ?", txn_id))
        self.assertEqual(0, self.count(
            "SELECT COUNT(*) FROM links WHERE from_id = ? OR to_id = ?",
            txn_id, txn_id))
        self.assertEqual(0, self.count(
            "SELECT COUNT(*) FROM bill_payments WHERE bill_id = ? AND period = ?",
            self.bill_id, self.period))
        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'unmark_bill_paid'").fetchone()
        self.assertEqual(f"bill:{self.bill_id}", audit["target"])
        with self.assertRaisesRegex(actions.NotFound, "no payment for this period"):
            actions.unmark_bill_paid(self.db, "ui:avery", self.bill_id, self.period)

    def test_pay_is_atomic_audit_or_nothing(self):
        self.db.execute("DROP TABLE audit_log")
        self.db.commit()
        txns = self.count("SELECT COUNT(*) FROM transactions")
        payments = self.count("SELECT COUNT(*) FROM bill_payments")
        with self.assertRaises(sqlite3.OperationalError):
            self.pay()
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(txns, self.count("SELECT COUNT(*) FROM transactions"))
        self.assertEqual(payments, self.count("SELECT COUNT(*) FROM bill_payments"))

    def test_unpay_is_atomic_audit_or_nothing(self):
        self.pay()
        payment = self.db.execute(
            "SELECT * FROM bill_payments WHERE bill_id = ? AND period = ?",
            (self.bill_id, self.period)).fetchone()
        txn_id = payment["txn_id"]
        self.db.execute("DROP TABLE audit_log")
        self.db.commit()

        with self.assertRaises(sqlite3.OperationalError):
            actions.unmark_bill_paid(
                self.db, "ui:avery", self.bill_id, self.period)

        self.assertFalse(self.db.in_transaction)
        self.assertEqual(1, self.count(
            "SELECT COUNT(*) FROM bill_payments WHERE id = ?", payment["id"]))
        self.assertEqual(1, self.count(
            "SELECT COUNT(*) FROM transactions WHERE id = ?", txn_id))


if __name__ == "__main__":
    unittest.main()
