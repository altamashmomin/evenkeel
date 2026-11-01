"""mark_bill_paid's optional `amount` (what was actually paid — utilities vary)
and the split it already accepted but the dialog never exposed.

Pins: absent/blank amount is the frozen old behaviour (the bill's usual
amount); a sent amount becomes the logged transaction's amount, the audit's
amount_cents, and bill_variance's actual; bad amounts are refused with nothing
written; payer_share_pct reaches the split rows."""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _seedbase

import actions
import derivations

PERIOD = "2030-01"


class PayAmountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-bill-amount-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=31, months=2)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.bill = actions.create_bill(self.db, "ui:avery", {
            "name": "Electric & gas", "amount": "140.00", "due_day": 15,
            "category": "Utilities"})

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def pay(self, **data):
        return actions.mark_bill_paid(
            self.db, "ui:avery", self.bill["id"],
            dict({"period": PERIOD}, **data), default_paid_by=1)

    def logged(self):
        return self.db.execute(
            "SELECT t.* FROM bill_payments bp JOIN transactions t ON t.id = bp.txn_id "
            "WHERE bp.bill_id = ? AND bp.period = ?",
            (self.bill["id"], PERIOD)).fetchone()

    def test_absent_or_blank_amount_is_the_usual_amount(self):
        for data in ({}, {"amount": None}, {"amount": ""}, {"amount": "  "}):
            with self.subTest(data=data):
                self.pay(**data)
                self.assertEqual(14000, self.logged()["amount_cents"])
                actions.unmark_bill_paid(self.db, "ui:avery", self.bill["id"], PERIOD)

    def test_sent_amount_is_what_gets_logged_and_reported(self):
        self.pay(amount="151.37")
        txn = self.logged()
        self.assertEqual(15137, txn["amount_cents"])
        detail = json.loads(self.db.execute(
            "SELECT detail_json FROM audit_log WHERE action = 'mark_bill_paid' "
            "ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(15137, detail["amount_cents"])
        row = next(b for b in derivations.bill_variance(self.db, PERIOD)
                   if b["bill_id"] == self.bill["id"])
        self.assertEqual((14000, 15137, 1137),
                         (row["defined_cents"], row["actual_cents"], row["variance_cents"]))
        # The definition is untouched: next month's usual is still $140.
        self.assertEqual(14000, self.db.execute(
            "SELECT amount_cents FROM bills WHERE id = ?",
            (self.bill["id"],)).fetchone()[0])
        # And spend moved by exactly the actual amount.
        month = date.today().isoformat()[:7]
        actions.unmark_bill_paid(self.db, "ui:avery", self.bill["id"], PERIOD)
        before = derivations.spending_summary(self.db, month)[month]["total_cents"]
        self.pay(amount=98.5)
        after = derivations.spending_summary(self.db, month)[month]["total_cents"]
        self.assertEqual(9850, after - before)

    def test_bad_amounts_are_refused_and_nothing_is_written(self):
        before = self.db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        cases = [("abc", "invalid amount"), (True, "invalid amount"),
                 ("Infinity", "invalid amount"), ("NaN", "invalid amount"),
                 ("0", "amount must be positive"), (-5, "amount must be positive"),
                 ("1e30", "amount is too large")]
        for value, msg in cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(actions.ActionError, msg):
                    self.pay(amount=value)
        self.assertEqual(before, self.db.execute(
            "SELECT COUNT(*) FROM transactions").fetchone()[0])
        self.assertIsNone(self.logged())

    def test_payer_share_reaches_the_splits(self):
        self.pay(amount="200", payer_share_pct=60, paid_by=1)
        shares = dict(self.db.execute(
            "SELECT member_id, share_bp FROM splits WHERE transaction_id = ?",
            (self.logged()["id"],)).fetchall())
        self.assertEqual(6000, shares[1])
        self.assertEqual(4000, sum(v for k, v in shares.items() if k != 1))


class PayAmountRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-bill-amount-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=37, months=1)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "bill-amount-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_bill_amount_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.bill_id = actions.create_bill(conn, "ui:avery", {
            "name": "Electric & gas", "amount": "140.00", "due_day": 15})["id"]
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_pay_with_amount_keeps_the_v1_shape_and_feeds_variance(self):
        c = self.app_module.app.test_client()
        with c.session_transaction() as session:
            session["user_id"] = 1
        r = c.post(f"/api/bills/{self.bill_id}/pay",
                   json={"period": PERIOD, "amount": "151.37", "is_shared": True,
                         "payer_share_pct": "50"})
        self.assertEqual(201, r.status_code)
        body = r.get_json()
        self.assertEqual(
            ["amount", "category", "due_day", "id", "name",
             "paid_on", "paid_this_period", "period"], sorted(body))
        self.assertEqual(140.0, body["amount"])   # the bill's usual, unchanged
        v = c.get(f"/api/analytics/bill-variance?period={PERIOD}").get_json()
        row = next(b for b in v["bills"] if b["bill_id"] == self.bill_id)
        self.assertEqual({"cents": 15137, "display": "$151.37"}, row["actual"])
        bad = c.post(f"/api/bills/{self.bill_id}/pay",
                     json={"period": "2030-02", "amount": "abc"})
        self.assertEqual(400, bad.status_code)
        self.assertEqual({"error": "invalid amount"}, bad.get_json())


if __name__ == "__main__":
    unittest.main()
