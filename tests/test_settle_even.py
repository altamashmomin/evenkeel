"""settle_up(method='already_even') + settlement_history + their routes.

"We're already even" is the household saying the balance was wrong and they
were square as of a date. It must be EXACTLY a settlement in the money sense
(same amount, splits, links, so the balance lands on zero the same way), be
labeled honestly ("no money moved"), be undoable by deleting the row, and
never be placed where it would push the books past zero (before the latest
settlement, or in the future). No charge's amount or payer moves."""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import _seedbase

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import actions  # noqa: E402
import derivations  # noqa: E402


def _payload(bal, when, **extra):
    data = {"date": when, "amount": f"{bal['amount_cents'] / 100:.2f}",
            "description": "x", "category": "Settlement",
            "paid_by": bal["ower"]["id"], "is_shared": True,
            "payer_share_pct": 0, "source": "settlement"}
    data.update(extra)
    return data


class SettleEvenVerbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-settle-even-")
        self.db_path = Path(self.tmp.name) / "t.db"
        _seedbase.seed_into(self.db_path, seed=31, months=2)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("DELETE FROM links WHERE link_type = 'settles'")
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.commit()
        self.m = derivations.compute_balance(self.db)["members"]
        self.today = date.today()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def expense(self, when, cents, payer_id):
        cur = self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction) "
            "VALUES (?, ?, 'Groceries run', 'Groceries', ?, 1, 'simplefin', 'out')",
            (when, cents, payer_id))
        actions.write_legacy_two_member_splits(
            self.db, cur.lastrowid, payer_id, True, 50, self.m)
        self.db.commit()
        return cur.lastrowid

    def charges_snapshot(self):
        return [tuple(r) for r in self.db.execute(
            "SELECT id, amount_cents, paid_by, is_shared FROM transactions "
            "WHERE source != 'settlement' ORDER BY id")]

    def test_marks_even_like_a_settlement_and_labels_it(self):
        d = (self.today - timedelta(days=3)).isoformat()
        e1 = self.expense(d, 10000, self.m[0]["id"])
        before = self.charges_snapshot()
        bal = derivations.compute_balance(self.db)
        row = actions.settle_up(self.db, "ui:alta", _payload(
            bal, self.today.isoformat(), method="already_even",
            note="  split it   in person "))
        self.assertEqual("settled", derivations.compute_balance(self.db)["state"])
        self.assertEqual(5000, row["amount_cents"])
        self.assertEqual("settlement", row["source"])
        self.assertIn("no money moved", row["description"])
        self.assertTrue(row["description"].endswith("· split it in person"))
        # It closed the open charge exactly like settle_up does.
        self.assertEqual(1, self.db.execute(
            "SELECT COUNT(*) FROM links WHERE link_type='settles' AND from_id=? "
            "AND to_id=?", (row["id"], e1)).fetchone()[0])
        # No charge's amount, payer or sharing moved.
        self.assertEqual(before, self.charges_snapshot())
        audit = json.loads(self.db.execute(
            "SELECT detail_json FROM audit_log WHERE action='settle_up' "
            "ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual("already_even", audit["method"])
        self.assertEqual("split it in person", audit["note"])

    def test_paid_is_still_the_default_and_unchanged(self):
        self.expense((self.today - timedelta(days=2)).isoformat(), 4000, self.m[1]["id"])
        bal = derivations.compute_balance(self.db)
        row = actions.settle_up(self.db, "ui:alta", _payload(bal, self.today.isoformat()))
        self.assertTrue(row["description"].startswith("Settlement — "))
        audit = json.loads(self.db.execute(
            "SELECT detail_json FROM audit_log WHERE action='settle_up' "
            "ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual("paid", audit["method"])
        self.assertIsNone(audit["note"])

    def test_undo_is_delete_and_restores_the_balance(self):
        self.expense((self.today - timedelta(days=2)).isoformat(), 4000, self.m[1]["id"])
        bal = derivations.compute_balance(self.db)
        row = actions.settle_up(self.db, "ui:alta", _payload(
            bal, self.today.isoformat(), method="already_even"))
        actions.delete_transaction(self.db, "ui:alta", row["id"])
        self.assertEqual(bal, derivations.compute_balance(self.db))

    def test_backdated_even_clears_only_up_to_that_day(self):
        early = (self.today - timedelta(days=20)).isoformat()
        mid = (self.today - timedelta(days=10)).isoformat()
        late = (self.today - timedelta(days=2)).isoformat()
        self.expense(early, 10000, self.m[0]["id"])   # second owes first 50.00
        self.expense(late, 3000, self.m[0]["id"])     # second owes first 15.00
        as_of = derivations.compute_balance(self.db, as_of=mid)
        self.assertEqual(5000, as_of["amount_cents"])
        actions.settle_up(self.db, "ui:alta", _payload(
            as_of, mid, method="already_even"))
        after = derivations.compute_balance(self.db)
        self.assertEqual(("owing", 1500), (after["state"], after["amount_cents"]))

    def test_even_cannot_go_before_the_latest_settlement(self):
        self.expense((self.today - timedelta(days=20)).isoformat(), 10000, self.m[0]["id"])
        bal = derivations.compute_balance(self.db)
        actions.settle_up(self.db, "ui:alta", _payload(
            bal, (self.today - timedelta(days=5)).isoformat()))
        self.expense((self.today - timedelta(days=1)).isoformat(), 2000, self.m[0]["id"])
        back = (self.today - timedelta(days=10)).isoformat()
        stale = derivations.compute_balance(self.db, as_of=back)
        stale = stale if stale["state"] == "owing" else derivations.compute_balance(self.db)
        with self.assertRaisesRegex(actions.ActionError, "last settled"):
            actions.settle_up(self.db, "ui:alta", _payload(
                stale, back, method="already_even"))

    def test_even_cannot_be_in_the_future(self):
        self.expense((self.today - timedelta(days=2)).isoformat(), 4000, self.m[1]["id"])
        bal = derivations.compute_balance(self.db)
        with self.assertRaisesRegex(actions.ActionError, "future"):
            actions.settle_up(self.db, "ui:alta", _payload(
                bal, (self.today + timedelta(days=1)).isoformat(),
                method="already_even"))

    def test_rejects_unknown_method_and_non_text_note(self):
        self.expense((self.today - timedelta(days=2)).isoformat(), 4000, self.m[1]["id"])
        bal = derivations.compute_balance(self.db)
        with self.assertRaisesRegex(actions.ActionError, "method"):
            actions.settle_up(self.db, "ui:alta", _payload(
                bal, self.today.isoformat(), method="forgive"))
        with self.assertRaisesRegex(actions.ActionError, "note"):
            actions.settle_up(self.db, "ui:alta", _payload(
                bal, self.today.isoformat(), method="already_even", note=5))
        self.assertEqual(0, self.db.execute(
            "SELECT COUNT(*) FROM transactions WHERE source='settlement'").fetchone()[0])

    def test_history_reads_method_and_marks_latest(self):
        self.assertEqual([], derivations.settlement_history(self.db))
        self.expense((self.today - timedelta(days=9)).isoformat(), 4000, self.m[1]["id"])
        bal = derivations.compute_balance(self.db)
        first = actions.settle_up(self.db, "ui:alta", _payload(
            bal, (self.today - timedelta(days=8)).isoformat()))
        self.expense((self.today - timedelta(days=3)).isoformat(), 6000, self.m[0]["id"])
        bal = derivations.compute_balance(self.db)
        second = actions.settle_up(self.db, "ui:alta", _payload(
            bal, self.today.isoformat(), method="already_even", note="we're square"))
        hist = derivations.settlement_history(self.db)
        self.assertEqual([second["id"], first["id"]], [h["transaction_id"] for h in hist])
        self.assertEqual(["already_even", "paid"], [h["method"] for h in hist])
        self.assertEqual([True, False], [h["is_latest"] for h in hist])
        self.assertEqual("we're square", hist[0]["note"])
        self.assertEqual(self.m[1]["id"], hist[0]["paid_by"])   # second owed first
        self.assertEqual(self.m[0]["id"], hist[0]["owed"])
        # The new charge plus the earlier settlement row (settle_up closes those too).
        self.assertEqual(2, hist[0]["covers"])

    def test_history_treats_settlements_without_audit_as_paid(self):
        cur = self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction) VALUES "
            "('2026-01-05', 1234, 'legacy', 'Settlement', ?, 1, 'settlement', 'out')",
            (self.m[1]["id"],))
        self.db.commit()
        hist = derivations.settlement_history(self.db)
        self.assertEqual(cur.lastrowid, hist[0]["transaction_id"])
        self.assertEqual("paid", hist[0]["method"])


class SettleEvenRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-settle-even-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=7, months=2)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "settle-even-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_settle_even_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True
        self.c = self.app_module.app.test_client()
        with self.c.session_transaction() as session:
            session["user_id"] = 1

    def tearDown(self):
        self.tmp.cleanup()

    def test_mark_even_through_the_route_then_undo(self):
        bd = self.c.get("/api/settle/breakdown").get_json()
        self.assertEqual("owing", bd["state"])
        before = self.c.get("/api/balance").get_json()
        r = self.c.post("/api/transactions", json={
            "date": date.today().isoformat(),
            "amount": f"{bd['amount']['cents'] / 100:.2f}",
            "description": "Marked even", "category": "Settlement",
            "paid_by": bd["ower"]["id"], "is_shared": True, "payer_share_pct": 0,
            "source": "settlement", "method": "already_even", "note": "all good"})
        self.assertEqual(201, r.status_code, r.get_json())
        self.assertIn("no money moved", r.get_json()["description"])
        self.assertTrue(self.c.get("/api/balance").get_json()["settled"])
        hist = self.c.get("/api/settle/history").get_json()["settlements"]
        self.assertEqual("already_even", hist[0]["method"])
        self.assertEqual("all good", hist[0]["note"])
        self.assertTrue(hist[0]["is_latest"])
        self.assertEqual(["cents", "display"], sorted(hist[0]["amount"]))
        self.assertEqual(200, self.c.delete(
            f"/api/transactions/{hist[0]['transaction_id']}").status_code)
        self.assertEqual(before, self.c.get("/api/balance").get_json())

    def test_breakdown_as_of_and_bad_date(self):
        old = self.c.get("/api/settle/breakdown?as_of=1999-01-01").get_json()
        self.assertEqual("settled", old["state"])
        self.assertEqual("1999-01-01", old["as_of"])
        self.assertEqual(400, self.c.get(
            "/api/settle/breakdown?as_of=not-a-date").status_code)

    def test_bad_method_is_a_400(self):
        bd = self.c.get("/api/settle/breakdown").get_json()
        r = self.c.post("/api/transactions", json={
            "date": date.today().isoformat(),
            "amount": f"{bd['amount']['cents'] / 100:.2f}",
            "description": "x", "category": "Settlement",
            "paid_by": bd["ower"]["id"], "is_shared": True, "payer_share_pct": 0,
            "source": "settlement", "method": "forgive"})
        self.assertEqual(400, r.status_code)

    def test_history_requires_login(self):
        self.assertEqual(401, self.app_module.app.test_client()
                         .get("/api/settle/history").status_code)


if __name__ == "__main__":
    unittest.main()
