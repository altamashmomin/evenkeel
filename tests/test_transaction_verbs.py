"""edit_transaction / delete_transaction verbs: the settlement-history edit
policy (settlement rows frozen, editing or deleting a covered row reopens
it), atomic edit-or-nothing, and frozen NotFound/validation messages."""
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
import derivations


class TransactionVerbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-txnverb-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=17, months=3)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def count(self, sql, *params):
        return self.db.execute(sql, params).fetchone()[0]

    def settle(self, actor="ui:avery"):
        balance = derivations.compute_balance(self.db)
        self.assertEqual("owing", balance["state"])
        return actions.settle_up(self.db, actor, {
            "date": date.today().isoformat(),
            "amount": balance["amount_cents"] / 100,
            "description": "Settlement — test",
            "category": "Settlement",
            "paid_by": balance["ower"]["id"],
            "is_shared": True,
            "payer_share_pct": 0,
            "source": "settlement",
        })

    def a_manual_row(self, **overrides):
        cur = self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source)
               VALUES (?, 5000, 'Groceries run', 'Groceries', 1, 1, 'manual')""",
            (date.today().isoformat(),))
        txn_id = cur.lastrowid
        self.db.executemany(
            "INSERT INTO splits (transaction_id, member_id, share_bp) VALUES (?, ?, ?)",
            [(txn_id, 1, 5000), (txn_id, 2, 5000)])
        self.db.commit()
        return txn_id

    # ---------------------------------------------------------- edit_transaction

    def test_settlement_rows_cannot_be_edited(self):
        settled = self.settle()
        with self.assertRaisesRegex(
                actions.ActionError,
                "settlement rows cannot be edited; delete and recreate"):
            actions.edit_transaction(
                self.db, "ui:avery", settled["id"], {"amount": 5})
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(
            0, self.count("SELECT COUNT(*) FROM audit_log WHERE action = 'edit_transaction'"))

    def test_missing_transaction_raises_not_found(self):
        with self.assertRaises(actions.NotFound):
            actions.edit_transaction(self.db, "ui:avery", 999999, {"amount": 5})

    def test_empty_payload_rejected(self):
        txn_id = self.a_manual_row()
        with self.assertRaisesRegex(actions.ActionError, "nothing to update"):
            actions.edit_transaction(self.db, "ui:avery", txn_id, {})

    def test_edit_changes_fields_and_writes_audit(self):
        txn_id = self.a_manual_row()
        row = actions.edit_transaction(
            self.db, "ui:avery", txn_id, {"amount": 12.34, "description": "Corrected"})
        self.assertEqual(1234, row["amount_cents"])
        self.assertEqual("Corrected", row["description"])
        # Untouched fields, including category, survive.
        self.assertEqual("Groceries", row["category"])

        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'edit_transaction'").fetchone()
        self.assertEqual("ui:avery", audit["actor"])
        self.assertEqual(f"transaction:{txn_id}", audit["target"])
        detail = json.loads(audit["detail_json"])
        self.assertEqual(5000, detail["before"]["amount_cents"])
        self.assertEqual(1234, detail["changed"]["amount_cents"])
        self.assertEqual([], detail["severed_settles_links"])

    def test_recategorize_leaves_balance_and_month_total_unchanged(self):
        # The Home "Spent" recategorize flow moves transactions between
        # categories via a category-only edit_transaction. Relabeling must not
        # move the who-owes-whom balance or the month's spend total — only the
        # per-category distribution shifts. This is the safety claim the
        # frontend makes ("amounts, splits, and who owes whom don't change").
        txn_id = self.a_manual_row()   # shared 50/50 Groceries row
        month = date.today().strftime("%Y-%m")
        before_balance = derivations.compute_balance(self.db)["amount_cents"]
        before_total = derivations.spending_summary(
            self.db, month)[month]["total_cents"]

        row = actions.edit_transaction(
            self.db, "ui:avery", txn_id, {"category": "Dining"})
        self.assertEqual("Dining", row["category"])

        after_balance = derivations.compute_balance(self.db)["amount_cents"]
        after = derivations.spending_summary(self.db, month)[month]
        self.assertEqual(before_balance, after_balance, "balance moved")
        self.assertEqual(before_total, after["total_cents"], "month total moved")
        # The spend genuinely moved categories: now under Dining, not Groceries.
        by_cat = {c["category"]: c["amount_cents"] for c in after["by_category"]}
        self.assertEqual(5000, by_cat.get("Dining"))
        self.assertNotIn("Groceries", by_cat)

    def test_recategorize_transaction_is_a_category_only_facade(self):
        # B1: the agent write tier's recategorize verb delegates to
        # edit_transaction with a category-only payload — same audit action,
        # amount/split untouched, settlement freeze inherited.
        txn_id = self.a_manual_row()   # 5000c shared Groceries
        row = actions.recategorize_transaction(self.db, "ui:blake", txn_id, "Dining")
        self.assertEqual("Dining", row["category"])
        self.assertEqual(5000, row["amount_cents"])       # amount untouched
        audit = self.db.execute(
            "SELECT actor, detail_json FROM audit_log "
            "WHERE action = 'edit_transaction'").fetchone()
        self.assertEqual("ui:blake", audit["actor"])
        self.assertEqual({"category": "Dining"}, json.loads(audit["detail_json"])["changed"])
        # split preserved (still 50/50)
        shares = [r["share_bp"] for r in self.db.execute(
            "SELECT share_bp FROM splits WHERE transaction_id = ? ORDER BY member_id",
            (txn_id,))]
        self.assertEqual([5000, 5000], shares)

    def test_recategorize_transaction_refuses_a_settlement(self):
        settled = self.settle()
        with self.assertRaisesRegex(actions.ActionError, "settlement rows cannot be edited"):
            actions.recategorize_transaction(self.db, "ui:avery", settled["id"], "Dining")

    def test_edit_without_share_carries_forward_existing_payer_share(self):
        txn_id = self.a_manual_row()
        # Give the row a lopsided share first.
        actions.write_legacy_two_member_splits(self.db, txn_id, 1, True, 70)
        self.db.commit()
        actions.edit_transaction(self.db, "ui:avery", txn_id, {"description": "Renamed"})
        share = self.db.execute(
            "SELECT share_bp FROM splits WHERE transaction_id = ? AND member_id = 1",
            (txn_id,)).fetchone()["share_bp"]
        self.assertEqual(7000, share)

    def test_editing_a_covered_row_severs_its_settles_link_and_audits_it(self):
        settled = self.settle()
        covered_id = self.db.execute(
            "SELECT to_id FROM links WHERE link_type = 'settles' AND from_id = ? "
            "ORDER BY to_id LIMIT 1", (settled["id"],)).fetchone()["to_id"]

        actions.edit_transaction(
            self.db, "ui:blake", covered_id, {"description": "Reopened"})

        remaining = self.count(
            "SELECT COUNT(*) FROM links WHERE link_type = 'settles' AND to_id = ?",
            covered_id)
        self.assertEqual(0, remaining)

        detail = json.loads(self.db.execute(
            "SELECT detail_json FROM audit_log WHERE action = 'edit_transaction' "
            "AND target = ?", (f"transaction:{covered_id}",)).fetchone()[0])
        self.assertEqual(1, len(detail["severed_settles_links"]))
        self.assertEqual(settled["id"], detail["severed_settles_links"][0]["from_id"])

    def test_edit_is_atomic_audit_or_nothing(self):
        txn_id = self.a_manual_row()
        self.db.execute("DROP TABLE audit_log")
        self.db.commit()
        with self.assertRaises(sqlite3.OperationalError):
            actions.edit_transaction(self.db, "ui:avery", txn_id, {"amount": 1})
        self.assertFalse(self.db.in_transaction)
        row = self.db.execute(
            "SELECT amount_cents FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        self.assertEqual(5000, row["amount_cents"])

    # -------------------------------------------------------- delete_transaction

    def test_missing_delete_raises_not_found(self):
        with self.assertRaises(actions.NotFound):
            actions.delete_transaction(self.db, "ui:avery", 999999)

    def test_delete_removes_row_splits_links_and_audits_before_image(self):
        txn_id = self.a_manual_row()
        actions.delete_transaction(self.db, "ui:avery", txn_id)
        self.assertEqual(
            0, self.count("SELECT COUNT(*) FROM transactions WHERE id = ?", txn_id))
        self.assertEqual(
            0, self.count("SELECT COUNT(*) FROM splits WHERE transaction_id = ?", txn_id))

        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'delete_transaction'").fetchone()
        self.assertEqual(f"transaction:{txn_id}", audit["target"])
        detail = json.loads(audit["detail_json"])
        self.assertEqual(txn_id, detail["deleted"]["transaction"]["id"])
        self.assertEqual(2, len(detail["deleted"]["splits"]))

    def test_deleting_a_settlement_reopens_what_it_covered(self):
        settled = self.settle()
        covered = [r["to_id"] for r in self.db.execute(
            "SELECT to_id FROM links WHERE link_type = 'settles' AND from_id = ?",
            (settled["id"],)).fetchall()]
        self.assertTrue(covered)

        actions.delete_transaction(self.db, "ui:avery", settled["id"])

        remaining = self.count(
            "SELECT COUNT(*) FROM links WHERE link_type = 'settles'")
        self.assertEqual(0, remaining)
        # Every previously-covered row is now settleable again.
        balance = derivations.compute_balance(self.db)
        self.assertEqual("owing", balance["state"])

    def test_delete_is_atomic_audit_or_nothing(self):
        txn_id = self.a_manual_row()
        self.db.execute("DROP TABLE audit_log")
        self.db.commit()
        with self.assertRaises(sqlite3.OperationalError):
            actions.delete_transaction(self.db, "ui:avery", txn_id)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(
            1, self.count("SELECT COUNT(*) FROM transactions WHERE id = ?", txn_id))


if __name__ == "__main__":
    unittest.main()
