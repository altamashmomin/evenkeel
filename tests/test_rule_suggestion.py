"""suggest_rule_after_classify: the "make this a rule?" read-only decision.

Fires exactly once per income_type -- when tagging brings the count of
inflows carrying a real type to two (the settled auto-rule-aggressiveness
call: wait for a repeat match). Suppressed when an enabled rule already
covers the row. The verb classify_inflow stays a pure one-row edit; this
helper is the separate read the route layers on top.

No verb creates direction='in' rows yet, so inflows are seeded via SQL --
ordinary fixture setup, not an application write path (mirrors
test_classify_inflow)."""
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


class RuleSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-rulesuggest-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=47, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def an_inflow(self, description="ADP PAYROLL 8842"):
        cur = self.db.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source, direction, income_type)
               VALUES (?, 320000, ?, 'Other', 1, 0,
                       'simplefin', 'in', 'unclassified')""",
            (date.today().isoformat(), description))
        self.db.commit()
        return cur.lastrowid

    def an_outflow(self):
        cur = self.db.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source)
               VALUES (?, 5000, 'Groceries', 'Groceries', 1, 0, 'manual')""",
            (date.today().isoformat(),))
        self.db.commit()
        return cur.lastrowid

    def classify(self, txn_id, income_type):
        return actions.classify_inflow(self.db, "ui:avery", txn_id, income_type)

    def test_first_tag_of_a_type_offers_nothing(self):
        row = self.classify(self.an_inflow(), "paycheck")
        self.assertIsNone(actions.suggest_rule_after_classify(self.db, row))

    def test_second_tag_of_same_type_offers_prefilled_rule(self):
        self.classify(self.an_inflow(), "paycheck")
        row = self.classify(self.an_inflow("ACME CORP DIRECT DEP"), "paycheck")
        self.assertEqual(
            {"match_desc": "ACME CORP DIRECT DEP", "set_type": "paycheck"},
            actions.suggest_rule_after_classify(self.db, row))

    def test_third_tag_offers_nothing_a_decline_stays_declined(self):
        self.classify(self.an_inflow(), "paycheck")
        self.classify(self.an_inflow(), "paycheck")
        row = self.classify(self.an_inflow(), "paycheck")
        self.assertIsNone(actions.suggest_rule_after_classify(self.db, row))

    def test_types_are_counted_independently(self):
        # One paycheck + one gift: neither type has reached two, no offer.
        self.classify(self.an_inflow(), "paycheck")
        gift = self.classify(self.an_inflow("BIRTHDAY VENMO"), "gift")
        self.assertIsNone(actions.suggest_rule_after_classify(self.db, gift))
        # The second paycheck then trips only the paycheck offer.
        pay = self.classify(self.an_inflow(), "paycheck")
        offer = actions.suggest_rule_after_classify(self.db, pay)
        self.assertEqual("paycheck", offer["set_type"])

    def test_suppressed_when_an_enabled_rule_already_matches(self):
        actions.create_income_rule(
            self.db, "ui:avery",
            {"set_type": "paycheck", "match_desc": "ADP"})
        self.classify(self.an_inflow(), "paycheck")
        row = self.classify(self.an_inflow(), "paycheck")
        # Count is two, but a live rule already covers these rows -- offering
        # a duplicate would be dead weight (and create_income_rule rejects it).
        self.assertIsNone(actions.suggest_rule_after_classify(self.db, row))

    def test_a_disabled_rule_does_not_suppress_the_offer(self):
        rule = actions.create_income_rule(
            self.db, "ui:avery",
            {"set_type": "paycheck", "match_desc": "ADP"})
        actions.set_rule_enabled(self.db, "ui:avery", rule["id"], False)
        self.classify(self.an_inflow(), "paycheck")
        row = self.classify(self.an_inflow(), "paycheck")
        self.assertIsNotNone(actions.suggest_rule_after_classify(self.db, row))

    def test_reclassifying_to_a_second_type_can_offer(self):
        # Two rows land as gift; retagging one to 'other' leaves one gift,
        # so gift no longer offers, and 'other' has only one -- no offer.
        a = self.an_inflow()
        b = self.an_inflow("STRIPE PAYOUT")
        self.classify(a, "gift")
        self.classify(b, "gift")           # gift now has two
        row = self.classify(a, "other")    # gift back to one, other at one
        self.assertIsNone(actions.suggest_rule_after_classify(self.db, row))

    def test_outflow_row_is_never_offered(self):
        # Defensive: the helper guards on direction even though the route
        # only ever hands it a freshly classified inflow.
        out = self.db.execute(
            "SELECT * FROM transactions WHERE id = ?", (self.an_outflow(),)).fetchone()
        self.assertIsNone(actions.suggest_rule_after_classify(self.db, out))

    def test_match_desc_is_trimmed(self):
        self.classify(self.an_inflow(), "paycheck")
        row = self.classify(self.an_inflow("  PADDED NAME  "), "paycheck")
        offer = actions.suggest_rule_after_classify(self.db, row)
        self.assertEqual("PADDED NAME", offer["match_desc"])


if __name__ == "__main__":
    unittest.main()
