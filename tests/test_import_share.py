"""How the sync records a NEW charge: shared or the owner's own (#022).

Before #022 every synced charge came in shared 50/50, so a card one person
uses for their own things (and its card bill payments, Venmos and fees)
drifted the who-owes-whom balance every week. These tests pin
derivations.import_share's signals, strongest first, the verb that sets a
card's default, its route, and that none of it ever rewrites a past charge.
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import _seedbase

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import actions  # noqa: E402
import derivations  # noqa: E402


def _connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-import-share-")
        self.db_path = Path(self.tmp.name) / "t.db"
        _seedbase.seed_into(self.db_path, seed=43, months=1)
        self.db = _connect(self.db_path)
        ids = [r["id"] for r in self.db.execute(
            "SELECT id FROM members WHERE active = 1 ORDER BY id")]
        self.alta, self.charlee = ids[0], ids[1]
        self.n = 0
        actions.note_bank_account(self.db, "sync", "accC", "Checking")
        actions.set_account_owner(self.db, "ui:alta", "accC", self.charlee)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def charge(self, desc, cents, shared=False, pct=50, payer=None, acct="accC"):
        self.n += 1
        data = {"date": "2026-11-03", "amount": f"{cents / 100:.2f}",
                "description": desc, "paid_by": payer or self.charlee,
                "is_shared": shared}
        if shared:
            data["payer_share_pct"] = pct
        return actions.record_transaction(
            self.db, "sync", data, "simplefin",
            external_id=f"simplefin:{acct}:x{self.n}")["id"]

    def share(self, desc, cents=1000, acct="accC"):
        return derivations.import_share(self.db, acct, desc, cents)


class ImportShareTests(_Base):
    def test_an_unknown_charge_starts_as_the_owners_own(self):
        self.assertEqual({"is_shared": False, "payer_share_pct": 50,
                          "why": "card default"}, self.share("NAIL SALON 12"))
        self.assertFalse(derivations.import_share(self.db)["is_shared"])  # bare

    def test_a_card_set_to_start_shared_starts_shared(self):
        actions.set_account_shared_default(self.db, "ui:alta", "accC", True)
        self.assertEqual({"is_shared": True, "payer_share_pct": 50,
                          "why": "card default"}, self.share("GROCER 44"))

    def test_money_movement_never_starts_shared(self):
        actions.set_account_shared_default(self.db, "ui:alta", "accC", True)
        for desc in ("SOMEBANK   DES:CC PYMT    ID:X",
                     "CARDCO US   DES:CREDITCARD ID:X",
                     "APPLECARD GSBANK DES:PAYMENT    ID:1",
                     "BANKCO CREDIT CRD AUTOPAY",
                     "CARDCO E-PAYMENT 1234",
                     "PMNT SENT  0101 VENMO *Sam",
                     "Zelle payment to Sam",
                     "PURCHASE INTEREST CHARGE",
                     "Monthly Maintenance Fee",
                     "Online Banking transfer to SAV 0001"):
            got = self.share(desc)
            self.assertFalse(got["is_shared"], desc)
            self.assertEqual("money movement", got["why"], desc)
        # The rent's bill-pay descriptor is not a card payment.
        self.assertTrue(self.share("RENTPORTAL   DES:BILLPAY    ID:A")["is_shared"])

    def test_a_joint_account_moves_nobodys_balance(self):
        actions.note_bank_account(self.db, "sync", "accJ", "Joint")
        actions.set_account_owner(self.db, "ui:alta", "accJ", None, joint=True)
        self.assertEqual({"is_shared": True, "payer_share_pct": 100, "why": "joint"},
                         self.share("PMNT SENT VENMO *Sam", acct="accJ"))

    def test_a_merchant_a_person_decided_follows_that_decision(self):
        old = self.charge("CHECKCARD 0101 WAREHOUSE CLUB #0123", 9000)
        self.assertFalse(self.share("CHECKCARD 0201 WAREHOUSE CLUB #0123")["is_shared"])
        actions.edit_transaction(self.db, "ui:charlee", old,
                                 {"is_shared": True, "payer_share_pct": 60})
        got = self.share("CHECKCARD 0201 WAREHOUSE CLUB #0123")
        self.assertEqual({"is_shared": True, "payer_share_pct": 60,
                          "why": "you decided before"}, got)
        # A person's "shared" beats the money-movement guess too.
        v = self.charge("PMNT SENT 0101 VENMO *Sam", 3000)
        actions.edit_transaction(self.db, "ui:alta", v, {"is_shared": True})
        self.assertTrue(self.share("PMNT SENT 0201 VENMO *Sam")["is_shared"])

    def test_a_script_or_the_sync_is_not_a_decision(self):
        old = self.charge("CHECKCARD 0101 WAREHOUSE CLUB #0123", 9000)
        actions.edit_transaction(self.db, "cli:fix_payers", old, {"is_shared": True})
        self.assertFalse(self.share("CHECKCARD 0201 WAREHOUSE CLUB #0123")["is_shared"])
        # A payer-only edit says nothing about sharing.
        actions.edit_transaction(self.db, "ui:alta", old, {"paid_by": self.alta})
        self.assertFalse(self.share("CHECKCARD 0201 WAREHOUSE CLUB #0123")["is_shared"])

    def test_a_bill_charge_keeps_the_split_it_had_last_time(self):
        bill = actions.create_bill(self.db, "ui:alta", {
            "name": "Rent", "amount": "2000.00", "due_day": 1})
        bill_id = bill["id"] if hasattr(bill, "keys") else bill
        last = self.charge("RENTPORTAL DES:BILLPAY ID:A1", 200000,
                           shared=True, pct=50, payer=self.alta)
        actions.link_bill_payment(self.db, "ui:alta", bill_id,
                                  {"period": "2026-11", "txn_id": last})
        self.assertEqual({"is_shared": True, "payer_share_pct": 50, "why": "bill"},
                         self.share("RENTPORTAL DES:BILLPAY ID:B2", 200000))
        # Far from the usual amount, the bill signal doesn't fire.
        self.assertEqual("card default",
                         self.share("RENTPORTAL DES:BILLPAY ID:B2", 9900)["why"])

    def test_nothing_here_moves_a_past_charge_or_the_balance(self):
        self.charge("GROCER 44", 4000, shared=True)
        before = ([tuple(r) for r in self.db.execute(
            "SELECT * FROM transactions ORDER BY id")],
            [tuple(r) for r in self.db.execute("SELECT * FROM splits ORDER BY transaction_id, member_id")],
            derivations.compute_balance(self.db))
        actions.set_account_shared_default(self.db, "ui:alta", "accC", True)
        actions.set_account_shared_default(self.db, "ui:alta", "accC", False)
        self.share("GROCER 44")
        after = ([tuple(r) for r in self.db.execute(
            "SELECT * FROM transactions ORDER BY id")],
            [tuple(r) for r in self.db.execute("SELECT * FROM splits ORDER BY transaction_id, member_id")],
            derivations.compute_balance(self.db))
        self.assertEqual(before, after)


class SharedDefaultVerbTests(_Base):
    def test_validation_and_audit(self):
        with self.assertRaises(actions.ActionError):
            actions.set_account_shared_default(self.db, "ui:alta", "accC", 1)
        with self.assertRaises(actions.NotFound):
            actions.set_account_shared_default(self.db, "ui:alta", "NEVERSEEN", True)
        row = actions.set_account_shared_default(self.db, "ui:alta", "accC", True)
        self.assertEqual(1, row["shared_default"])
        self.assertEqual(self.charlee, row["member_id"])   # owner untouched
        audit = self.db.execute(
            "SELECT actor, action FROM audit_log WHERE target = 'bank_account:accC' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(("ui:alta", "set_account_shared_default"), tuple(audit))
        acct = next(a for a in derivations.bank_accounts(self.db)
                    if a["account_id"] == "accC")
        self.assertTrue(acct["shared_default"])


class SharedDefaultRouteTests(_Base):
    def setUp(self):
        super().setUp()
        self.charge("Target", 2500)
        self.token = actions.create_api_token(
            self.db, "ui:alta",
            {"label": "t", "user_id": 1, "scopes": "read,write"})["token"]
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "import-share-secret")
        spec = importlib.util.spec_from_file_location("app_import_share", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True
        self.c = self.app_module.app.test_client()
        with self.c.session_transaction() as session:
            session["user_id"] = 1

    def test_set_and_read_back(self):
        accts = self.c.get("/api/accounts").get_json()["accounts"]
        self.assertFalse(next(a for a in accts if a["account_id"] == "accC")
                         ["shared_default"])
        r = self.c.put("/api/accounts/accC/shared", json={"shared": True})
        self.assertEqual(200, r.status_code, r.get_json())
        self.assertTrue(r.get_json()["account"]["shared_default"])
        self.assertEqual(400, self.c.put("/api/accounts/accC/shared",
                                         json={"shared": "yes"}).status_code)
        self.assertEqual(404, self.c.put("/api/accounts/NOPE/shared",
                                         json={"shared": True}).status_code)

    def test_a_token_cannot_change_it(self):
        anon = self.app_module.app.test_client()
        r = anon.put("/api/accounts/accC/shared", json={"shared": True},
                     headers={"Authorization": f"Bearer {self.token}"})
        self.assertIn(r.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
