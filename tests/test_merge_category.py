"""merge_category (the safe form of 'delete a category'): categories are
emergent transaction tags, so deleting one means relabelling every reference —
transactions across ALL months, bills, pantry items, and the category's budget
— into a caller-named destination, atomically. These tests prove the move is
complete (no orphans — the glitch this verb exists to prevent), that the
guards hold (Settlement, self-merge, empty names, unknown category), and that
no money number changes (a relabel is not a transaction edit)."""
import json
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


class MergeCategoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-mergecat-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=51, months=2)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def spend_on(self, date, category, cents=1200):
        self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction, income_type) "
            "VALUES (?, ?, 'MERGE TEST ROW', ?, 1, 0, 'simplefin', 'out', NULL)",
            (date, cents, category))
        self.db.commit()

    def merge(self, src, dst):
        return actions.merge_category(
            self.db, "ui:avery", {"from_category": src, "into_category": dst})

    def count(self, table, category):
        return self.db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE category = ?",
            (category,)).fetchone()[0]

    # ------------------------------------------------ the complete move
    def test_merge_moves_every_reference_and_audits_counts(self):
        self.spend_on("2026-06-03", "Doomed")          # a different month...
        self.spend_on("2026-07-11", "Doomed")          # ...than this one
        actions.create_bill(self.db, "ui:avery",
                            {"name": "Box sub", "amount": "10.00",
                             "due_day": 5, "category": "Doomed"})
        actions.add_item(self.db, "ui:avery",
                         {"name": "Tape", "category": "Doomed"})
        actions.set_budget(self.db, "ui:avery",
                           {"category": "Doomed", "amount": "50.00"})
        result = self.merge("Doomed", "MergedDest")
        self.assertEqual({"into": "MergedDest", "budget": "moved",
                          "transactions": 2, "bills": 1, "items": 1}, result)
        # No orphans: nothing anywhere still carries the old name.
        for table in ("transactions", "bills", "items", "budgets"):
            self.assertEqual(0, self.count(table, "Doomed"), table)
        self.assertEqual(2, self.count("transactions", "MergedDest"))
        # The budget row FOLLOWED the merge (same limit, new name, still active).
        budget = self.db.execute(
            "SELECT * FROM budgets WHERE category = 'MergedDest'").fetchone()
        self.assertEqual((5000, 1), (budget["amount_cents"], budget["active"]))
        detail = json.loads(self.db.execute(
            "SELECT detail_json FROM audit_log WHERE action = 'merge_category' "
            "AND target = 'category:Doomed'").fetchone()["detail_json"])
        self.assertEqual(result, detail)

    def test_merge_retires_the_budget_when_into_has_its_own(self):
        self.spend_on("2026-07-11", "Doomed")
        actions.set_budget(self.db, "ui:avery",
                           {"category": "Doomed", "amount": "50.00"})
        actions.set_budget(self.db, "ui:avery",
                           {"category": "MergedDest", "amount": "80.00"})
        result = self.merge("Doomed", "MergedDest")
        self.assertEqual("retired", result["budget"])
        rows = {r["category"]: r for r in self.db.execute(
            "SELECT * FROM budgets WHERE category IN ('Doomed', 'MergedDest')")}
        self.assertEqual(0, rows["Doomed"]["active"])          # reversible retire
        self.assertEqual((8000, 1), (rows["MergedDest"]["amount_cents"],
                                     rows["MergedDest"]["active"]))  # into keeps its own

    def test_rename_is_a_merge_into_a_fresh_name(self):
        self.spend_on("2026-07-11", "Doomed")
        result = self.merge("Doomed", "  Better Name  ")       # normalized
        self.assertEqual("Better Name", result["into"])
        self.assertEqual(1, self.count("transactions", "Better Name"))

    def test_items_updated_at_is_not_bumped_by_a_relabel(self):
        # restock_suggestions uses items.updated_at as its since-the-item-
        # changed bound; a relabel is not a stock event and must not shift it.
        item = actions.add_item(self.db, "ui:avery",
                                {"name": "Tape", "category": "Doomed"})
        self.spend_on("2026-07-11", "Doomed")
        self.merge("Doomed", "MergedDest")
        after = self.db.execute("SELECT updated_at, category FROM items "
                                "WHERE id = ?", (item["id"],)).fetchone()
        self.assertEqual(("MergedDest", item["updated_at"]),
                         (after["category"], after["updated_at"]))

    # ------------------------------------------------------------ guards
    def test_guards_settlement_self_empty_and_unknown(self):
        self.spend_on("2026-07-11", "Doomed")
        for src, dst, msg in (
                ("Settlement", "Other", "system-managed"),
                ("Doomed", "Settlement", "system-managed"),
                ("Doomed", "Doomed", "already this category"),
                ("Doomed", "   ", "required"),
                ("", "MergedDest", "required"),
                ("NeverExisted", "MergedDest", "nothing is tagged")):
            with self.assertRaisesRegex(actions.ActionError, msg):
                self.merge(src, dst)
        # The guard rejections changed nothing.
        self.assertEqual(1, self.count("transactions", "Doomed"))

    # ------------------------------------------- money numbers untouched
    def test_merge_changes_no_money_number(self):
        self.spend_on("2026-07-11", "Doomed")
        before_balance = derivations.compute_balance(self.db)
        before_monthly = {
            m: entry["total_cents"]
            for m, entry in derivations.spending_summary(self.db).items()}
        self.merge("Doomed", "MergedDest")
        self.assertEqual(before_balance, derivations.compute_balance(self.db))
        after_monthly = {
            m: entry["total_cents"]
            for m, entry in derivations.spending_summary(self.db).items()}
        self.assertEqual(before_monthly, after_monthly)


if __name__ == "__main__":
    unittest.main()
