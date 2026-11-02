"""bill_payment_candidates: the Calendar's "Looks paid … Link it?" suggestion.

Pins the matching rules (exact amount; same merchant as last month's linked
charge or a bill-name word, within ±50%), the ±10-day window around the due
date, one charge per bill and one bill per charge, that paid bills get no
suggestion, that inflows/transfers/settlements/logged bill transactions are
never offered, and that the derivation writes nothing."""
import importlib.util
import os
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

PERIOD = "2030-02"
PREV = "2030-01"


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-bill-cand-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=41, months=2)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.rent = actions.create_bill(self.db, "ui:avery", {
            "name": "Rent", "amount": "2400.00", "due_day": 1, "category": "Rent"})
        self.power = actions.create_bill(self.db, "ui:avery", {
            "name": "Electric & gas (Public Grid)", "amount": "140.00",
            "due_day": 15, "category": "Utilities"})
        self.n = 0

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def charge(self, txn_date, amount, description):
        self.n += 1
        return actions.record_transaction(self.db, "sync", {
            "date": txn_date, "amount": amount, "description": description,
            "category": "Bills", "paid_by": 1, "is_shared": True,
            "payer_share_pct": 50,
        }, source="simplefin", external_id=f"cand-{self.n}")

    def cands(self, period=PERIOD):
        return {c["bill_id"]: c for c in derivations.bill_payment_candidates(self.db, period)}

    def test_bare_call_is_empty(self):
        self.assertEqual([], derivations.bill_payment_candidates(self.db))

    def test_exact_amount_near_the_due_date(self):
        t = self.charge("2030-01-30", "2400.00", "AVALON PISCATAWAY")
        c = self.cands()
        self.assertEqual(t["id"], c[self.rent["id"]]["txn_id"])
        self.assertEqual("exact amount", c[self.rent["id"]]["reason"])
        self.assertNotIn(self.power["id"], c)

    def test_outside_the_window_is_not_suggested(self):
        self.charge("2030-01-20", "2400.00", "AVALON PISCATAWAY")   # 12 days early
        self.charge("2030-02-28", "140.00", "SOMETHING")            # 13 days late
        self.assertEqual({}, self.cands())

    def test_a_varying_bill_matches_by_name_within_half(self):
        t = self.charge("2030-02-16", "171.22", "PUBLIC GRID ENERGY BILL")
        self.charge("2030-02-14", "171.22", "RANDOM STORE")          # no name, not exact
        c = self.cands()
        self.assertEqual(t["id"], c[self.power["id"]]["txn_id"])
        self.assertEqual("name match", c[self.power["id"]]["reason"])

    def test_same_merchant_as_last_month_wins_and_tolerance_holds(self):
        last = self.charge("2030-01-16", "120.00", "COTTAGE ENERGY 0001")
        actions.link_bill_payment(self.db, "ui:avery", self.power["id"],
                                  {"period": PREV, "txn_id": last["id"]})
        exact_other = self.charge("2030-02-15", "140.00", "SOME GYM")
        same = self.charge("2030-02-17", "188.40", "COTTAGE ENERGY 0002")
        c = self.cands()
        self.assertEqual(same["id"], c[self.power["id"]]["txn_id"])
        self.assertEqual("same as last time", c[self.power["id"]]["reason"])
        self.assertNotEqual(exact_other["id"], c[self.power["id"]]["txn_id"])
        # Beyond ±50% the merchant match alone isn't enough.
        actions.link_bill_payment(self.db, "ui:avery", self.power["id"],
                                  {"period": PERIOD, "txn_id": same["id"]})
        self.charge("2030-03-15", "300.00", "COTTAGE ENERGY 0003")
        self.assertNotIn(self.power["id"], self.cands("2030-03"))

    def test_paid_bills_get_no_suggestion(self):
        self.charge("2030-01-31", "2400.00", "AVALON")
        actions.mark_bill_paid(self.db, "ui:avery", self.rent["id"],
                               {"period": PERIOD}, default_paid_by=1)
        self.assertNotIn(self.rent["id"], self.cands())

    def test_one_charge_is_never_offered_to_two_bills(self):
        other = actions.create_bill(self.db, "ui:avery", {
            "name": "Parking", "amount": "2400.00", "due_day": 2})
        self.charge("2030-02-01", "2400.00", "AVALON")
        c = self.cands()
        self.assertEqual(1, len([v for v in c.values() if v["amount_diff_cents"] == 0]))
        self.assertEqual(1, len({v["txn_id"] for v in c.values()}))
        self.assertTrue(self.rent["id"] in c or other["id"] in c)

    def test_inflow_transfer_settlement_never_offered(self):
        inflow = self.charge("2030-02-01", "2400.00", "AVALON REFUND")
        self.db.execute("UPDATE transactions SET direction = 'in' WHERE id = ?",
                        (inflow["id"],))
        self.db.commit()
        xfer = self.charge("2030-02-01", "2400.00", "AVALON XFER")
        actions.set_transfer(self.db, "ui:avery", xfer["id"], True)
        settle = self.charge("2030-02-01", "2400.00", "AVALON SETTLE")
        self.db.execute("UPDATE transactions SET source = 'settlement' WHERE id = ?",
                        (settle["id"],))
        self.db.commit()
        self.assertEqual({}, self.cands())

    def test_reads_only(self):
        self.charge("2030-01-30", "2400.00", "AVALON")
        before = self.db.total_changes
        derivations.bill_payment_candidates(self.db, PERIOD)
        self.assertEqual(before, self.db.total_changes)


class CandidateRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-bill-cand-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=43, months=1)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "bill-cand-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_bill_cand_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.bill_id = actions.create_bill(conn, "ui:avery", {
            "name": "Rent", "amount": "2400.00", "due_day": 1})["id"]
        self.txn_id = actions.record_transaction(conn, "sync", {
            "date": "2030-01-30", "amount": "2400.00", "description": "AVALON",
            "category": "Rent", "paid_by": 1, "is_shared": True,
            "payer_share_pct": 50}, source="simplefin", external_id="cr-1")["id"]
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_candidates_route(self):
        c = self.app_module.app.test_client()
        self.assertEqual(401, c.get("/api/bills/candidates").status_code)
        with c.session_transaction() as session:
            session["user_id"] = 1
        body = c.get(f"/api/bills/candidates?period={PERIOD}").get_json()
        row = next(x for x in body["candidates"] if x["bill_id"] == self.bill_id)
        self.assertEqual(self.txn_id, row["txn_id"])
        self.assertEqual({"cents": 240000, "display": "$2,400.00"}, row["amount"])
        self.assertEqual("exact amount", row["reason"])
        self.assertEqual(400, c.get("/api/bills/candidates?period=2030-13").status_code)


if __name__ == "__main__":
    unittest.main()
