"""record_transaction verb: the UI manual-entry insert and sync's insert,
unified — dedupe on external_id lives inside the verb, source is a verb
decision (never taken from caller data), and every insert is audited."""
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


class RecordTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-record-txn-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "31", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def count(self, sql, *params):
        return self.db.execute(sql, params).fetchone()[0]

    def manual_payload(self, **overrides):
        body = {
            "date": date.today().isoformat(), "amount": 25.5,
            "description": "Groceries run", "category": "Groceries",
            "paid_by": 1, "is_shared": True, "payer_share_pct": 50,
        }
        body.update(overrides)
        return body

    def test_manual_insert_writes_row_splits_and_audit(self):
        row = actions.record_transaction(
            self.db, "ui:avery", self.manual_payload(), source="manual")
        self.assertEqual(2550, row["amount_cents"])
        self.assertEqual("manual", row["source"])
        self.assertIsNone(row["external_id"])

        splits = self.db.execute(
            "SELECT member_id, share_bp FROM splits WHERE transaction_id = ? "
            "ORDER BY member_id", (row["id"],)).fetchall()
        self.assertEqual([5000, 5000], [s["share_bp"] for s in splits])

        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'record_transaction'").fetchone()
        self.assertEqual("ui:avery", audit["actor"])
        self.assertEqual(f"transaction:{row['id']}", audit["target"])
        detail = json.loads(audit["detail_json"])
        self.assertEqual("manual", detail["source"])
        self.assertIsNone(detail["external_id"])
        self.assertEqual(2550, detail["amount_cents"])

    def test_unshared_insert_writes_no_split_rows(self):
        row = actions.record_transaction(
            self.db, "ui:avery",
            self.manual_payload(is_shared=False), source="manual")
        self.assertEqual(
            0, self.count(
                "SELECT COUNT(*) FROM splits WHERE transaction_id = ?", row["id"]))

    def test_source_is_a_verb_decision_not_client_data(self):
        payload = self.manual_payload()
        payload["source"] = "settlement"  # a client trying to sneak past settle_up
        row = actions.record_transaction(
            self.db, "ui:avery", payload, source="manual")
        self.assertEqual("manual", row["source"])

    def test_sync_style_insert_dedupes_on_external_id(self):
        payload = self.manual_payload(paid_by=1)
        first = actions.record_transaction(
            self.db, "sync", payload, source="simplefin",
            external_id="simplefin:acct1:txn1")
        self.assertIsNotNone(first)
        again = actions.record_transaction(
            self.db, "sync", payload, source="simplefin",
            external_id="simplefin:acct1:txn1")
        self.assertIsNone(again)

        self.assertEqual(
            1, self.count(
                "SELECT COUNT(*) FROM transactions WHERE external_id = ?",
                "simplefin:acct1:txn1"))
        self.assertEqual(
            1, self.count(
                "SELECT COUNT(*) FROM audit_log WHERE action = 'record_transaction'"))

    def test_dedupe_no_op_does_not_open_a_stray_transaction(self):
        payload = self.manual_payload()
        actions.record_transaction(
            self.db, "sync", payload, source="simplefin", external_id="dupe-me")
        actions.record_transaction(
            self.db, "sync", payload, source="simplefin", external_id="dupe-me")
        self.assertFalse(self.db.in_transaction)

    def test_frozen_validation_messages(self):
        with self.assertRaisesRegex(ValueError, "amount must be positive"):
            actions.record_transaction(
                self.db, "ui:avery", self.manual_payload(amount=0), source="manual")
        with self.assertRaisesRegex(ValueError, "description is required"):
            actions.record_transaction(
                self.db, "ui:avery", self.manual_payload(description="  "),
                source="manual")
        with self.assertRaisesRegex(ValueError, "paid_by must be one of the two users"):
            actions.record_transaction(
                self.db, "ui:avery", self.manual_payload(paid_by=999), source="manual")

    def test_shared_insert_rejects_three_active_members(self):
        self.db.execute(
            "INSERT INTO members (username, display_name, password_hash, active, created_at) "
            "VALUES ('casey', 'Casey', 'disabled', 1, '2026-07-19T00:00:00+00:00')")
        self.db.commit()
        before = self.count("SELECT COUNT(*) FROM transactions")
        with self.assertRaisesRegex(
                actions.ActionError,
                "shared transactions require exactly two active members"):
            actions.record_transaction(
                self.db, "ui:avery", self.manual_payload(), source="manual")
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(before, self.count("SELECT COUNT(*) FROM transactions"))

    def test_edit_is_atomic_audit_or_nothing(self):
        self.db.execute("DROP TABLE audit_log")
        self.db.commit()
        before = self.count("SELECT COUNT(*) FROM transactions")
        with self.assertRaises(sqlite3.OperationalError):
            actions.record_transaction(
                self.db, "ui:avery", self.manual_payload(), source="manual")
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(before, self.count("SELECT COUNT(*) FROM transactions"))

    # ------------------------------------------------------- direction='in'

    def test_direction_defaults_to_out(self):
        row = actions.record_transaction(
            self.db, "ui:avery", self.manual_payload(), source="manual")
        self.assertEqual("out", row["direction"])
        self.assertIsNone(row["income_type"])

    def test_invalid_direction_rejected(self):
        with self.assertRaisesRegex(
                actions.ActionError, "direction must be 'in' or 'out'"):
            actions.record_transaction(
                self.db, "ui:avery",
                self.manual_payload(direction="sideways"), source="manual")

    def test_inflow_gets_no_splits_even_when_is_shared_is_true(self):
        row = actions.record_transaction(
            self.db, "sync",
            self.manual_payload(direction="in", is_shared=True),
            source="simplefin", external_id="in-1")
        self.assertEqual("in", row["direction"])
        self.assertEqual(0, row["is_shared"])
        self.assertEqual(
            0, self.count(
                "SELECT COUNT(*) FROM splits WHERE transaction_id = ?", row["id"]))

    def test_inflow_with_no_matching_rule_lands_unclassified(self):
        row = actions.record_transaction(
            self.db, "sync", self.manual_payload(direction="in"),
            source="simplefin", external_id="in-2")
        self.assertEqual("unclassified", row["income_type"])

        audit = self.db.execute(
            "SELECT detail_json FROM audit_log WHERE action = 'record_transaction' "
            "AND target = ?", (f"transaction:{row['id']}",)).fetchone()
        detail = json.loads(audit["detail_json"])
        self.assertEqual("in", detail["direction"])
        self.assertEqual("unclassified", detail["income_type"])
        self.assertIsNone(detail["matched_rule_id"])

    def test_inflow_auto_classifies_and_bumps_hit_count_when_a_rule_matches(self):
        rule = actions.create_income_rule(
            self.db, "ui:avery",
            {"match_desc": "Groceries run", "set_type": "paycheck"})
        row = actions.record_transaction(
            self.db, "sync", self.manual_payload(direction="in"),
            source="simplefin", external_id="in-3")
        self.assertEqual("paycheck", row["income_type"])

        hit_count = self.db.execute(
            "SELECT hit_count FROM income_rules WHERE id = ?",
            (rule["id"],)).fetchone()["hit_count"]
        self.assertEqual(1, hit_count)

        detail = json.loads(self.db.execute(
            "SELECT detail_json FROM audit_log WHERE action = 'record_transaction' "
            "AND target = ?", (f"transaction:{row['id']}",)).fetchone()[0])
        self.assertEqual(rule["id"], detail["matched_rule_id"])

    def test_inflow_rule_can_override_the_owner(self):
        actions.create_income_rule(
            self.db, "ui:avery",
            {"match_desc": "Groceries run", "set_type": "gift", "set_paid_by": 2})
        row = actions.record_transaction(
            self.db, "sync", self.manual_payload(direction="in", paid_by=1),
            source="simplefin", external_id="in-4")
        self.assertEqual(2, row["paid_by"])
        self.assertEqual("gift", row["income_type"])

    def test_inflow_is_atomic_audit_or_nothing(self):
        self.db.execute("DROP TABLE audit_log")
        self.db.commit()
        with self.assertRaises(sqlite3.OperationalError):
            actions.record_transaction(
                self.db, "sync", self.manual_payload(direction="in"),
                source="simplefin", external_id="in-5")
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(
            0, self.count(
                "SELECT COUNT(*) FROM transactions WHERE external_id = ?", "in-5"))


if __name__ == "__main__":
    unittest.main()
