"""income_rules.set_transfer (transfer-neutral fix, increment T3a): a rule that
marks its matches as transfers. The engine plumbing — record_transaction (sync)
and apply_rules (retroactive) — must set is_transfer=1 when a matched rule
carries set_transfer, and leave it 0 for ordinary rules. The verb that CREATES
such a rule + the nudge UI are T3b; this inserts the rule directly to prove the
engine honors the column."""
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


class RuleTransferEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-rule-transfer-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=61, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.execute("DELETE FROM income_rules")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def a_rule(self, match_desc, set_type="transfer", set_transfer=1):
        cur = self.db.execute(
            "INSERT INTO income_rules (priority, match_desc, set_type, set_transfer, "
            "created_at) VALUES (0, ?, ?, ?, '2026-07-01')",
            (match_desc, set_type, set_transfer))
        self.db.commit()
        return cur.lastrowid

    def test_column_exists_default_zero(self):
        cols = {r[1]: r for r in self.db.execute("PRAGMA table_info(income_rules)")}
        self.assertIn("set_transfer", cols)
        self.assertEqual(1, cols["set_transfer"][3], "NOT NULL")
        self.assertEqual("0", str(cols["set_transfer"][4]), "default 0")

    def test_sync_flags_a_matching_inflow_as_transfer(self):
        self.a_rule("payment thank you")
        row = actions.record_transaction(
            self.db, actor="sync",
            data={"date": "2026-07-18", "amount": "270.36",
                  "description": "PAYMENT THANK YOU - WEB", "paid_by": 1,
                  "is_shared": False},
            source="simplefin", external_id="simplefin:cc:1", direction="in")
        self.assertEqual(1, row["is_transfer"], "transfer rule did not flag the row")
        # And it's out of income.
        self.assertEqual(0, derivations.income_summary(
            self.db, "2026-07")["gross_inflows_cents"])

    def test_ordinary_rule_does_not_flag(self):
        self.a_rule("adp payroll", set_type="paycheck", set_transfer=0)
        row = actions.record_transaction(
            self.db, actor="sync",
            data={"date": "2026-07-01", "amount": "3000.00",
                  "description": "ADP PAYROLL", "paid_by": 1, "is_shared": False},
            source="simplefin", external_id="simplefin:chk:1", direction="in")
        self.assertEqual(0, row["is_transfer"])
        self.assertEqual("paycheck", row["income_type"])

    def test_apply_rules_flags_the_unclassified_backlog(self):
        # An existing unclassified "Payment Thank You" (pre-rule), then the rule
        # is added and swept retroactively.
        self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction, income_type) "
            "VALUES ('2026-07-18', 27036, 'Payment Thank You - Web', 'Other', 1, 0, "
            "'simplefin', 'in', 'unclassified')")
        self.db.commit()
        self.a_rule("payment thank you")
        applied = actions.apply_rules(self.db, "ui:avery")
        self.assertEqual(1, len(applied))
        row = self.db.execute(
            "SELECT is_transfer FROM transactions WHERE description LIKE 'Payment%'"
        ).fetchone()
        self.assertEqual(1, row["is_transfer"], "backlog row not flagged")

    def test_apply_rules_dry_run_previews_transfer_without_writing(self):
        self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction, income_type) "
            "VALUES ('2026-07-18', 27036, 'Payment Thank You', 'Other', 1, 0, "
            "'simplefin', 'in', 'unclassified')")
        self.db.commit()
        self.a_rule("payment thank you")
        preview = actions.apply_rules(self.db, "ui:avery", dry_run=True)
        self.assertEqual(1, preview[0]["set_transfer"])
        # Nothing written under dry_run.
        row = self.db.execute(
            "SELECT is_transfer, income_type FROM transactions").fetchone()
        self.assertEqual(0, row["is_transfer"])
        self.assertEqual("unclassified", row["income_type"])


class CreateTransferRuleTests(unittest.TestCase):
    """T3b: create_income_rule accepts set_transfer + the make-a-rule nudge."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-create-xfer-rule-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=67, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.execute("DELETE FROM income_rules")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _inflow(self, desc, is_transfer=0, income_type="unclassified"):
        cur = self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction, income_type, is_transfer) "
            "VALUES ('2026-07-18', 27036, ?, 'Other', 1, 0, 'simplefin', 'in', ?, ?)",
            (desc, income_type, is_transfer))
        self.db.commit()
        return self.db.execute(
            "SELECT * FROM transactions WHERE id = ?", (cur.lastrowid,)).fetchone()

    def test_create_rule_with_only_match_and_set_transfer_defaults_type(self):
        rule = actions.create_income_rule(
            self.db, "ui:avery", {"match_desc": "Payment Thank You", "set_transfer": True})
        self.assertEqual(1, rule["set_transfer"])
        self.assertEqual("transfer", rule["set_type"], "set_type defaulted to transfer")

    def test_created_transfer_rule_flags_future_sync(self):
        actions.create_income_rule(
            self.db, "ui:avery", {"match_desc": "Payment Thank You", "set_transfer": True})
        row = actions.record_transaction(
            self.db, actor="sync",
            data={"date": "2026-08-18", "amount": "270.36",
                  "description": "PAYMENT THANK YOU - WEB", "paid_by": 1,
                  "is_shared": False},
            source="simplefin", external_id="simplefin:cc:aug", direction="in")
        self.assertEqual(1, row["is_transfer"])

    def test_ordinary_rule_still_has_set_transfer_zero(self):
        rule = actions.create_income_rule(
            self.db, "ui:avery", {"match_desc": "ADP", "set_type": "paycheck"})
        self.assertEqual(0, rule["set_transfer"])

    def test_suggest_offers_at_the_second_transfer_only(self):
        first = self._inflow("Payment Thank You - Web", is_transfer=1)
        # count == 1 → no offer yet
        self.assertIsNone(actions.suggest_transfer_rule_after_mark(self.db, first))
        second = self._inflow("Payment Thank You", is_transfer=1)
        s = actions.suggest_transfer_rule_after_mark(self.db, second)
        self.assertIsNotNone(s)
        self.assertTrue(s["set_transfer"])
        self.assertEqual("Payment Thank You", s["match_desc"])
        # count == 3 → no repeat nag
        third = self._inflow("Payment Thank You 3", is_transfer=1)
        self.assertIsNone(actions.suggest_transfer_rule_after_mark(self.db, third))

    def test_suggest_suppressed_when_a_rule_already_matches(self):
        self._inflow("Payment Thank You a", is_transfer=1)
        row = self._inflow("Payment Thank You b", is_transfer=1)
        actions.create_income_rule(
            self.db, "ui:avery", {"match_desc": "Payment Thank You", "set_transfer": True})
        self.assertIsNone(actions.suggest_transfer_rule_after_mark(self.db, row),
                          "a covering rule should suppress the nudge")

    def test_suggest_ignores_outflows_and_non_transfers(self):
        inc = self._inflow("regular income", is_transfer=0)
        self.assertIsNone(actions.suggest_transfer_rule_after_mark(self.db, inc))


if __name__ == "__main__":
    unittest.main()
