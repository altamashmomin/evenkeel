"""set_budget / remove_budget verbs (Analytics Tier C, BUDGETS-DESIGN).

Category monthly limits: upsert (insert / update / reactivate), soft-delete,
audited, never touching money."""
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import actions  # noqa: E402

SEED_AS_OF = "2026-07-19"


class BudgetVerbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-budget-verbs-")
        self.db_path = Path(self.tmp.name) / "test.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "51", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _budget(self, category):
        return self.db.execute(
            "SELECT * FROM budgets WHERE category = ?", (category,)).fetchone()

    def test_set_budget_creates_and_audits(self):
        row = actions.set_budget(
            self.db, "ui:avery", {"category": "Groceries", "amount": "400"})
        self.assertEqual("Groceries", row["category"])
        self.assertEqual(40000, row["amount_cents"])   # integer cents
        self.assertEqual(1, row["active"])
        audit = self.db.execute(
            "SELECT actor, action FROM audit_log WHERE target = ?",
            (f"budget:{row['id']}",)).fetchone()
        self.assertEqual(("ui:avery", "set_budget"), (audit["actor"], audit["action"]))

    def test_set_budget_upserts_the_same_category(self):
        a = actions.set_budget(self.db, "ui:avery", {"category": "Dining", "amount": "200"})
        b = actions.set_budget(self.db, "ui:avery", {"category": "Dining", "amount": "250"})
        self.assertEqual(a["id"], b["id"])            # same row, not a second
        self.assertEqual(25000, b["amount_cents"])    # amount replaced
        n = self.db.execute(
            "SELECT COUNT(*) FROM budgets WHERE category = 'Dining'").fetchone()[0]
        self.assertEqual(1, n)

    def test_set_budget_validates(self):
        with self.assertRaisesRegex(actions.ActionError, "category is required"):
            actions.set_budget(self.db, "ui:avery", {"amount": "100"})
        with self.assertRaisesRegex(actions.ActionError, "amount must be positive"):
            actions.set_budget(self.db, "ui:avery", {"category": "Gas", "amount": "0"})
        with self.assertRaisesRegex(actions.ActionError, "invalid amount"):
            actions.set_budget(self.db, "ui:avery", {"category": "Gas", "amount": "abc"})

    def test_remove_budget_soft_deletes_then_set_reactivates(self):
        row = actions.set_budget(self.db, "ui:avery", {"category": "Pets", "amount": "50"})
        actions.remove_budget(self.db, "ui:avery", row["id"])
        self.assertEqual(0, self._budget("Pets")["active"])   # soft-deleted, row stays
        # re-setting the category reactivates the SAME row (upsert on conflict)
        again = actions.set_budget(self.db, "ui:avery", {"category": "Pets", "amount": "60"})
        self.assertEqual(row["id"], again["id"])
        self.assertEqual(1, again["active"])
        self.assertEqual(6000, again["amount_cents"])
        rm = self.db.execute(
            "SELECT action FROM audit_log WHERE target = ? ORDER BY id",
            (f"budget:{row['id']}",)).fetchall()
        self.assertEqual(["set_budget", "remove_budget", "set_budget"],
                         [r["action"] for r in rm])

    def test_remove_budget_missing_or_already_removed_is_not_found(self):
        with self.assertRaises(actions.NotFound):
            actions.remove_budget(self.db, "ui:avery", 999)
        row = actions.set_budget(self.db, "ui:avery", {"category": "Health", "amount": "30"})
        actions.remove_budget(self.db, "ui:avery", row["id"])
        with self.assertRaises(actions.NotFound):        # already inactive
            actions.remove_budget(self.db, "ui:avery", row["id"])


if __name__ == "__main__":
    unittest.main()
