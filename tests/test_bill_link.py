"""link_bill_payment + the non-destructive unmark + bill_link_options.

The money claim this file pins: linking a bill to a charge that is already in
the ledger moves NO money number (balance, every monthly spend total, the
transaction and split row counts), and undoing that link never deletes the
charge. Before this, the only way to say "rent is paid" logged a second
transaction beside the synced bank debit — rent counted twice."""
import importlib.util
import json
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

PERIOD = "2030-01"   # far future: no seeded payment or charge can occupy it


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-bill-link-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=23, months=2)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.bill = actions.create_bill(self.db, "ui:avery", {
            "name": "Rent", "amount": "2400.00", "due_day": 1, "category": "Rent"})

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def count(self, sql, *params):
        return self.db.execute(sql, params).fetchone()[0]

    def raw(self, sql, *params):
        """Fixture-only direct edit (to build an inflow / settlement shape the
        verbs deliberately can't), committed so the next verb starts clean."""
        self.db.execute(sql, params)
        self.db.commit()

    def charge(self, txn_date="2029-12-30", amount="2400.00",
               description="AVALON PISCATAWAY RENT", source="simplefin",
               ext=None):
        return actions.record_transaction(self.db, "sync", {
            "date": txn_date, "amount": amount, "description": description,
            "category": "Rent", "paid_by": 1, "is_shared": True,
            "payer_share_pct": 50,
        }, source=source,
            external_id=ext or (f"ext-{txn_date}-{amount}-{description}"
                                if source == "simplefin" else None))

    def link(self, txn_id, period=PERIOD, bill_id=None):
        return actions.link_bill_payment(
            self.db, "ui:avery", bill_id or self.bill["id"],
            {"period": period, "txn_id": txn_id})

    def money_snapshot(self):
        months = [r[0] for r in self.db.execute(
            "SELECT DISTINCT substr(txn_date, 1, 7) FROM transactions ORDER BY 1")]
        return json.dumps({
            "balance": derivations.compute_balance(self.db),
            "months": {m: derivations.spending_summary(self.db, m) for m in months},
            "txns": self.count("SELECT COUNT(*) FROM transactions"),
            "splits": self.count("SELECT COUNT(*) FROM splits"),
            "links": self.count("SELECT COUNT(*) FROM links"),
        }, sort_keys=True, default=lambda o: dict(o) if isinstance(o, sqlite3.Row) else str(o))


