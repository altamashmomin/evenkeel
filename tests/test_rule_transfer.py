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


if __name__ == "__main__":
    unittest.main()
