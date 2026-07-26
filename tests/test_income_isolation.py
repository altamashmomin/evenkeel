"""Cross-error regression suite for the sync flip: proves inflows cannot
leak into spending totals or the who-owes-whom balance, per INCOME-DESIGN
invariants 1 and 2. Some fixtures deliberately manufacture an inflow with
split rows attached -- an invariant violation record_transaction itself
never produces -- specifically to prove the derivation layer's explicit
direction filters catch it even if that invariant were ever broken
upstream, not merely benefit from splits happening to be absent today."""
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
import derivations


class IncomeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-income-isolation-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "71", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        # A clean slate for these tests: wipe the seed's own transactions
        # (and their splits) so every total in this file is hand-built and
        # exactly known, rather than "seed total plus my fixture."
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def an_outflow(self, amount_cents=5000, category="Groceries", shared=True):
        cur = self.db.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source)
               VALUES (?, ?, 'spend', ?, 1, ?, 'manual')""",
            (date.today().isoformat(), amount_cents, category, 1 if shared else 0))
        txn_id = cur.lastrowid
        if shared:
            self.db.executemany(
                "INSERT INTO splits (transaction_id, member_id, share_bp) "
                "VALUES (?, ?, ?)", [(txn_id, 1, 5000), (txn_id, 2, 5000)])
        self.db.commit()
        return txn_id

    def an_inflow(self, amount_cents=320000, category="Groceries",
                  with_splits=False, income_type="unclassified"):
        cur = self.db.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source, direction, income_type)
               VALUES (?, ?, 'income', ?, 1, ?, 'simplefin', 'in', ?)""",
            (date.today().isoformat(), amount_cents, category,
             1 if with_splits else 0, income_type))
        txn_id = cur.lastrowid
        if with_splits:
            # Deliberately manufactures the invariant-1 violation described
            # in the module docstring: record_transaction never does this.
            self.db.executemany(
                "INSERT INTO splits (transaction_id, member_id, share_bp) "
                "VALUES (?, ?, ?)", [(txn_id, 1, 5000), (txn_id, 2, 5000)])
        self.db.commit()
        return txn_id

    # --------------------------------------------------------- spending_summary

    def test_spending_summary_excludes_inflow_in_the_same_category_and_month(self):
        self.an_outflow(amount_cents=5000, category="Groceries")
        self.an_inflow(amount_cents=999999, category="Groceries")
        month = date.today().strftime("%Y-%m")
        summary = derivations.spending_summary(self.db, month)[month]
        self.assertEqual(5000, summary["total_cents"])
        self.assertEqual(["Groceries"], [c["category"] for c in summary["by_category"]])
        self.assertEqual(5000, summary["by_category"][0]["amount_cents"])

    def test_spending_summary_with_only_inflows_reports_zero(self):
        self.an_inflow(amount_cents=320000)
        month = date.today().strftime("%Y-%m")
        summary = derivations.spending_summary(self.db, month)[month]
        self.assertEqual(0, summary["total_cents"])
        self.assertEqual([], summary["by_category"])

    # ------------------------------------------------- spending_summary: refunds

    def test_refund_nets_its_category_in_the_month_it_lands(self):
        # The whole point of increment 5: a refund reverses spend in its
        # category. $50 spent, $20 refunded -> $30 net.
        self.an_outflow(amount_cents=5000, category="Groceries")
        self.an_inflow(amount_cents=2000, category="Groceries", income_type="refund")
        month = date.today().strftime("%Y-%m")
        summary = derivations.spending_summary(self.db, month)[month]
        self.assertEqual(3000, summary["total_cents"])
        self.assertEqual(["Groceries"], [c["category"] for c in summary["by_category"]])
        self.assertEqual(3000, summary["by_category"][0]["amount_cents"])

    def test_refund_nets_only_its_own_category(self):
        self.an_outflow(amount_cents=5000, category="Groceries")
        self.an_outflow(amount_cents=3000, category="Dining")
        self.an_inflow(amount_cents=2000, category="Groceries", income_type="refund")
        month = date.today().strftime("%Y-%m")
        summary = derivations.spending_summary(self.db, month)[month]
        by_cat = {c["category"]: c["amount_cents"] for c in summary["by_category"]}
        self.assertEqual(3000, by_cat["Groceries"])
        self.assertEqual(3000, by_cat["Dining"])
        self.assertEqual(6000, summary["total_cents"])

    def test_refund_without_matching_spend_goes_negative_not_clamped(self):
        # INCOME-DESIGN's accepted dip: a refund landing before/without its
        # purchase pushes the category negative. Deliberately not clamped.
        self.an_inflow(amount_cents=2000, category="Groceries", income_type="refund")
        month = date.today().strftime("%Y-%m")
        summary = derivations.spending_summary(self.db, month)[month]
        self.assertEqual(-2000, summary["total_cents"])
        self.assertEqual([{"category": "Groceries", "amount_cents": -2000}],
                         summary["by_category"])

    def test_refund_nets_only_within_its_own_month(self):
        self.an_outflow(amount_cents=5000, category="Groceries")  # this month
        # A refund dated in a clearly different month must not touch this one.
        self.db.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source, direction, income_type)
               VALUES ('2020-01-15', 2000, 'old refund', 'Groceries', 1, 0,
                       'simplefin', 'in', 'refund')""")
        self.db.commit()
        month = date.today().strftime("%Y-%m")
        summary = derivations.spending_summary(self.db, month)[month]
        self.assertEqual(5000, summary["total_cents"])
        # The all-months form nets each refund into its own month only.
        all_months = derivations.spending_summary(self.db)
        self.assertEqual(5000, all_months[month]["total_cents"])
        self.assertEqual(-2000, all_months["2020-01"]["total_cents"])

    def test_only_refund_income_type_nets_spend(self):
        # The bounded-exemption guard that replaces the tripwire's automated
        # coverage: spending_summary is EXEMPT because it nets refunds, but
        # NO other inflow type may reduce spend. A huge inflow of every
        # non-refund type sits in a spent category; the total must not budge.
        month = date.today().strftime("%Y-%m")
        for itype in sorted(actions.INCOME_TYPES - {"refund"}):
            with self.subTest(income_type=itype):
                self.db.execute("DELETE FROM splits")
                self.db.execute("DELETE FROM transactions")
                self.db.commit()
                self.an_outflow(amount_cents=5000, category="Groceries")
                self.an_inflow(amount_cents=999999, category="Groceries",
                               income_type=itype)
                summary = derivations.spending_summary(self.db, month)[month]
                self.assertEqual(5000, summary["total_cents"],
                                 f"{itype} inflow must not net spend")
                self.assertEqual(5000, summary["by_category"][0]["amount_cents"])

    # --------------------------------------------------------- compute_balance

    def test_compute_balance_ignores_inflow_with_no_splits(self):
        self.an_outflow(amount_cents=6000, shared=True)  # member 1 owed 3000 by member 2
        self.an_inflow(amount_cents=999999, with_splits=False)
        balance = derivations.compute_balance(self.db)
        self.assertEqual("owing", balance["state"])
        self.assertEqual(3000, balance["amount_cents"])

    def test_compute_balance_ignores_inflow_even_if_it_had_splits(self):
        # The defense-in-depth case: an inflow that (incorrectly) carries
        # split rows must still not move the balance -- the explicit
        # direction filter, not just the absence of splits, is doing the
        # protecting here.
        self.an_outflow(amount_cents=6000, shared=True)
        self.an_inflow(amount_cents=999999, with_splits=True)
        balance = derivations.compute_balance(self.db)
        self.assertEqual("owing", balance["state"])
        self.assertEqual(3000, balance["amount_cents"])

    def test_compute_balance_settled_when_only_a_mis_split_inflow_exists(self):
        self.an_inflow(amount_cents=100000, with_splits=True)
        balance = derivations.compute_balance(self.db)
        self.assertEqual("settled", balance["state"])

    # --------------------------------------------------------- settle_up coverage

    def test_settle_up_never_covers_a_mis_split_inflow(self):
        shared_id = self.an_outflow(amount_cents=6000, shared=True)
        inflow_id = self.an_inflow(amount_cents=999999, with_splits=True)
        balance = derivations.compute_balance(self.db)
        settled = actions.settle_up(self.db, "ui:avery", {
            "date": date.today().isoformat(),
            "amount": balance["amount_cents"] / 100,
            "description": "test settle", "category": "Settlement",
            "paid_by": balance["ower"]["id"], "is_shared": True,
            "payer_share_pct": 0, "source": "settlement",
        })
        covered = [r["to_id"] for r in self.db.execute(
            "SELECT to_id FROM links WHERE link_type = 'settles' AND from_id = ?",
            (settled["id"],)).fetchall()]
        self.assertEqual([shared_id], covered)
        self.assertNotIn(inflow_id, covered)


if __name__ == "__main__":
    unittest.main()