class LinkVerbTests(_Base):
    def test_link_moves_no_money_number_and_writes_payment_plus_audit(self):
        txn = self.charge()
        before = self.money_snapshot()
        bill, period = self.link(txn["id"])
        self.assertEqual(before, self.money_snapshot())
        self.assertEqual(PERIOD, period)
        self.assertEqual(self.bill["id"], bill["id"])

        payment = self.db.execute(
            "SELECT * FROM bill_payments WHERE bill_id = ? AND period = ?",
            (self.bill["id"], PERIOD)).fetchone()
        self.assertEqual(txn["id"], payment["txn_id"])
        self.assertEqual("2029-12-30", payment["paid_on"])   # the charge's own date

        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'link_bill_payment'").fetchone()
        self.assertEqual("ui:avery", audit["actor"])
        self.assertEqual(f"bill:{self.bill['id']}", audit["target"])
        detail = json.loads(audit["detail_json"])
        self.assertEqual({"period": PERIOD, "transaction": txn["id"],
                          "amount_cents": 240000, "txn_date": "2029-12-30"}, detail)

    def test_linked_charge_feeds_bill_variance_and_marks_the_month_paid(self):
        txn = self.charge(amount="2412.50")
        self.link(txn["id"])
        row = next(b for b in derivations.bill_variance(self.db, PERIOD)
                   if b["bill_id"] == self.bill["id"])
        self.assertTrue(row["paid"])
        self.assertEqual(241250, row["actual_cents"])
        self.assertEqual(1250, row["variance_cents"])
        forecast = derivations.cash_flow_forecast(self.db, PERIOD)
        self.assertNotIn(self.bill["id"],
                         [b["bill_id"] for b in forecast["bills_remaining"]])

    def test_validation(self):
        txn = self.charge()
        cases = [
            ({"period": "2030-1", "txn_id": txn["id"]}, "period must be YYYY-MM"),
            ({"period": PERIOD, "txn_id": "7"}, "txn_id must be a transaction id"),
            ({"period": PERIOD, "txn_id": True}, "txn_id must be a transaction id"),
            ({"period": PERIOD, "txn_id": 0}, "txn_id must be a transaction id"),
            ({"period": PERIOD}, "txn_id must be a transaction id"),
        ]
        for data, msg in cases:
            with self.subTest(data=data):
                with self.assertRaisesRegex(actions.ActionError, msg):
                    actions.link_bill_payment(self.db, "ui:avery", self.bill["id"], data)
        with self.assertRaises(actions.NotFound):
            self.link(10 ** 9)
        with self.assertRaises(actions.NotFound):
            self.link(txn["id"], bill_id=10 ** 9)
        self.assertEqual(0, self.count("SELECT COUNT(*) FROM bill_payments WHERE "
                                       "period = ?", PERIOD))

    def test_refuses_inflow_settlement_transfer(self):
        inflow = self.charge(ext="in-1")
        self.raw("UPDATE transactions SET direction = 'in' WHERE id = ?", inflow["id"])
        with self.assertRaisesRegex(actions.ActionError, "only money going out"):
            self.link(inflow["id"])

        settlement = self.charge(ext="set-1")
        self.raw("UPDATE transactions SET source = 'settlement' WHERE id = ?", settlement["id"])
        with self.assertRaisesRegex(actions.ActionError, "settlement"):
            self.link(settlement["id"])

        transfer = self.charge(ext="xfer-1")
        actions.set_transfer(self.db, "ui:avery", transfer["id"], True)
        with self.assertRaisesRegex(actions.ActionError, "transfer"):
            self.link(transfer["id"])

    def test_one_charge_pays_one_bill_and_one_payment_per_month(self):
        txn = self.charge()
        self.link(txn["id"])
        other = actions.create_bill(self.db, "ui:avery", {
            "name": "Electric & gas", "amount": "140.00", "due_day": 15})
        with self.assertRaisesRegex(actions.ActionError, "already pays a bill"):
            self.link(txn["id"], bill_id=other["id"])
        with self.assertRaisesRegex(actions.ActionError, "already pays a bill"):
            self.link(txn["id"], period="2030-02")
        second = self.charge(txn_date="2029-12-31", ext="second")
        with self.assertRaisesRegex(actions.ActionError, "already marked paid"):
            self.link(second["id"])

    def test_a_bill_transaction_ledger_logged_cannot_be_relinked(self):
        actions.mark_bill_paid(self.db, "ui:avery", self.bill["id"],
                               {"period": PERIOD}, default_paid_by=1)
        logged = self.db.execute(
            "SELECT txn_id FROM bill_payments WHERE bill_id = ? AND period = ?",
            (self.bill["id"], PERIOD)).fetchone()["txn_id"]
        with self.assertRaisesRegex(actions.ActionError, "already pays a bill"):
            self.link(logged, period="2030-02")

    def test_inactive_bill_is_not_found(self):
        txn = self.charge()
        actions.delete_bill(self.db, "ui:avery", self.bill["id"])
        with self.assertRaises(actions.NotFound):
            self.link(txn["id"])

    def test_link_is_atomic_audit_or_nothing(self):
        txn = self.charge()
        original = actions._write_audit

        def boom(*a, **k):
            raise RuntimeError("audit failed")
        actions._write_audit = boom
        try:
            with self.assertRaises(RuntimeError):
                self.link(txn["id"])
        finally:
            actions._write_audit = original
        self.assertEqual(0, self.count(
            "SELECT COUNT(*) FROM bill_payments WHERE txn_id = ?", txn["id"]))


