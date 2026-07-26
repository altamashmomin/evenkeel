"""Goal verbs: frozen validation, signed-event rows with named intent,
destructive before-image audit, and the transaction boundary."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
sys.path.insert(0, str(REPO))

import actions


class GoalVerbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-goal-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "23", "--months", "2", "--as-of", SEED_AS_OF],
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

    def audit_detail(self, action):
        row = self.db.execute(
            "SELECT * FROM audit_log WHERE action = ? ORDER BY id DESC", (action,)
        ).fetchone()
        return row, json.loads(row["detail_json"]) if row else None

    def test_create_goal_writes_row_and_audit(self):
        goal = actions.create_goal(self.db, "ui:avery", {
            "name": "New brakes", "target": 400.00, "target_date": "2026-12-01"})
        self.assertEqual("New brakes", goal["name"])
        self.assertEqual(40000, goal["target_cents"])
        self.assertEqual("2026-12-01", goal["target_date"])
        audit, detail = self.audit_detail("create_goal")
        self.assertEqual(f"goal:{goal['id']}", audit["target"])
        self.assertEqual(40000, detail["goal"]["target_cents"])

    def test_create_goal_frozen_messages(self):
        cases = [
            ({"name": " ", "target": 10}, "name is required"),
            ({"name": "x", "target": "nope"}, "invalid target amount"),
            ({"name": "x", "target": 0}, "target must be positive"),
            ({"name": "x", "target": 10, "target_date": "12/01/2026"},
             "invalid target date"),
        ]
        for payload, message in cases:
            with self.assertRaisesRegex(ValueError, message):
                actions.create_goal(self.db, "ui:avery", payload)
        self.assertFalse(self.db.in_transaction)

    def test_delete_goal_audits_summary_before_cascade(self):
        goal_id = self.db.execute("SELECT id FROM goals ORDER BY id").fetchone()["id"]
        contributions = self.count(
            "SELECT COUNT(*) FROM goal_contributions WHERE goal_id = ?", goal_id)
        saved = self.count(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM goal_contributions "
            "WHERE goal_id = ?", goal_id)
        self.assertGreater(contributions, 0)

        actions.delete_goal(self.db, "ui:blake", goal_id)
        self.assertEqual(0, self.count(
            "SELECT COUNT(*) FROM goals WHERE id = ?", goal_id))
        self.assertEqual(0, self.count(
            "SELECT COUNT(*) FROM goal_contributions WHERE goal_id = ?", goal_id))
        audit, detail = self.audit_detail("delete_goal")
        self.assertEqual("ui:blake", audit["actor"])
        self.assertEqual(goal_id, detail["goal"]["id"])
        self.assertEqual(contributions, detail["contribution_count"])
        self.assertEqual(saved, detail["saved_cents"])
        with self.assertRaisesRegex(actions.NotFound, "not found"):
            actions.delete_goal(self.db, "ui:blake", goal_id)

    def test_contribute_and_withdraw_store_sign_and_intent(self):
        goal_id = self.db.execute("SELECT id FROM goals ORDER BY id").fetchone()["id"]
        saved_before = self.count(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM goal_contributions "
            "WHERE goal_id = ?", goal_id)

        actions.contribute_to_goal(self.db, "ui:avery", goal_id, 1, 2500, "bonus")
        actions.withdraw_from_goal(self.db, "ui:blake", goal_id, 2, 1000, None)

        rows = self.db.execute(
            "SELECT user_id, amount_cents, note FROM goal_contributions "
            "WHERE goal_id = ? ORDER BY id DESC LIMIT 2", (goal_id,)).fetchall()
        self.assertEqual([(2, -1000, None), (1, 2500, "bonus")],
                         [(r["user_id"], r["amount_cents"], r["note"]) for r in rows])
        self.assertEqual(saved_before + 1500, self.count(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM goal_contributions "
            "WHERE goal_id = ?", goal_id))

        _, contribute_detail = self.audit_detail("contribute_to_goal")
        self.assertEqual(2500, contribute_detail["amount_cents"])
        _, withdraw_detail = self.audit_detail("withdraw_from_goal")
        self.assertEqual(1000, withdraw_detail["amount_cents"])

    def test_goal_flow_validation(self):
        goal_id = self.db.execute("SELECT id FROM goals ORDER BY id").fetchone()["id"]
        with self.assertRaisesRegex(actions.NotFound, "not found"):
            actions.contribute_to_goal(self.db, "ui:avery", 9999, 1, 100)
        with self.assertRaisesRegex(ValueError, "member must be an active member"):
            actions.contribute_to_goal(self.db, "ui:avery", goal_id, 99, 100)
        with self.assertRaisesRegex(ValueError, "amount must be positive"):
            actions.withdraw_from_goal(self.db, "ui:avery", goal_id, 1, 0)
        self.assertFalse(self.db.in_transaction)

    def test_verbs_are_atomic_audit_or_nothing(self):
        goal_id = self.db.execute("SELECT id FROM goals ORDER BY id").fetchone()["id"]
        goals = self.count("SELECT COUNT(*) FROM goals")
        contributions = self.count("SELECT COUNT(*) FROM goal_contributions")
        self.db.execute("DROP TABLE audit_log")
        self.db.commit()

        with self.assertRaises(sqlite3.OperationalError):
            actions.create_goal(self.db, "ui:avery", {"name": "x", "target": 10})
        with self.assertRaises(sqlite3.OperationalError):
            actions.delete_goal(self.db, "ui:avery", goal_id)
        with self.assertRaises(sqlite3.OperationalError):
            actions.contribute_to_goal(self.db, "ui:avery", goal_id, 1, 100)

        self.assertFalse(self.db.in_transaction)
        self.assertEqual(goals, self.count("SELECT COUNT(*) FROM goals"))
        self.assertEqual(contributions, self.count(
            "SELECT COUNT(*) FROM goal_contributions"))


if __name__ == "__main__":
    unittest.main()
