"""set_transfer verb + PUT /api/transactions/<id>/transfer (transfer-neutral
fix, increment T2): mark/unmark a transaction as a transfer. Flag-only and
fully reversible; settlements can't be transfers; every change is audited."""
import importlib.util
import os
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


class SetTransferVerbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-set-transfer-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=29, months=2)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def an_inflow(self):
        cur = self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction, income_type) "
            "VALUES ('2026-07-05', 27036, 'Payment Thank You', 'Other', 1, 0, "
            "'simplefin', 'in', 'unclassified')")
        self.db.commit()
        return cur.lastrowid

    def test_mark_sets_flag_and_audits(self):
        tid = self.an_inflow()
        row = actions.set_transfer(self.db, "ui:avery", tid, True)
        self.assertEqual(1, row["is_transfer"])
        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'set_transfer'").fetchone()
        self.assertEqual("ui:avery", audit["actor"])
        self.assertEqual(f"transaction:{tid}", audit["target"])

    def test_unmark_is_reversible(self):
        tid = self.an_inflow()
        actions.set_transfer(self.db, "ui:avery", tid, True)
        row = actions.set_transfer(self.db, "ui:avery", tid, False)
        self.assertEqual(0, row["is_transfer"])

    def test_truthy_values_coerce_to_one(self):
        tid = self.an_inflow()
        self.assertEqual(1, actions.set_transfer(self.db, "ui:a", tid, 1)["is_transfer"])
        self.assertEqual(0, actions.set_transfer(self.db, "ui:a", tid, 0)["is_transfer"])

    def test_missing_row_raises_not_found(self):
        with self.assertRaises(actions.NotFound):
            actions.set_transfer(self.db, "ui:avery", 999999, True)

    def test_settlement_cannot_be_a_transfer(self):
        # Create an owing balance, settle it, then try to flag the settlement.
        bal = derivations.compute_balance(self.db)
        if bal["state"] != "owing":
            self.skipTest("seed not owing")
        settled = actions.settle_up(self.db, "ui:avery", {
            "date": date.today().isoformat(), "amount": bal["amount_cents"] / 100,
            "description": "s", "category": "Settlement",
            "paid_by": bal["ower"]["id"], "is_shared": True,
            "payer_share_pct": 0, "source": "settlement"})
        with self.assertRaisesRegex(actions.ActionError, "settlement cannot be"):
            actions.set_transfer(self.db, "ui:avery", settled["id"], True)

    def test_flag_only_does_not_touch_splits(self):
        # A shared outflow flagged transfer keeps its split rows (reversible);
        # the derivations ignore it while flagged.
        cur = self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction) "
            "VALUES ('2026-07-06', 5000, 'x', 'Groceries', 1, 1, 'manual', 'out')")
        tid = cur.lastrowid
        members = derivations.compute_balance(self.db)["members"]
        actions.write_legacy_two_member_splits(self.db, tid, 1, True, 50, members)
        self.db.commit()
        before = self.db.execute(
            "SELECT COUNT(*) FROM splits WHERE transaction_id = ?", (tid,)).fetchone()[0]
        actions.set_transfer(self.db, "ui:avery", tid, True)
        after = self.db.execute(
            "SELECT COUNT(*) FROM splits WHERE transaction_id = ?", (tid,)).fetchone()[0]
        self.assertEqual(before, after, "splits were mutated")


class SetTransferRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-set-transfer-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=29, months=1)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction, income_type) "
            "VALUES ('2026-07-05', 27036, 'Payment Thank You', 'Other', 1, 0, "
            "'simplefin', 'in', 'unclassified')")
        conn.commit()
        self.tid = conn.execute(
            "SELECT id FROM transactions WHERE description = 'Payment Thank You'").fetchone()[0]
        conn.close()
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "set-transfer-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_set_transfer_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, logged_in=True):
        client = self.app_module.app.test_client()
        if logged_in:
            with client.session_transaction() as session:
                session["user_id"] = 1
        return client

    def test_put_marks_transfer_and_returns_extended_shape(self):
        body = self.client().put(
            f"/api/transactions/{self.tid}/transfer", json={"is_transfer": True}).get_json()
        self.assertTrue(body["is_transfer"])
        self.assertEqual("in", body["direction"])

    def test_marked_transfer_drops_out_of_income_filter(self):
        c = self.client()
        # Before: the inflow shows under the income filter.
        before = c.get("/api/activity?filter=income").get_json()
        self.assertTrue(any(t["id"] == self.tid for t in before["transactions"]))
        c.put(f"/api/transactions/{self.tid}/transfer", json={"is_transfer": True})
        after = c.get("/api/activity?filter=income").get_json()
        self.assertFalse(any(t["id"] == self.tid for t in after["transactions"]),
                         "transfer still shows under income")
        # But it stays visible under 'all', flagged.
        all_feed = c.get("/api/activity?filter=all").get_json()
        row = next(t for t in all_feed["transactions"] if t["id"] == self.tid)
        self.assertTrue(row["is_transfer"])

    def test_missing_row_is_404(self):
        self.assertEqual(404, self.client().put(
            "/api/transactions/999999/transfer", json={"is_transfer": True}).status_code)

    def test_requires_login(self):
        self.assertEqual(401, self.client(logged_in=False).put(
            f"/api/transactions/{self.tid}/transfer", json={"is_transfer": True}).status_code)


if __name__ == "__main__":
    unittest.main()