class UnlinkTests(_Base):
    def test_unmark_of_a_linked_charge_keeps_the_charge(self):
        txn = self.charge()
        before = self.money_snapshot()
        self.link(txn["id"])
        result = actions.unmark_bill_paid(self.db, "ui:avery", self.bill["id"], PERIOD)
        self.assertEqual({"kept_transaction": txn["id"]}, result)
        self.assertEqual(before, self.money_snapshot())
        self.assertIsNotNone(self.db.execute(
            "SELECT id FROM transactions WHERE id = ?", (txn["id"],)).fetchone())
        self.assertEqual(2, self.count(
            "SELECT COUNT(*) FROM splits WHERE transaction_id = ?", txn["id"]))
        self.assertEqual(0, self.count(
            "SELECT COUNT(*) FROM bill_payments WHERE bill_id = ?", self.bill["id"]))
        detail = json.loads(self.db.execute(
            "SELECT detail_json FROM audit_log WHERE action = 'unmark_bill_paid' "
            "ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(txn["id"], detail["kept_transaction"])
        self.assertIsNone(detail["deleted"])
        # And it can be linked again (a mis-pick is fully undoable).
        self.link(txn["id"])

    def test_unmark_of_a_manual_link_also_keeps_it(self):
        txn = self.charge(source="manual", description="Rent via portal")
        self.link(txn["id"])
        actions.unmark_bill_paid(self.db, "ui:avery", self.bill["id"], PERIOD)
        self.assertIsNotNone(self.db.execute(
            "SELECT id FROM transactions WHERE id = ?", (txn["id"],)).fetchone())

    def test_unmark_of_a_logged_payment_still_deletes_it_and_audit_shape_is_frozen(self):
        actions.mark_bill_paid(self.db, "ui:avery", self.bill["id"],
                               {"period": PERIOD}, default_paid_by=1)
        txn_id = self.db.execute(
            "SELECT txn_id FROM bill_payments WHERE bill_id = ?",
            (self.bill["id"],)).fetchone()[0]
        result = actions.unmark_bill_paid(self.db, "ui:avery", self.bill["id"], PERIOD)
        self.assertEqual({"kept_transaction": None}, result)
        self.assertIsNone(self.db.execute(
            "SELECT id FROM transactions WHERE id = ?", (txn_id,)).fetchone())
        detail = json.loads(self.db.execute(
            "SELECT detail_json FROM audit_log WHERE action = 'unmark_bill_paid' "
            "ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual({"period", "bill", "payment", "deleted"}, set(detail))


class LinkOptionsTests(_Base):
    def test_bare_call_and_unknown_bill_return_empty(self):
        self.assertEqual([], derivations.bill_link_options(self.db))
        self.assertEqual([], derivations.bill_link_options(self.db, 10 ** 9, PERIOD))

    def test_window_filters_and_ranking(self):
        exact = self.charge(txn_date="2029-12-29")
        near = self.charge(txn_date="2030-01-02", amount="2399.00", ext="near")
        far_amount = self.charge(txn_date="2030-01-01", amount="35.10",
                                 description="Coffee", ext="coffee")
        self.charge(txn_date="2029-12-20", ext="too-early")       # > 10 days before
        self.charge(txn_date="2030-02-11", ext="too-late")        # > 10 days after
        inflow = self.charge(txn_date="2030-01-03", ext="inflow")
        self.raw("UPDATE transactions SET direction = 'in' WHERE id = ?", inflow["id"])
        transfer = self.charge(txn_date="2030-01-03", ext="xfer")
        actions.set_transfer(self.db, "ui:avery", transfer["id"], True)
        settlement = self.charge(txn_date="2030-01-03", ext="settle")
        self.raw("UPDATE transactions SET source = 'settlement' WHERE id = ?", settlement["id"])
        other_bill = actions.create_bill(self.db, "ui:avery", {
            "name": "Gym", "amount": "40.00", "due_day": 5})
        linked = self.charge(txn_date="2030-01-05", amount="40.00",
                             description="Gym", ext="gym")
        self.link(linked["id"], bill_id=other_bill["id"])

        opts = derivations.bill_link_options(self.db, self.bill["id"], PERIOD)
        self.assertEqual([exact["id"], near["id"], far_amount["id"]],
                         [o["txn_id"] for o in opts])
        self.assertEqual(0, opts[0]["amount_diff_cents"])
        self.assertEqual(-3, opts[0]["days_from_due"])
        self.assertEqual(-100, opts[1]["amount_diff_cents"])
        self.assertEqual(1, opts[1]["days_from_due"])

    def test_logged_bill_transactions_are_never_offered(self):
        other = actions.create_bill(self.db, "ui:avery", {
            "name": "Phone", "amount": "60.00", "due_day": 3})
        actions.mark_bill_paid(self.db, "ui:avery", other["id"],
                               {"period": PERIOD}, default_paid_by=1)
        # mark_bill_paid dates its transaction today, so look across "today"'s
        # period too — a source='bill' row must never be offered anywhere.
        for period in (PERIOD, derivations._clamped_due_date(
                actions.current_period(), 1).isoformat()[:7]):
            opts = derivations.bill_link_options(self.db, self.bill["id"], period)
            self.assertNotIn("bill", {o["source"] for o in opts})

    def test_inflows_never_appear_even_in_window(self):
        txn = self.charge(txn_date="2030-01-01")
        self.raw("UPDATE transactions SET direction = 'in' WHERE id = ?", txn["id"])
        self.assertEqual([], derivations.bill_link_options(
            self.db, self.bill["id"], PERIOD))


class LinkRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-bill-link-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=29, months=1)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "bill-link-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_bill_link_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        self.bill_id = actions.create_bill(conn, "ui:avery", {
            "name": "Rent", "amount": "2400.00", "due_day": 1})["id"]
        self.txn_id = actions.record_transaction(conn, "sync", {
            "date": "2029-12-30", "amount": "2400.00",
            "description": "AVALON RENT", "category": "Rent", "paid_by": 1,
            "is_shared": True, "payer_share_pct": 50,
        }, source="simplefin", external_id="route-rent")["id"]
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, signed_in=True):
        client = self.app_module.app.test_client()
        if signed_in:
            with client.session_transaction() as session:
                session["user_id"] = 1
        return client

    def test_options_link_and_unlink_round_trip(self):
        c = self.client()
        opts = c.get(f"/api/bills/{self.bill_id}/link-options?period={PERIOD}")
        self.assertEqual(200, opts.status_code)
        body = opts.get_json()
        self.assertEqual(PERIOD, body["period"])
        first = body["options"][0]
        self.assertEqual(self.txn_id, first["txn_id"])
        self.assertEqual({"cents": 240000, "display": "$2,400.00"}, first["amount"])
        self.assertEqual(0, first["amount_diff"]["cents"])

        linked = c.post(f"/api/bills/{self.bill_id}/link",
                        json={"period": PERIOD, "txn_id": self.txn_id})
        self.assertEqual(201, linked.status_code)
        bill = linked.get_json()
        self.assertTrue(bill["paid_this_period"])
        self.assertEqual("2029-12-30", bill["paid_on"])
        self.assertEqual(
            ["amount", "category", "due_day", "id", "name",
             "paid_on", "paid_this_period", "period"], sorted(bill))

        again = c.post(f"/api/bills/{self.bill_id}/link",
                       json={"period": PERIOD, "txn_id": self.txn_id})
        self.assertEqual(400, again.status_code)

        undo = c.delete(f"/api/bills/{self.bill_id}/pay?period={PERIOD}")
        self.assertEqual({"ok": True, "kept_transaction": self.txn_id}, undo.get_json())

    def test_errors_and_auth(self):
        c = self.client()
        self.assertEqual(404, c.post(f"/api/bills/{self.bill_id}/link",
                                     json={"period": PERIOD, "txn_id": 10 ** 9}).status_code)
        self.assertEqual(404, c.post("/api/bills/999999/link",
                                     json={"period": PERIOD, "txn_id": self.txn_id}).status_code)
        self.assertEqual(400, c.get(
            f"/api/bills/{self.bill_id}/link-options?period=2030-13").status_code)
        self.assertEqual(400, c.get(
            f"/api/bills/{self.bill_id}/link-options?period=nope").status_code)
        anon = self.client(signed_in=False)
        self.assertEqual(401, anon.get(
            f"/api/bills/{self.bill_id}/link-options").status_code)
        self.assertEqual(401, anon.post(f"/api/bills/{self.bill_id}/link",
                                        json={"txn_id": self.txn_id}).status_code)


if __name__ == "__main__":
    unittest.main()
