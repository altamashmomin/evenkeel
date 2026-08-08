"""member_breakdown derivation (analytics #11): per-member paid (fronted
shared expenses) vs owed (basis-point fair share) vs net. Shared outflows
only; nets sum to zero (the conservation compute_balance rests on); inflows
and personal (unsplit) outflows are ignored. Hand-built for exact numbers."""
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
sys.path.insert(0, str(REPO))

import derivations

M = "2026-06"


class MemberBreakdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-member-bd-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "68", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def shared(self, amount_cents, paid_by, bp1, bp2, month=M):
        cur = self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, ?, 'shared', 'X', ?, 1, 'manual', 'out')""",
            (f"{month}-10", amount_cents, paid_by))
        tid = cur.lastrowid
        self.db.executemany(
            "INSERT INTO splits (transaction_id, member_id, share_bp) VALUES (?, ?, ?)",
            [(tid, 1, bp1), (tid, 2, bp2)])
        self.db.commit()

    def by_id(self, month=M):
        return {m["member_id"]: m for m in derivations.member_breakdown(self.db, month)}

    def test_single_shared_expense_50_50(self):
        self.shared(6000, paid_by=1, bp1=5000, bp2=5000)
        b = self.by_id()
        self.assertEqual((6000, 3000, 3000),
                         (b[1]["paid_cents"], b[1]["owed_cents"], b[1]["net_cents"]))
        self.assertEqual((0, 3000, -3000),
                         (b[2]["paid_cents"], b[2]["owed_cents"], b[2]["net_cents"]))
        self.assertEqual(0, b[1]["net_cents"] + b[2]["net_cents"])  # conservation

    def test_both_pay_nets_offset(self):
        self.shared(6000, paid_by=1, bp1=5000, bp2=5000)   # m1 owed 3000
        self.shared(4000, paid_by=2, bp1=5000, bp2=5000)   # each owed 2000 more
        b = self.by_id()
        self.assertEqual((6000, 5000, 1000),
                         (b[1]["paid_cents"], b[1]["owed_cents"], b[1]["net_cents"]))
        self.assertEqual((4000, 5000, -1000),
                         (b[2]["paid_cents"], b[2]["owed_cents"], b[2]["net_cents"]))

    def test_uneven_basis_point_split(self):
        self.shared(6000, paid_by=1, bp1=7000, bp2=3000)   # m1 owes 4200, m2 owes 1800
        b = self.by_id()
        self.assertEqual((6000, 4200, 1800),
                         (b[1]["paid_cents"], b[1]["owed_cents"], b[1]["net_cents"]))
        self.assertEqual((0, 1800, -1800),
                         (b[2]["paid_cents"], b[2]["owed_cents"], b[2]["net_cents"]))

    def test_personal_and_inflow_rows_are_ignored(self):
        self.shared(6000, paid_by=1, bp1=5000, bp2=5000)
        # A personal (unsplit) outflow and a paycheck inflow — neither counts.
        self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, 9999, 'personal', 'X', 1, 0, 'manual', 'out')""",
            (f"{M}-11",))
        self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, 500000, 'pay', 'Other', 1, 0, 'simplefin', 'in', 'paycheck')""",
            (f"{M}-05",))
        self.db.commit()
        b = self.by_id()
        self.assertEqual(6000, b[1]["paid_cents"])   # unchanged by personal/inflow
        self.assertEqual(3000, b[1]["net_cents"])

    def test_odd_cent_split_conserves_and_matches_balance(self):
        # 101¢ split 50/50 does not divide to whole cents. Rounding each
        # member's share independently leaks the residual into the payer's
        # net (nets sum to ±1, not 0) and disagrees with compute_balance.
        # The payer must absorb the residual, exactly as compute_balance
        # rounds only the non-payer's share.
        self.shared(101, paid_by=1, bp1=5000, bp2=5000)
        b = self.by_id()
        self.assertEqual(0, b[1]["net_cents"] + b[2]["net_cents"])  # conservation
        self.assertEqual((101, 51, 50), (b[1]["paid_cents"], b[1]["owed_cents"],
                                         b[1]["net_cents"]))
        self.assertEqual((0, 50, -50), (b[2]["paid_cents"], b[2]["owed_cents"],
                                        b[2]["net_cents"]))
        # The pairwise figure must match compute_balance to the cent.
        bal = derivations.compute_balance(self.db)   # all-time; only this row
        self.assertEqual("owing", bal["state"])
        self.assertEqual(2, bal["ower"]["id"])       # member 2 owes member 1
        self.assertEqual(bal["amount_cents"], -b[2]["net_cents"])   # 50

    def test_odd_cent_split_other_parity(self):
        # 103¢ 50/50 rounds the non-payer's share UP (ties-to-even at .5);
        # the payer's residual makes it conserve the other direction too.
        self.shared(103, paid_by=1, bp1=5000, bp2=5000)
        b = self.by_id()
        self.assertEqual(0, b[1]["net_cents"] + b[2]["net_cents"])
        self.assertEqual((103, 51, 52), (b[1]["paid_cents"], b[1]["owed_cents"],
                                         b[1]["net_cents"]))
        self.assertEqual((0, 52, -52), (b[2]["paid_cents"], b[2]["owed_cents"],
                                        b[2]["net_cents"]))
        self.assertEqual(derivations.compute_balance(self.db)["amount_cents"],
                         -b[2]["net_cents"])         # 52, agrees to the cent

    def test_month_scoping(self):
        self.shared(6000, paid_by=1, bp1=5000, bp2=5000, month="2026-05")
        self.shared(2000, paid_by=1, bp1=5000, bp2=5000, month="2026-06")
        self.assertEqual(1000, self.by_id("2026-06")[1]["net_cents"])   # only June
        self.assertEqual(4000, self.by_id(None)[1]["net_cents"])        # all months


if __name__ == "__main__":
    unittest.main()
