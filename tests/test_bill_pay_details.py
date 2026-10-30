"""Migration #017 + set_bill_pay_details + GET/PUT /api/bills/pay-details:
where a bill is paid and whether it's on autopay. Proves the columns default to
"not recorded / not autopay", the verb's link hygiene (https only, bare host
normalized, blank clears, partial payload keeps the other field), the audit
row, that no money number moves, and that the byte-frozen /api/bills shape is
untouched — the details ride their own endpoint."""
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
from derivations import compute_balance, spending_summary


class BillPayDetailsVerbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-bill-pay-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=13, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.bill = actions.create_bill(self.db, "ui:avery", {
            "name": "Rent", "amount": 2400, "due_day": 1, "category": "Housing"})

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def set(self, data, bill_id=None):
        return actions.set_bill_pay_details(
            self.db, "ui:avery", bill_id or self.bill["id"], data)

    def test_migration_defaults_existing_rows_to_unrecorded(self):
        rows = self.db.execute("SELECT pay_url, autopay FROM bills").fetchall()
        self.assertTrue(rows)
        self.assertEqual({(None, 0)}, {tuple(r) for r in rows})

    def test_migration_017_apply_is_idempotent(self):
        spec = importlib.util.spec_from_file_location(
            "m017", REPO / "migrations" / "017_bill_pay_details.py")
        m017 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m017)
        m017.apply(self.db)   # already applied by the seed — a clean no-op
        m017.apply(self.db)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(bills)")}
        self.assertTrue({"pay_url", "autopay"} <= cols)

    def test_sets_both_and_audits_before_after(self):
        row = self.set({"pay_url": "https://www.avalonaccess.com/", "autopay": False})
        self.assertEqual("https://www.avalonaccess.com/", row["pay_url"])
        self.assertEqual(0, row["autopay"])
        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'set_bill_pay_details'").fetchone()
        self.assertEqual("ui:avery", audit["actor"])
        self.assertEqual(f"bill:{self.bill['id']}", audit["target"])
        detail = json.loads(audit["detail_json"])
        self.assertEqual({"pay_url": None, "autopay": 0}, detail["before"])
        self.assertEqual({"pay_url": "https://www.avalonaccess.com/", "autopay": 0},
                         detail["after"])

    def test_bare_host_gains_https(self):
        row = self.set({"pay_url": "  onepublicgrid.com  "})
        self.assertEqual("https://onepublicgrid.com", row["pay_url"])

    def test_partial_payload_keeps_the_other_field(self):
        self.set({"pay_url": "optimum.net", "autopay": True})
        row = self.set({"autopay": False})
        self.assertEqual("https://optimum.net", row["pay_url"])
        self.assertEqual(0, row["autopay"])
        row = self.set({"pay_url": "https://optimum.net/pay-bill"})
        self.assertEqual(0, row["autopay"])

    def test_blank_clears_the_link(self):
        self.set({"pay_url": "optimum.net"})
        self.assertIsNone(self.set({"pay_url": ""})["pay_url"])
        self.set({"pay_url": "optimum.net"})
        self.assertIsNone(self.set({"pay_url": None})["pay_url"])

    def test_refuses_anything_but_a_plain_https_link(self):
        for bad in ("http://optimum.net", "javascript:alert(1)",
                    "javascript://x.com/%0aalert(1)", "https://me@evil.com",
                    "https://optimum .net", "https://localhost",
                    "https://", "ftp://files.example.com",
                    "https://[::1", "https://a.com/" + "x" * 300, 42):
            with self.subTest(bad=bad):
                with self.assertRaises(actions.ActionError):
                    self.set({"pay_url": bad})
        row = self.db.execute(
            "SELECT pay_url FROM bills WHERE id = ?", (self.bill["id"],)).fetchone()
        self.assertIsNone(row["pay_url"], "a refused link writes nothing")
        self.assertEqual(0, self.db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'set_bill_pay_details'"
        ).fetchone()[0])

    def test_autopay_must_be_boolean(self):
        for bad in ("yes", 2, None, "true"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(actions.ActionError, "autopay"):
                    self.set({"autopay": bad})
        self.assertEqual(1, self.set({"autopay": True})["autopay"])
        self.assertEqual(0, self.set({"autopay": 0})["autopay"])

    def test_empty_payload_is_refused(self):
        with self.assertRaisesRegex(actions.ActionError, "nothing to change"):
            self.set({})

    def test_missing_or_deleted_bill_is_not_found(self):
        with self.assertRaises(actions.NotFound):
            self.set({"autopay": True}, bill_id=999999)
        actions.delete_bill(self.db, "ui:avery", self.bill["id"])
        with self.assertRaises(actions.NotFound):
            self.set({"autopay": True})

    def test_no_money_number_moves(self):
        month = self.db.execute(
            "SELECT MAX(substr(txn_date, 1, 7)) FROM transactions").fetchone()[0]
        balance_before = compute_balance(self.db)
        spend_before = spending_summary(self.db, month)
        txns_before = self.db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        self.set({"pay_url": "avalonaccess.com", "autopay": True})
        self.assertEqual(balance_before, compute_balance(self.db))
        self.assertEqual(spend_before, spending_summary(self.db, month))
        self.assertEqual(txns_before, self.db.execute(
            "SELECT COUNT(*) FROM transactions").fetchone()[0])


class BillPayDetailsRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-bill-pay-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=19, months=1)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "bill-pay-route-test-secret")
        spec = importlib.util.spec_from_file_location(
            "app_bill_pay_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def client(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
        return client

    def test_list_shape_defaults_and_host(self):
        client = self.client()
        bill = client.post("/api/bills", json={
            "name": "Internet", "amount": 10, "due_day": 8}).get_json()
        rows = client.get("/api/bills/pay-details").get_json()
        mine = next(r for r in rows if r["id"] == bill["id"])
        self.assertEqual({"id": bill["id"], "pay_url": None, "pay_host": None,
                          "autopay": False}, mine)
        put = client.put(f"/api/bills/{bill['id']}/pay-details",
                         json={"pay_url": "https://www.optimum.net/pay-bill",
                               "autopay": True})
        self.assertEqual(200, put.status_code)
        self.assertEqual({"id": bill["id"],
                          "pay_url": "https://www.optimum.net/pay-bill",
                          "pay_host": "optimum.net", "autopay": True}, put.get_json())
        listed = client.get("/api/bills/pay-details").get_json()
        self.assertIn(put.get_json(), listed)
        ids = [r["id"] for r in client.get("/api/bills").get_json()]
        self.assertEqual(ids, [r["id"] for r in listed],
                         "same active bills, same order as /api/bills")

    def test_bills_shape_is_unchanged_by_pay_details(self):
        client = self.client()
        bill = client.post("/api/bills", json={
            "name": "Electric", "amount": 25, "due_day": 19}).get_json()
        before = client.get("/api/bills").get_data(as_text=True)
        client.put(f"/api/bills/{bill['id']}/pay-details",
                   json={"pay_url": "onepublicgrid.com", "autopay": True})
        self.assertEqual(before, client.get("/api/bills").get_data(as_text=True))

    def test_bad_link_400_missing_bill_404_anon_401(self):
        client = self.client()
        bill = client.post("/api/bills", json={
            "name": "Rent", "amount": 2400, "due_day": 1}).get_json()
        bad = client.put(f"/api/bills/{bill['id']}/pay-details",
                         json={"pay_url": "http://avalonaccess.com"})
        self.assertEqual(400, bad.status_code)
        self.assertIn("https", bad.get_json()["error"])
        self.assertEqual(404, client.put("/api/bills/999999/pay-details",
                                         json={"autopay": True}).status_code)
        anon = self.app_module.app.test_client()
        self.assertEqual(401, anon.get("/api/bills/pay-details").status_code)
        self.assertEqual(401, anon.put(f"/api/bills/{bill['id']}/pay-details",
                                       json={"autopay": True}).status_code)

    def test_deleted_bill_drops_out_of_the_list(self):
        client = self.client()
        bill = client.post("/api/bills", json={
            "name": "Gone", "amount": 5, "due_day": 3}).get_json()
        client.delete(f"/api/bills/{bill['id']}")
        ids = [r["id"] for r in client.get("/api/bills/pay-details").get_json()]
        self.assertNotIn(bill["id"], ids)


if __name__ == "__main__":
    unittest.main()
