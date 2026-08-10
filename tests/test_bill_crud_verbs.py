"""create_bill / update_bill / delete_bill verbs: the deployed route's
exact validation order and messages (including update_bill's asymmetric
combined message), atomic edit-or-nothing, and audit coverage for a
write path that previously had none."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
sys.path.insert(0, str(REPO))

import _seedbase

import actions


class BillCrudVerbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-bill-crud-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=13, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def count(self, sql, *params):
        return self.db.execute(sql, params).fetchone()[0]

    def bill_payload(self, **overrides):
        body = {"name": "Internet", "amount": 60, "due_day": 15,
                "category": "Internet"}
        body.update(overrides)
        return body

    # ---------------------------------------------------------------- create

    def test_create_writes_row_and_audit(self):
        bill = actions.create_bill(self.db, "ui:avery", self.bill_payload())
        self.assertEqual("Internet", bill["name"])
        self.assertEqual(6000, bill["amount_cents"])
        self.assertEqual(15, bill["due_day"])
        self.assertEqual(1, bill["active"])

        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'create_bill'").fetchone()
        self.assertEqual("ui:avery", audit["actor"])
        self.assertEqual(f"bill:{bill['id']}", audit["target"])
        detail = json.loads(audit["detail_json"])
        self.assertEqual(6000, detail["bill"]["amount_cents"])

    def test_create_category_defaults_to_bills(self):
        bill = actions.create_bill(
            self.db, "ui:avery", self.bill_payload(category=""))
        self.assertEqual("Bills", bill["category"])

    def test_create_frozen_messages_and_order(self):
        with self.assertRaisesRegex(actions.ActionError, "name is required"):
            actions.create_bill(self.db, "ui:avery", self.bill_payload(name=""))
        with self.assertRaisesRegex(
                actions.ActionError, "invalid amount or due day"):
            actions.create_bill(self.db, "ui:avery", self.bill_payload(amount="nope"))
        with self.assertRaisesRegex(
                actions.ActionError, "invalid amount or due day"):
            actions.create_bill(self.db, "ui:avery", self.bill_payload(due_day="nope"))
        with self.assertRaisesRegex(actions.ActionError, "amount must be positive"):
            actions.create_bill(self.db, "ui:avery", self.bill_payload(amount=0))
        with self.assertRaisesRegex(
                actions.ActionError, "due day must be between 1 and 31"):
            actions.create_bill(self.db, "ui:avery", self.bill_payload(due_day=32))

    def test_create_is_atomic_audit_or_nothing(self):
        self.db.execute("DROP TABLE audit_log")
        self.db.commit()
        before = self.count("SELECT COUNT(*) FROM bills")
        with self.assertRaises(sqlite3.OperationalError):
            actions.create_bill(self.db, "ui:avery", self.bill_payload())
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(before, self.count("SELECT COUNT(*) FROM bills"))

    # ---------------------------------------------------------------- update

    def test_update_missing_bill_raises_not_found(self):
        with self.assertRaises(actions.NotFound):
            actions.update_bill(self.db, "ui:avery", 999999, {"amount": 5})

    def test_update_changes_fields_and_audits_before_after(self):
        bill = actions.create_bill(self.db, "ui:avery", self.bill_payload())
        updated = actions.update_bill(
            self.db, "ui:blake", bill["id"], {"amount": 65.5})
        self.assertEqual(6550, updated["amount_cents"])
        self.assertEqual("Internet", updated["name"])  # untouched field survives

        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'update_bill'").fetchone()
        self.assertEqual("ui:blake", audit["actor"])
        detail = json.loads(audit["detail_json"])
        self.assertEqual(6000, detail["before"]["amount_cents"])
        self.assertEqual(6550, detail["after"]["amount_cents"])

    def test_update_partial_payload_carries_forward_existing_values(self):
        bill = actions.create_bill(self.db, "ui:avery", self.bill_payload())
        updated = actions.update_bill(
            self.db, "ui:avery", bill["id"], {"name": "Broadband"})
        self.assertEqual("Broadband", updated["name"])
        self.assertEqual(6000, updated["amount_cents"])
        self.assertEqual(15, updated["due_day"])

    def test_update_uses_one_combined_message_unlike_create(self):
        # Deployed asymmetry, preserved: create_bill distinguishes "amount
        # must be positive" from "due day must be between 1 and 31";
        # update_bill uses one message for both.
        bill = actions.create_bill(self.db, "ui:avery", self.bill_payload())
        with self.assertRaisesRegex(
                actions.ActionError, "^invalid amount or due day$"):
            actions.update_bill(self.db, "ui:avery", bill["id"], {"amount": 0})
        with self.assertRaisesRegex(
                actions.ActionError, "^invalid amount or due day$"):
            actions.update_bill(self.db, "ui:avery", bill["id"], {"due_day": 32})

    def test_update_is_atomic_audit_or_nothing(self):
        bill = actions.create_bill(self.db, "ui:avery", self.bill_payload())
        self.db.execute("DROP TABLE audit_log")
        self.db.commit()
        with self.assertRaises(sqlite3.OperationalError):
            actions.update_bill(self.db, "ui:avery", bill["id"], {"amount": 10})
        self.assertFalse(self.db.in_transaction)
        row = self.db.execute(
            "SELECT amount_cents FROM bills WHERE id = ?", (bill["id"],)).fetchone()
        self.assertEqual(6000, row["amount_cents"])

    # ---------------------------------------------------------------- delete

    def test_delete_missing_bill_raises_not_found(self):
        with self.assertRaises(actions.NotFound):
            actions.delete_bill(self.db, "ui:avery", 999999)

    def test_delete_is_soft_and_second_delete_is_not_found(self):
        bill = actions.create_bill(self.db, "ui:avery", self.bill_payload())
        actions.delete_bill(self.db, "ui:avery", bill["id"])
        row = self.db.execute(
            "SELECT active FROM bills WHERE id = ?", (bill["id"],)).fetchone()
        self.assertEqual(0, row["active"])
        with self.assertRaises(actions.NotFound):
            actions.delete_bill(self.db, "ui:avery", bill["id"])

    def test_delete_audits_bill_state_before_deactivation(self):
        bill = actions.create_bill(self.db, "ui:avery", self.bill_payload())
        actions.delete_bill(self.db, "ui:avery", bill["id"])
        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'delete_bill'").fetchone()
        detail = json.loads(audit["detail_json"])
        self.assertEqual(1, detail["bill"]["active"])
        self.assertEqual(6000, detail["bill"]["amount_cents"])

    def test_delete_leaves_past_payments_untouched(self):
        bill_id = self.db.execute(
            "SELECT id FROM bills WHERE active = 1 ORDER BY id LIMIT 1").fetchone()["id"]
        payments_before = self.count(
            "SELECT COUNT(*) FROM bill_payments WHERE bill_id = ?", bill_id)
        actions.delete_bill(self.db, "ui:avery", bill_id)
        payments_after = self.count(
            "SELECT COUNT(*) FROM bill_payments WHERE bill_id = ?", bill_id)
        self.assertEqual(payments_before, payments_after)

    def test_delete_is_atomic_audit_or_nothing(self):
        bill = actions.create_bill(self.db, "ui:avery", self.bill_payload())
        self.db.execute("DROP TABLE audit_log")
        self.db.commit()
        with self.assertRaises(sqlite3.OperationalError):
            actions.delete_bill(self.db, "ui:avery", bill["id"])
        self.assertFalse(self.db.in_transaction)
        row = self.db.execute(
            "SELECT active FROM bills WHERE id = ?", (bill["id"],)).fetchone()
        self.assertEqual(1, row["active"])


if __name__ == "__main__":
    unittest.main()
