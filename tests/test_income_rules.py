"""create_income_rule / set_rule_enabled / apply_rules: the matching
engine's priority-ordered first-match-wins semantics, the conflict check,
dry_run vs. real application, hit_count observability, and the "a no-op
isn't audited" discipline for a batch verb."""
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


class IncomeRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-income-rules-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=51, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def count(self, sql, *params):
        return self.db.execute(sql, params).fetchone()[0]

    def rule_payload(self, **overrides):
        body = {"match_desc": "ADP PAYROLL", "set_type": "paycheck"}
        body.update(overrides)
        return body

    def an_inflow(self, description="ADP PAYROLL 8842", amount_cents=320000,
                  external_id=None, paid_by=1):
        cur = self.db.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source, external_id, direction, income_type)
               VALUES (?, ?, ?, 'Other', ?, 0, 'simplefin', ?, 'in', 'unclassified')""",
            (date.today().isoformat(), amount_cents, description, paid_by, external_id))
        self.db.commit()
        return cur.lastrowid

    # ---------------------------------------------------------- create_income_rule

    def test_create_writes_row_and_audit(self):
        rule = actions.create_income_rule(self.db, "ui:avery", self.rule_payload())
        self.assertEqual("ADP PAYROLL", rule["match_desc"])
        self.assertEqual("paycheck", rule["set_type"])
        self.assertEqual(1, rule["enabled"])
        self.assertEqual(0, rule["hit_count"])

        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'create_income_rule'").fetchone()
        self.assertEqual("ui:avery", audit["actor"])
        self.assertEqual(f"rule:{rule['id']}", audit["target"])

    def test_set_type_rejects_unclassified_and_bogus_values(self):
        with self.assertRaisesRegex(actions.ActionError, "set_type must be one of"):
            actions.create_income_rule(
                self.db, "ui:avery", self.rule_payload(set_type="unclassified"))
        with self.assertRaisesRegex(actions.ActionError, "set_type must be one of"):
            actions.create_income_rule(
                self.db, "ui:avery", self.rule_payload(set_type="bogus"))

    def test_requires_at_least_one_match_criterion(self):
        with self.assertRaisesRegex(actions.ActionError, "at least one match criterion"):
            actions.create_income_rule(
                self.db, "ui:avery", {"set_type": "paycheck"})

    def test_min_max_cents_must_be_integers_and_ordered(self):
        with self.assertRaisesRegex(actions.ActionError, "must be an integer"):
            actions.create_income_rule(
                self.db, "ui:avery", self.rule_payload(min_cents="100"))
        with self.assertRaisesRegex(
                actions.ActionError, "min_cents must not exceed max_cents"):
            actions.create_income_rule(
                self.db, "ui:avery",
                self.rule_payload(match_desc=None, min_cents=500, max_cents=100))

    def test_set_paid_by_must_be_active_member(self):
        with self.assertRaisesRegex(actions.ActionError, "set_paid_by must be"):
            actions.create_income_rule(
                self.db, "ui:avery", self.rule_payload(set_paid_by=999))

    def test_priority_defaults_to_zero_and_parses_ints(self):
        rule = actions.create_income_rule(self.db, "ui:avery", self.rule_payload())
        self.assertEqual(0, rule["priority"])
        rule2 = actions.create_income_rule(
            self.db, "ui:avery",
            self.rule_payload(match_desc="OTHER DESC", priority=5))
        self.assertEqual(5, rule2["priority"])
        with self.assertRaisesRegex(actions.ActionError, "priority must be an integer"):
            actions.create_income_rule(
                self.db, "ui:avery",
                self.rule_payload(match_desc="YET ANOTHER", priority="nope"))

    def test_conflict_check_rejects_duplicate_enabled_criteria(self):
        actions.create_income_rule(self.db, "ui:avery", self.rule_payload())
        with self.assertRaisesRegex(
                actions.ActionError, "a rule with these match criteria already exists"):
            actions.create_income_rule(
                self.db, "ui:avery", self.rule_payload(set_type="gift"))

    def test_conflict_check_ignores_disabled_rules(self):
        rule = actions.create_income_rule(self.db, "ui:avery", self.rule_payload())
        actions.set_rule_enabled(self.db, "ui:avery", rule["id"], enabled=False)
        second = actions.create_income_rule(
            self.db, "ui:avery", self.rule_payload(set_type="gift"))
        self.assertNotEqual(rule["id"], second["id"])

    def test_create_is_atomic_audit_or_nothing(self):
        self.db.execute("DROP TABLE audit_log")
        self.db.commit()
        before = self.count("SELECT COUNT(*) FROM income_rules")
        with self.assertRaises(sqlite3.OperationalError):
            actions.create_income_rule(self.db, "ui:avery", self.rule_payload())
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(before, self.count("SELECT COUNT(*) FROM income_rules"))

    # ------------------------------------------------------------- set_rule_enabled

    def test_set_rule_enabled_missing_raises_not_found(self):
        with self.assertRaises(actions.NotFound):
            actions.set_rule_enabled(self.db, "ui:avery", 999999, enabled=False)

    def test_set_rule_enabled_toggles_and_audits_before_after(self):
        rule = actions.create_income_rule(self.db, "ui:avery", self.rule_payload())
        disabled = actions.set_rule_enabled(self.db, "ui:blake", rule["id"], enabled=False)
        self.assertEqual(0, disabled["enabled"])

        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'set_rule_enabled'").fetchone()
        self.assertEqual("ui:blake", audit["actor"])
        detail = json.loads(audit["detail_json"])
        self.assertTrue(detail["before"])
        self.assertFalse(detail["after"])

        row = self.db.execute(
            "SELECT * FROM income_rules WHERE id = ?", (rule["id"],)).fetchone()
        self.assertIsNotNone(row)  # no delete -- disabled rules keep history

    # -------------------------------------------------------------------- apply_rules

    def test_dry_run_previews_without_writing_anything(self):
        actions.create_income_rule(self.db, "ui:avery", self.rule_payload())
        txn_id = self.an_inflow()
        preview = actions.apply_rules(self.db, "ui:avery", dry_run=True)
        self.assertEqual(
            [{"transaction_id": txn_id, "rule_id": 1,
              "set_type": "paycheck", "set_paid_by": None}], preview)

        row = self.db.execute(
            "SELECT income_type FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        self.assertEqual("unclassified", row["income_type"])
        self.assertEqual(
            0, self.count("SELECT COUNT(*) FROM audit_log WHERE action = 'apply_rules'"))
        self.assertEqual(
            0, self.count("SELECT hit_count FROM income_rules WHERE id = 1"))

    def test_apply_classifies_and_increments_hit_count_and_audits(self):
        actions.create_income_rule(self.db, "ui:avery", self.rule_payload())
        txn_id = self.an_inflow()
        applied = actions.apply_rules(self.db, "ui:avery", dry_run=False)
        self.assertEqual(1, len(applied))

        row = self.db.execute(
            "SELECT income_type FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        self.assertEqual("paycheck", row["income_type"])
        hit_count = self.db.execute(
            "SELECT hit_count FROM income_rules WHERE id = 1").fetchone()["hit_count"]
        self.assertEqual(1, hit_count)

        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'apply_rules'").fetchone()
        self.assertIsNone(audit["target"])
        detail = json.loads(audit["detail_json"])
        self.assertEqual(1, len(detail["applied"]))

    def test_no_match_leaves_row_unclassified_and_skips_audit(self):
        actions.create_income_rule(self.db, "ui:avery", self.rule_payload())
        txn_id = self.an_inflow(description="Completely unrelated deposit")
        applied = actions.apply_rules(self.db, "ui:avery", dry_run=False)
        self.assertEqual([], applied)
        row = self.db.execute(
            "SELECT income_type FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        self.assertEqual("unclassified", row["income_type"])
        self.assertEqual(
            0, self.count("SELECT COUNT(*) FROM audit_log WHERE action = 'apply_rules'"))

    def test_first_match_wins_by_priority(self):
        # Lower priority runs first: the specific rule (priority 0) should
        # win over the generic one (priority 10) for an overlapping row.
        actions.create_income_rule(
            self.db, "ui:avery",
            self.rule_payload(match_desc="ADP", set_type="paycheck", priority=0))
        actions.create_income_rule(
            self.db, "ui:avery",
            {"min_cents": 0, "set_type": "other", "priority": 10})
        txn_id = self.an_inflow(description="ADP PAYROLL 8842")
        actions.apply_rules(self.db, "ui:avery", dry_run=False)
        row = self.db.execute(
            "SELECT income_type FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        self.assertEqual("paycheck", row["income_type"])

    def test_match_account_parsed_from_external_id(self):
        actions.create_income_rule(
            self.db, "ui:avery",
            {"match_account": "acct-savings", "set_type": "transfer"})
        wrong_account = self.an_inflow(
            description="Transfer", external_id="simplefin:acct-checking:tx1")
        right_account = self.an_inflow(
            description="Transfer", external_id="simplefin:acct-savings:tx2")
        actions.apply_rules(self.db, "ui:avery", dry_run=False)
        rows = {r["id"]: r["income_type"] for r in self.db.execute(
            "SELECT id, income_type FROM transactions "
            "WHERE id IN (?, ?)", (wrong_account, right_account))}
        self.assertEqual("unclassified", rows[wrong_account])
        self.assertEqual("transfer", rows[right_account])

    def test_amount_bounds_match(self):
        actions.create_income_rule(
            self.db, "ui:avery",
            {"min_cents": 300000, "max_cents": 350000, "set_type": "paycheck"})
        too_small = self.an_inflow(description="x", amount_cents=100000)
        in_range = self.an_inflow(description="y", amount_cents=320000)
        actions.apply_rules(self.db, "ui:avery", dry_run=False)
        rows = {r["id"]: r["income_type"] for r in self.db.execute(
            "SELECT id, income_type FROM transactions WHERE id IN (?, ?)",
            (too_small, in_range))}
        self.assertEqual("unclassified", rows[too_small])
        self.assertEqual("paycheck", rows[in_range])

    def test_set_paid_by_override_applied_on_match(self):
        actions.create_income_rule(
            self.db, "ui:avery",
            self.rule_payload(set_paid_by=2))
        txn_id = self.an_inflow(paid_by=1)
        actions.apply_rules(self.db, "ui:avery", dry_run=False)
        row = self.db.execute(
            "SELECT paid_by, income_type FROM transactions WHERE id = ?",
            (txn_id,)).fetchone()
        self.assertEqual(2, row["paid_by"])
        self.assertEqual("paycheck", row["income_type"])

    def test_disabled_rule_never_matches(self):
        rule = actions.create_income_rule(self.db, "ui:avery", self.rule_payload())
        actions.set_rule_enabled(self.db, "ui:avery", rule["id"], enabled=False)
        txn_id = self.an_inflow()
        applied = actions.apply_rules(self.db, "ui:avery", dry_run=False)
        self.assertEqual([], applied)
        row = self.db.execute(
            "SELECT income_type FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        self.assertEqual("unclassified", row["income_type"])

    def test_apply_is_atomic_audit_or_nothing(self):
        actions.create_income_rule(self.db, "ui:avery", self.rule_payload())
        txn_id = self.an_inflow()
        self.db.execute("DROP TABLE audit_log")
        self.db.commit()
        with self.assertRaises(sqlite3.OperationalError):
            actions.apply_rules(self.db, "ui:avery", dry_run=False)
        self.assertFalse(self.db.in_transaction)
        row = self.db.execute(
            "SELECT income_type FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        self.assertEqual("unclassified", row["income_type"])
        hit_count = self.db.execute(
            "SELECT hit_count FROM income_rules WHERE id = 1").fetchone()["hit_count"]
        self.assertEqual(0, hit_count)


if __name__ == "__main__":
    unittest.main()
