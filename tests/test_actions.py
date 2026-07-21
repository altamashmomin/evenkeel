"""settle_up verb: validation, submission criteria, one-transaction edit
(row + splits + settles links + audit), and coverage semantics."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
sys.path.insert(0, str(REPO))

import actions
import derivations


def settle_body(amount, paid_by, on=None):
    # Mirrors what static/app.js posts from the settle dialog (date=today);
    # seeded rows never postdate today, so a today-dated settlement covers
    # them all under the on-or-before rule.
    return {
        "date": (on or date.today()).isoformat(),
        "amount": amount,
        "description": "Settlement — test",
        "category": "Settlement",
        "paid_by": paid_by,
        "is_shared": True,
        "payer_share_pct": 0,
        "source": "settlement",
    }


class SettleUpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-actions-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "42", "--months", "3", "--as-of", SEED_AS_OF],
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

    def test_settlement_row_splits_links_and_audit_land_together(self):
        shared_before = self.count(
            "SELECT COUNT(DISTINCT transaction_id) FROM splits")
        row = actions.settle_up(self.db, "ui:avery", settle_body(55.25, 1))
        self.assertEqual("settlement", row["source"])
        self.assertEqual(5525, row["amount_cents"])

        splits = self.db.execute(
            "SELECT member_id, share_bp FROM splits WHERE transaction_id = ? "
            "ORDER BY member_id", (row["id"],)).fetchall()
        self.assertEqual([(1, 0), (2, 10000)],
                         [(s["member_id"], s["share_bp"]) for s in splits])

        links = self.db.execute(
            "SELECT to_id FROM links WHERE link_type = 'settles' AND from_id = ?",
            (row["id"],)).fetchall()
        self.assertEqual(shared_before, len(links))
        self.assertNotIn(row["id"], [l["to_id"] for l in links])

        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'settle_up'").fetchall()
        self.assertEqual(1, len(audit))
        self.assertEqual("ui:avery", audit[0]["actor"])
        self.assertEqual(f"transaction:{row['id']}", audit[0]["target"])
        detail = json.loads(audit[0]["detail_json"])
        self.assertEqual(shared_before, len(detail["covers"]))

    def test_second_settlement_covers_only_the_first(self):
        first = actions.settle_up(self.db, "ui:avery", settle_body(10.00, 1))
        second = actions.settle_up(self.db, "ui:blake", settle_body(5.00, 2))
        covered = [l["to_id"] for l in self.db.execute(
            "SELECT to_id FROM links WHERE link_type = 'settles' AND from_id = ?",
            (second["id"],)).fetchall()]
        self.assertEqual([first["id"]], covered)

    def test_rows_dated_after_settlement_stay_uncovered(self):
        tomorrow = date.today() + timedelta(days=1)
        cur = self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source)
               VALUES (?, 4000, 'Future groceries', 'Groceries', 1, 1, 'manual')""",
            (tomorrow.isoformat(),))
        future_id = cur.lastrowid
        self.db.executemany(
            "INSERT INTO splits (transaction_id, member_id, share_bp) VALUES (?, ?, ?)",
            [(future_id, 1, 5000), (future_id, 2, 5000)])
        self.db.commit()

        today_settlement = actions.settle_up(
            self.db, "ui:avery", settle_body(10.00, 1))
        covered_today = [l["to_id"] for l in self.db.execute(
            "SELECT to_id FROM links WHERE link_type = 'settles' AND from_id = ?",
            (today_settlement["id"],)).fetchall()]
        self.assertNotIn(future_id, covered_today)

        later_settlement = actions.settle_up(
            self.db, "ui:blake", settle_body(5.00, 2, on=tomorrow))
        covered_later = [l["to_id"] for l in self.db.execute(
            "SELECT to_id FROM links WHERE link_type = 'settles' AND from_id = ?",
            (later_settlement["id"],)).fetchall()]
        self.assertEqual(sorted([future_id, today_settlement["id"]]),
                         sorted(covered_later))

    def test_full_settlement_zeroes_the_balance(self):
        balance = derivations.compute_balance(self.db)
        self.assertEqual("owing", balance["state"])
        row = actions.settle_up(
            self.db, "ui:avery",
            settle_body(balance["amount_cents"] / 100, balance["ower"]["id"]))
        self.assertIsNotNone(row)
        self.assertEqual("settled", derivations.compute_balance(self.db)["state"])

    def test_submission_criterion_requires_two_active_members(self):
        self.db.execute("UPDATE members SET active = 0 WHERE id = 2")
        self.db.commit()
        with self.assertRaises(actions.ActionError):
            actions.settle_up(self.db, "ui:avery", settle_body(10.00, 1))
        self.assertEqual(
            0, self.count("SELECT COUNT(*) FROM transactions WHERE description = ?",
                          "Settlement — test"))

    def test_submission_criterion_rejects_three_active_members(self):
        self.db.execute(
            "INSERT INTO members (username, display_name, password_hash, active, created_at) "
            "VALUES ('casey', 'Casey', 'disabled', 1, '2026-07-19T00:00:00+00:00')")
        self.db.commit()
        before = self.count("SELECT COUNT(*) FROM transactions")
        with self.assertRaisesRegex(
                actions.ActionError, "settle up requires exactly two active members"):
            actions.settle_up(self.db, "ui:avery", settle_body(10.00, 1))
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(before, self.count("SELECT COUNT(*) FROM transactions"))

    def test_frozen_validation_messages(self):
        with self.assertRaisesRegex(ValueError, "paid_by must be one of the two users"):
            actions.settle_up(self.db, "ui:avery", settle_body(10.00, 99))
        with self.assertRaisesRegex(ValueError, "amount must be positive"):
            actions.settle_up(self.db, "ui:avery", settle_body(0, 1))
        body = settle_body(10.00, 1)
        body["payer_share_pct"] = "33.335"
        with self.assertRaisesRegex(
                ValueError, "payer_share_pct must use at most two decimal places"):
            actions.settle_up(self.db, "ui:avery", body)

    def test_edit_is_atomic_audit_or_nothing(self):
        self.db.execute("DROP TABLE audit_log")
        self.db.commit()
        txns_before = self.count("SELECT COUNT(*) FROM transactions")
        links_before = self.count("SELECT COUNT(*) FROM links")
        with self.assertRaises(sqlite3.OperationalError):
            actions.settle_up(self.db, "ui:avery", settle_body(10.00, 1))
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(txns_before, self.count("SELECT COUNT(*) FROM transactions"))
        self.assertEqual(links_before, self.count("SELECT COUNT(*) FROM links"))


if __name__ == "__main__":
    unittest.main()
