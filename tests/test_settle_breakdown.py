"""settle_breakdown: the line-by-line explanation of the who-owes-whom
balance shown on the settle-up screen.

The load-bearing property is RECONCILIATION: the signed line items must sum
to compute_balance's amount, to the cent, in every state — fresh, after one
settlement, after two, and with a backdated expense. If they ever disagree,
the settle-up number and its explanation contradict each other, which is
worse than no breakdown. Every test here re-asserts that identity against
the canonical compute_balance rather than against a hand-computed figure, so
the guard tracks compute_balance if its rounding ever changes."""
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _seedbase

import actions
import derivations


class SettleBreakdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-settle-breakdown-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=31, months=2)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        # Controlled slate: drop the seed's shared history so the balance is
        # exactly what these expenses make it.
        self.db.execute("DELETE FROM links WHERE link_type = 'settles'")
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.commit()
        self.m = derivations.compute_balance(self.db)["members"]  # [first, second]

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    # --- helpers -----------------------------------------------------------
    def shared_expense(self, when, cents, payer_id, payer_pct=50, desc="x"):
        """A shared outflow with the legacy two-member split."""
        cur = self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction) "
            "VALUES (?, ?, ?, 'Groceries', ?, 1, 'manual', 'out')",
            (when, cents, desc, payer_id))
        txn_id = cur.lastrowid
        actions.write_legacy_two_member_splits(
            self.db, txn_id, payer_id, True, payer_pct, self.m)
        self.db.commit()
        return txn_id

    def settle(self, when=None):
        when = when or date.today().isoformat()
        bal = derivations.compute_balance(self.db, as_of=when)
        if bal["state"] != "owing":
            return None
        return actions.settle_up(self.db, "ui:test", {
            "date": when, "amount": bal["amount_cents"] / 100,
            "description": "settle", "category": "Settlement",
            "paid_by": bal["ower"]["id"], "is_shared": True,
            "payer_share_pct": 0, "source": "settlement"})

    def assert_reconciles(self, expect_clean=True):
        """The heart of the suite: lines + carryover equal compute_balance's
        signed figure, exactly, and amount_cents matches it. `expect_clean`
        asserts carryover is 0 — true whenever every settlement was made
        through settle_up (which links what it closes), so the itemized lines
        alone are the whole story."""
        bal = derivations.compute_balance(self.db)
        bd = derivations.settle_breakdown(self.db)
        signed_sum = sum(line["owed_cents"] for line in bd["lines"])
        if bal["state"] == "owing":
            signed_bal = (bal["amount_cents"]
                          if bal["ower"]["id"] == self.m[1]["id"]
                          else -bal["amount_cents"])
        else:
            signed_bal = 0
        # The reconciliation contract: lines + carryover == the balance.
        self.assertEqual(signed_bal, signed_sum + bd["carryover_cents"],
                         "lines + carryover do not equal the balance")
        self.assertEqual(bal["amount_cents"], bd["amount_cents"])
        self.assertEqual(bd["owed_to_first_cents"] - bd["owed_to_second_cents"],
                         signed_sum, "directional subtotals disagree with lines")
        if expect_clean:
            self.assertEqual(0, bd["carryover_cents"],
                             "clean settle_up history should need no carryover")
        return bal, bd

    # --- reconciliation across the settle cycle ---------------------------
    def test_fresh_household_reconciles_and_is_empty(self):
        bal, bd = self.assert_reconciles()
        self.assertEqual("settled", bd["state"])
        self.assertEqual([], bd["lines"])

    def test_single_expense_reconciles(self):
        self.shared_expense("2026-07-05", 10000, self.m[0]["id"])  # first paid
        bal, bd = self.assert_reconciles()
        self.assertEqual(1, len(bd["lines"]))
        self.assertEqual(5000, bd["lines"][0]["owed_cents"])   # + => second owes first
        self.assertEqual(self.m[1], bd["ower"])

    def test_two_way_expenses_net_and_reconcile(self):
        self.shared_expense("2026-07-05", 10000, self.m[0]["id"])   # 2nd owes 5000
        self.shared_expense("2026-07-06", 4000, self.m[1]["id"])    # 1st owes 2000
        bal, bd = self.assert_reconciles()
        self.assertEqual(2, len(bd["lines"]))
        self.assertEqual(5000, bd["owed_to_first_cents"])
        self.assertEqual(2000, bd["owed_to_second_cents"])
        self.assertEqual(3000, bd["amount_cents"])

    def test_after_settlement_excludes_covered_and_the_settlement_row(self):
        self.shared_expense("2026-07-05", 10000, self.m[0]["id"])
        self.settle("2026-07-10")
        # Everything so far is closed; breakdown is empty and reconciles to 0.
        bal, bd = self.assert_reconciles()
        self.assertEqual("settled", bd["state"])
        self.assertEqual([], bd["lines"], "settled rows / the settlement leaked in")

    def test_new_expense_after_settlement_shows_only_the_open_epoch(self):
        self.shared_expense("2026-07-05", 10000, self.m[0]["id"])
        self.settle("2026-07-10")
        self.shared_expense("2026-07-15", 6000, self.m[1]["id"])  # 1st owes 3000
        bal, bd = self.assert_reconciles()
        self.assertEqual(1, len(bd["lines"]), "only the post-settlement expense")
        self.assertEqual(-3000, bd["lines"][0]["owed_cents"])
        self.assertEqual(self.m[0], bd["ower"])

    def test_second_settlement_cycle_reconciles(self):
        self.shared_expense("2026-07-05", 10000, self.m[0]["id"])
        self.settle("2026-07-10")
        self.shared_expense("2026-07-15", 6000, self.m[1]["id"])
        self.settle("2026-07-20")
        bal, bd = self.assert_reconciles()
        self.assertEqual([], bd["lines"])

    def test_backdated_expense_after_settlement_reconciles(self):
        # A row dated BEFORE the settlement but entered AFTER it: date-based
        # logic would miscount it; the settles-link definition does not.
        self.shared_expense("2026-07-05", 10000, self.m[0]["id"])
        self.settle("2026-07-10")
        self.shared_expense("2026-07-08", 2000, self.m[0]["id"])  # backdated, unsettled
        bal, bd = self.assert_reconciles()
        self.assertEqual(1, len(bd["lines"]))
        self.assertEqual(1000, bd["lines"][0]["owed_cents"])

    def test_odd_cent_share_reconciles_to_compute_balance(self):
        # 70/30 on an odd amount exercises round_ratio; the breakdown must use
        # the same rounding as compute_balance, so they still agree exactly.
        self.shared_expense("2026-07-05", 3333, self.m[0]["id"], payer_pct=70)
        self.assert_reconciles()

    def test_legacy_settlement_without_links_still_reconciles_via_carryover(self):
        # A settlement row inserted directly (no 'settles' links) — the shape
        # legacy/pre-links data or the synthetic seed has. compute_balance
        # counts it; the itemized open lines can't (they'd over-count), so the
        # carryover residual absorbs the difference and the total still equals
        # the balance to the cent.
        self.shared_expense("2026-07-05", 10000, self.m[0]["id"])   # 2nd owes 5000
        # a raw settlement row: 2nd pays 1st 3000, shared, payer covers 0%.
        cur = self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction) "
            "VALUES ('2026-07-08', 3000, 'legacy settle', 'Settlement', ?, 1, "
            "'settlement', 'out')", (self.m[1]["id"],))
        actions.write_legacy_two_member_splits(
            self.db, cur.lastrowid, self.m[1]["id"], True, 0, self.m)
        self.db.commit()
        # Balance is now 5000 - 3000 = 2000 (2nd still owes 1st).
        bal, bd = self.assert_reconciles(expect_clean=False)
        self.assertEqual(2000, bd["amount_cents"])
        self.assertEqual(1, len(bd["lines"]), "only the expense is itemized")
        self.assertEqual(5000, bd["lines"][0]["owed_cents"])
        self.assertEqual(-3000, bd["carryover_cents"], "the raw settle is carryover")

    def test_as_of_matches_compute_balance_window(self):
        self.shared_expense("2026-07-05", 10000, self.m[0]["id"])
        self.shared_expense("2026-08-01", 4000, self.m[0]["id"])
        as_of = "2026-07-31"
        bd = derivations.settle_breakdown(self.db, as_of=as_of)
        bal = derivations.compute_balance(self.db, as_of=as_of)
        self.assertEqual(bal["amount_cents"], bd["amount_cents"])
        self.assertEqual(1, len(bd["lines"]), "the August row is outside the window")


if __name__ == "__main__":
    unittest.main()
