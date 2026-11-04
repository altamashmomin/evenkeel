"""Whose bank account is it (migration #020).

Before #020 the SimpleFIN sync credited every imported charge to one person
(SYNC_PAID_BY), so a charge on Charlee's card read "Altamash paid". These
tests pin the fix end to end:

- note_bank_account / set_account_owner: validation, audit only on a change,
  and neither one ever touching a transaction (owners are a rule for the
  future; invariant 3 forbids silently moving past money).
- bank_accounts / payer_fixes derivations: counts, mismatches, settled rows
  left out by default.
- the sync credits the account's owner for new imports, and falls back to
  SYNC_PAID_BY when no owner is set.
- the routes: read for any login, owner change for a session only.
- fix_payers.py: dry run writes nothing; --apply writes a backup, re-credits
  only "who paid", and never moves an amount or a monthly total.
"""
import contextlib
import importlib.util
import io
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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
    seed = 41

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-acct-owner-")
        self.db_path = Path(self.tmp.name) / "t.db"
        _seedbase.seed_into(self.db_path, seed=self.seed, months=2)
        self.db = _connect(self.db_path)
        self.db.execute("DELETE FROM links WHERE link_type = 'settles'")
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.commit()
        ids = [r["id"] for r in self.db.execute(
            "SELECT id FROM members WHERE active = 1 ORDER BY id")]
        self.alta, self.charlee = ids[0], ids[1]

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def charge(self, acct, txn, cents, payer, when="2026-10-10", shared=True):
        row = actions.record_transaction(self.db, "sync", {
            "date": when, "amount": f"{cents / 100:.2f}", "description": f"Shop {txn}",
            "category": "Groceries", "paid_by": payer, "is_shared": shared},
            "simplefin", external_id=f"simplefin:{acct}:{txn}")
        return row["id"]

    def txn_snapshot(self):
        return [tuple(r) for r in self.db.execute(
            "SELECT id, txn_date, amount_cents, paid_by, is_shared, category "
            "FROM transactions ORDER BY id")]


class AccountVerbTests(_Base):
    def audits(self, verb):
        return self.db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", (verb,)).fetchone()[0]

    def test_note_inserts_once_renames_and_audits_only_on_change(self):
        actions.note_bank_account(self.db, "sync", "ACT-1", "Chase Sapphire")
        actions.note_bank_account(self.db, "sync", "ACT-1", "Chase Sapphire")
        self.assertEqual(1, self.audits("note_bank_account"))
        row = actions.note_bank_account(self.db, "sync", "ACT-1", "  Chase   Freedom ")
        self.assertEqual("Chase Freedom", row["name"])
        self.assertIsNone(row["member_id"])
        self.assertEqual(2, self.audits("note_bank_account"))
        # A blank name never wipes a known one.
        actions.note_bank_account(self.db, "sync", "ACT-1", None)
        self.assertEqual("Chase Freedom", self.db.execute(
            "SELECT name FROM bank_accounts").fetchone()[0])
        self.assertEqual(2, self.audits("note_bank_account"))

    def test_note_never_touches_the_owner(self):
        actions.note_bank_account(self.db, "sync", "ACT-1", "Card")
        actions.set_account_owner(self.db, "ui:alta", "ACT-1", self.charlee)
        actions.note_bank_account(self.db, "sync", "ACT-1", "Card renamed")
        self.assertEqual(self.charlee, derivations.account_owner(self.db, "ACT-1"))

    def test_bad_account_ids_and_names_are_refused(self):
        for bad in ("", "has space", "a:b", None, 5, "x" * 201):
            with self.assertRaises(actions.ActionError):
                actions.note_bank_account(self.db, "sync", bad, "n")
        with self.assertRaises(actions.ActionError):
            actions.note_bank_account(self.db, "sync", "ACT-1", 12)

    def test_owner_needs_a_seen_account_and_an_active_member(self):
        with self.assertRaises(actions.NotFound):
            actions.set_account_owner(self.db, "ui:alta", "NEVER-SEEN", self.alta)
        self.charge("OLD-ACCT", "t1", 1000, self.alta)   # known only from a charge
        for bad in (999, True, "2", 2.0):
            with self.assertRaises(actions.ActionError):
                actions.set_account_owner(self.db, "ui:alta", "OLD-ACCT", bad)
        row = actions.set_account_owner(self.db, "ui:alta", "OLD-ACCT", self.charlee)
        self.assertEqual(self.charlee, row["member_id"])
        row = actions.set_account_owner(self.db, "ui:alta", "OLD-ACCT", None)
        self.assertIsNone(row["member_id"])
        self.assertEqual(2, self.audits("set_account_owner"))

    def test_setting_an_owner_never_rewrites_past_charges(self):
        self.charge("ACT-C", "t1", 2500, self.alta)
        self.charge("ACT-C", "t2", 4000, self.alta)
        before = self.txn_snapshot()
        bal = derivations.compute_balance(self.db)
        actions.set_account_owner(self.db, "ui:alta", "ACT-C", self.charlee)
        self.assertEqual(before, self.txn_snapshot())
        self.assertEqual(bal, derivations.compute_balance(self.db))


class AccountDerivationTests(_Base):
    def test_bank_accounts_counts_and_mismatches(self):
        self.charge("ACT-C", "t1", 2500, self.alta)
        self.charge("ACT-C", "t2", 4000, self.alta)
        self.charge("ACT-C", "t3", 1500, self.charlee)
        actions.note_bank_account(self.db, "sync", "ACT-A", "Alta checking")
        accts = {a["account_id"]: a for a in derivations.bank_accounts(self.db)}
        c = accts["ACT-C"]
        self.assertEqual(3, c["charges"])
        self.assertEqual({self.alta: 2, self.charlee: 1}, c["by_payer"])
        self.assertEqual("Account ending CT-C", c["label"])
        self.assertEqual(0, c["mismatched"])           # no owner yet
        self.assertEqual("Alta checking", accts["ACT-A"]["label"])
        self.assertEqual(0, accts["ACT-A"]["charges"])

        actions.set_account_owner(self.db, "ui:alta", "ACT-C", self.charlee)
        c = {a["account_id"]: a for a in derivations.bank_accounts(self.db)}["ACT-C"]
        self.assertEqual(2, c["mismatched"])
        self.assertEqual(2, c["mismatched_open"])

    def test_inflows_and_manual_rows_never_count(self):
        actions.record_transaction(self.db, "sync", {
            "date": "2026-10-10", "amount": "500.00", "description": "Paycheck",
            "category": "Income", "paid_by": self.alta, "is_shared": False},
            "simplefin", external_id="simplefin:ACT-C:dep1", direction="in")
        actions.record_transaction(self.db, "ui:alta", {
            "date": "2026-10-10", "amount": "20.00", "description": "Cash",
            "category": "Dining", "paid_by": self.alta, "is_shared": True}, "manual")
        actions.set_account_owner(self.db, "ui:alta", "ACT-C", self.charlee)
        c = {a["account_id"]: a for a in derivations.bank_accounts(self.db)}["ACT-C"]
        self.assertEqual(0, c["charges"])
        self.assertEqual([], derivations.payer_fixes(self.db))

    def test_payer_fixes_skips_settled_unless_asked(self):
        t1 = self.charge("ACT-C", "t1", 2500, self.alta, when="2026-09-01")
        bal = derivations.compute_balance(self.db)
        actions.settle_up(self.db, "ui:alta", {
            "date": "2026-09-02", "amount": f"{bal['amount_cents'] / 100:.2f}",
            "description": "x", "category": "Settlement",
            "paid_by": bal["ower"]["id"], "is_shared": True,
            "payer_share_pct": 0, "source": "settlement"})
        t2 = self.charge("ACT-C", "t2", 4000, self.alta, when="2026-10-01")
        self.charge("ACT-C", "t3", 1500, self.charlee, when="2026-10-02")
        self.charge("ACT-X", "t4", 999, self.alta)              # no owner: never
        actions.set_account_owner(self.db, "ui:alta", "ACT-C", self.charlee)
        open_only = derivations.payer_fixes(self.db)
        self.assertEqual([t2], [f["transaction_id"] for f in open_only])
        self.assertEqual(self.charlee, open_only[0]["owner"])
        every = derivations.payer_fixes(self.db, include_settled=True)
        self.assertEqual([t1, t2], [f["transaction_id"] for f in every])
        self.assertEqual([True, False], [f["settled"] for f in every])
        self.assertEqual([], derivations.payer_fixes(self.db, account_id="ACT-X"))


class SyncUsesOwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-acct-sync-")
        self.db_path = Path(self.tmp.name) / "sync.db"
        _seedbase.seed_into(self.db_path, seed=6, months=1)
        access = Path(self.tmp.name) / "access.url"
        access.write_text("https://bridge.simplefin.org/simplefin/access/test\n")
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ["SIMPLEFIN_ACCESS_FILE"] = str(access)
        os.environ["SYNC_PAID_BY"] = "1"
        spec = importlib.util.spec_from_file_location(
            "simplefin_sync_owner_test", REPO / "simplefin_sync.py")
        self.sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sync)

    def tearDown(self):
        for key in ("DATABASE_PATH", "SIMPLEFIN_ACCESS_FILE", "SYNC_PAID_BY"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def run_sync(self, txns, name="Charlee Visa"):
        payload = {"accounts": [{"id": "accC", "name": name, "transactions": txns}]}

        class Resp:
            status_code = 200

            def json(self):
                return payload

        class Req:
            def get(self, url, params=None, timeout=None):
                return Resp()

        with mock.patch.object(self.sync, "requests", Req()), \
                contextlib.redirect_stdout(io.StringIO()):
            self.sync.sync(force=True)

    def paid_by(self, ext):
        db = _connect(self.db_path)
        try:
            return db.execute("SELECT paid_by FROM transactions WHERE external_id = ?",
                              (ext,)).fetchone()[0]
        finally:
            db.close()

    def test_owner_is_credited_for_new_imports_only(self):
        now = int(time.time())
        self.run_sync([{"id": "t1", "amount": "-30.00", "posted": now,
                        "description": "Target"}])
        self.assertEqual(1, self.paid_by("simplefin:accC:t1"))     # default
        db = _connect(self.db_path)
        try:
            self.assertEqual("Charlee Visa", db.execute(
                "SELECT name FROM bank_accounts WHERE account_id='accC'").fetchone()[0])
            actions.set_account_owner(db, "ui:alta", "accC", 2)
        finally:
            db.close()
        self.run_sync([{"id": "t1", "amount": "-30.00", "posted": now,
                        "description": "Target"},
                       {"id": "t2", "amount": "-12.00", "posted": now,
                        "description": "Trader Joe's"}])
        self.assertEqual(1, self.paid_by("simplefin:accC:t1"))     # past row kept
        self.assertEqual(2, self.paid_by("simplefin:accC:t2"))     # new row: owner


class AccountRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-acct-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=8, months=1)
        db = _connect(self.db_path)
        self.txn = actions.record_transaction(db, "sync", {
            "date": "2026-10-10", "amount": "25.00", "description": "Target",
            "category": "Groceries", "paid_by": 1, "is_shared": True},
            "simplefin", external_id="simplefin:ACT-9:z1")["id"]
        self.token = actions.create_api_token(
            db, "ui:alta", {"label": "t", "user_id": 1, "scopes": "read,write"})["token"]
        db.close()
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "acct-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_acct_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True
        self.c = self.app_module.app.test_client()
        with self.c.session_transaction() as session:
            session["user_id"] = 1

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_lookup_and_set_owner(self):
        accts = self.c.get("/api/accounts").get_json()["accounts"]
        acct = next(a for a in accts if a["account_id"] == "ACT-9")
        self.assertIsNone(acct["owner"])
        self.assertEqual(1, acct["charges"])
        one = self.c.get(f"/api/transactions/{self.txn}/account").get_json()["account"]
        self.assertEqual("ACT-9", one["account_id"])
        r = self.c.put("/api/accounts/ACT-9/owner", json={"member_id": 2})
        self.assertEqual(200, r.status_code, r.get_json())
        body = r.get_json()["account"]
        self.assertEqual(2, body["owner"]["id"])
        self.assertEqual(1, body["mismatched"])
        self.assertEqual(400, self.c.put("/api/accounts/ACT-9/owner",
                                         json={"member_id": 99}).status_code)
        self.assertEqual(404, self.c.put("/api/accounts/NOPE/owner",
                                         json={"member_id": 2}).status_code)

    def test_manual_row_has_no_account(self):
        r = self.c.post("/api/transactions", json={
            "date": "2026-10-11", "amount": "9.00", "description": "Cash",
            "category": "Dining", "paid_by": 1, "is_shared": True})
        tid = r.get_json()["id"]
        self.assertIsNone(self.c.get(f"/api/transactions/{tid}/account")
                          .get_json()["account"])
        self.assertEqual(404, self.c.get("/api/transactions/999999/account").status_code)

    def test_a_token_cannot_change_an_owner(self):
        anon = self.app_module.app.test_client()
        self.assertEqual(401, anon.get("/api/accounts").status_code)
        r = anon.put("/api/accounts/ACT-9/owner", json={"member_id": 2},
                     headers={"Authorization": f"Bearer {self.token}"})
        self.assertIn(r.status_code, (401, 403))
        db = _connect(self.db_path)
        try:
            self.assertIsNone(db.execute(
                "SELECT member_id FROM bank_accounts WHERE account_id='ACT-9'")
                .fetchone())
        finally:
            db.close()


class FixPayersScriptTests(_Base):
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location(
            "fix_payers_test", REPO / "fix_payers.py")
        self.fp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.fp)
        self.t1 = self.charge("ACT-C", "t1", 2500, self.alta, when="2026-09-01")
        bal = derivations.compute_balance(self.db)
        actions.settle_up(self.db, "ui:alta", {
            "date": "2026-09-02", "amount": f"{bal['amount_cents'] / 100:.2f}",
            "description": "x", "category": "Settlement",
            "paid_by": bal["ower"]["id"], "is_shared": True,
            "payer_share_pct": 0, "source": "settlement"})
        self.t2 = self.charge("ACT-C", "t2", 4000, self.alta, when="2026-10-01")
        self.t3 = self.charge("ACT-C", "t3", 1200, self.alta, when="2026-10-03",
                              shared=False)
        self.keep = self.charge("ACT-A", "t4", 7000, self.alta, when="2026-10-04")
        actions.set_account_owner(self.db, "ui:alta", "ACT-C", self.charlee)
        actions.set_account_owner(self.db, "ui:alta", "ACT-A", self.alta)

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = self.fp.main(["--db", str(self.db_path), *args])
        return code, out.getvalue()

    def payers(self):
        return {r["id"]: r["paid_by"] for r in self.db.execute(
            "SELECT id, paid_by FROM transactions")}

    def backups(self):
        return [p for p in os.listdir(self.tmp.name) if ".bak-fixpayers-" in p]

    def test_dry_run_writes_nothing(self):
        before = self.txn_snapshot()
        audits = self.db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        code, out = self.run_main()
        self.assertEqual(0, code, out)
        self.assertIn("DRY RUN", out)
        self.assertIn("Would re-credit 2 charge(s)", out)
        self.assertIn("Left alone: 1 charge(s)", out)
        self.assertIn("Money check OK", out)
        self.assertEqual(before, self.txn_snapshot())
        self.assertEqual(audits, self.db.execute(
            "SELECT COUNT(*) FROM audit_log").fetchone()[0])
        self.assertEqual([], self.backups())

    def test_apply_recredits_open_rows_only_and_keeps_money(self):
        amounts = [(r[0], r[2], r[4]) for r in self.txn_snapshot()]
        months = {m: v["total_cents"] for m, v in
                  derivations.spending_summary(self.db).items()}
        code, out = self.run_main("--apply")
        self.assertEqual(0, code, out)
        self.assertEqual(1, len(self.backups()))
        p = self.payers()
        self.assertEqual(self.alta, p[self.t1])        # settled: left alone
        self.assertEqual(self.charlee, p[self.t2])
        self.assertEqual(self.charlee, p[self.t3])     # personal row too
        self.assertEqual(self.alta, p[self.keep])      # already right
        self.assertEqual(amounts, [(r[0], r[2], r[4]) for r in self.txn_snapshot()])
        self.assertEqual(months, {m: v["total_cents"] for m, v in
                                  derivations.spending_summary(self.db).items()})
        self.assertEqual(2, self.db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE actor = 'cli:fix_payers'").fetchone()[0])
        # Re-running finds nothing left to do.
        code, out = self.run_main()
        self.assertIn("Would re-credit 0 charge(s)", out)

    def test_include_settled_reaches_closed_rows(self):
        code, out = self.run_main("--apply", "--include-settled")
        self.assertEqual(0, code, out)
        self.assertIn("reopens it", out)
        self.assertEqual(self.charlee, self.payers()[self.t1])

    def test_apply_keeps_each_persons_share_on_an_uneven_split(self):
        uneven = self.charge("ACT-C", "t9", 10000, self.alta, when="2026-10-05")
        actions.edit_transaction(self.db, "ui:alta", uneven, {"payer_share_pct": 20})
        shares = lambda: {r["member_id"]: r["share_bp"] for r in self.db.execute(
            "SELECT member_id, share_bp FROM splits WHERE transaction_id = ?", (uneven,))}
        self.assertEqual({self.alta: 2000, self.charlee: 8000}, shares())
        code, out = self.run_main("--apply")
        self.assertEqual(0, code, out)
        self.assertEqual(self.charlee, self.payers()[uneven])
        self.assertEqual({self.alta: 2000, self.charlee: 8000}, shares())

    def test_old_schema_is_refused(self):
        self.db.execute("DELETE FROM schema_version WHERE version = 20")
        self.db.commit()
        code, out = self.run_main()
        self.assertEqual(2, code)


if __name__ == "__main__":
    unittest.main()


class JointAccountTests(_Base):
    """Migration #021: a joint account's charges are household spending that
    move nobody's balance (shared, payer's share 100%)."""

    def payer_bp(self, txn_id):
        return self.db.execute(
            "SELECT s.share_bp FROM splits s JOIN transactions t ON t.id = s.transaction_id "
            "WHERE t.id = ? AND s.member_id = t.paid_by", (txn_id,)).fetchone()[0]

    def test_joint_needs_no_single_owner_and_is_audited(self):
        self.charge("JNT", "t1", 1000, self.alta)
        with self.assertRaises(actions.ActionError):
            actions.set_account_owner(self.db, "ui:alta", "JNT", self.alta, joint=True)
        with self.assertRaises(actions.ActionError):
            actions.set_account_owner(self.db, "ui:alta", "JNT", None, joint="yes")
        row = actions.set_account_owner(self.db, "ui:alta", "JNT", None, joint=True)
        self.assertEqual(1, row["joint"])
        self.assertIsNone(row["member_id"])
        self.assertEqual(["JNT"], derivations.joint_accounts(self.db))
        # Picking a person afterwards clears joint.
        row = actions.set_account_owner(self.db, "ui:alta", "JNT", self.charlee)
        self.assertEqual(0, row["joint"])
        self.assertEqual([], derivations.joint_accounts(self.db))

    def test_marking_joint_moves_no_money_until_fixed(self):
        self.charge("JNT", "t1", 3000, self.alta)
        before = (self.txn_snapshot(), derivations.compute_balance(self.db))
        actions.set_account_owner(self.db, "ui:alta", "JNT", None, joint=True)
        self.assertEqual(before, (self.txn_snapshot(), derivations.compute_balance(self.db)))
        a = {x["account_id"]: x for x in derivations.bank_accounts(self.db)}["JNT"]
        self.assertTrue(a["joint"])
        self.assertEqual(1, a["mismatched"])

    def test_payer_fixes_records_joint_charges_the_joint_way(self):
        t1 = self.charge("JNT", "t1", 3000, self.alta, when="2026-10-01")
        t2 = self.charge("JNT", "t2", 2000, self.charlee, when="2026-10-02", shared=False)
        actions.set_account_owner(self.db, "ui:alta", "JNT", None, joint=True)
        fixes = derivations.payer_fixes(self.db)
        self.assertEqual([t1, t2], [f["transaction_id"] for f in fixes])
        self.assertTrue(all(f["joint"] and f["owner"] is None for f in fixes))
        months = {m: v["total_cents"] for m, v in derivations.spending_summary(self.db).items()}
        for f in fixes:
            actions.edit_transaction(self.db, "cli:fix_payers", f["transaction_id"],
                                     {"is_shared": True, "payer_share_pct": 100})
        self.assertEqual([], derivations.payer_fixes(self.db))
        self.assertEqual(10000, self.payer_bp(t1))
        self.assertEqual("settled", derivations.compute_balance(self.db)["state"])
        self.assertEqual(months, {m: v["total_cents"] for m, v in
                                  derivations.spending_summary(self.db).items()})
        a = {x["account_id"]: x for x in derivations.bank_accounts(self.db)}["JNT"]
        self.assertEqual(0, a["mismatched"])

    def test_fix_payers_script_handles_joint(self):
        spec = importlib.util.spec_from_file_location("fix_payers_joint", REPO / "fix_payers.py")
        fp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fp)
        t1 = self.charge("JNT", "t1", 3000, self.alta)
        actions.set_account_owner(self.db, "ui:alta", "JNT", None, joint=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fp.main(["--db", str(self.db_path), "--apply"])
        self.assertEqual(0, code, out.getvalue())
        self.assertIn("joint (nobody owes)", out.getvalue())
        self.assertIn("JOINT", out.getvalue())
        self.assertEqual(10000, self.payer_bp(t1))
        self.assertEqual(self.alta, self.db.execute(
            "SELECT paid_by FROM transactions WHERE id = ?", (t1,)).fetchone()[0])


class JointSyncAndRouteTests(SyncUsesOwnerTests):
    def test_joint_account_imports_move_nobodys_balance(self):
        now = int(time.time())
        self.run_sync([{"id": "t1", "amount": "-30.00", "posted": now, "description": "Target"}])
        db = _connect(self.db_path)
        try:
            actions.set_account_owner(db, "ui:alta", "accC", None, joint=True)
            bal_before = derivations.compute_balance(db)
        finally:
            db.close()
        self.run_sync([{"id": "t2", "amount": "-80.00", "posted": now,
                        "description": "Costco"}])
        db = _connect(self.db_path)
        try:
            row = db.execute("SELECT * FROM transactions WHERE external_id = ?",
                             ("simplefin:accC:t2",)).fetchone()
            bp = db.execute("SELECT share_bp FROM splits WHERE transaction_id = ? "
                            "AND member_id = ?", (row["id"], row["paid_by"])).fetchone()[0]
            self.assertEqual(1, row["is_shared"])
            self.assertEqual(10000, bp)
            self.assertEqual(bal_before, derivations.compute_balance(db))
        finally:
            db.close()
        os.environ["SECRET_KEY"] = os.environ.get("SECRET_KEY", "joint-route-secret")
        spec = importlib.util.spec_from_file_location("app_joint_test", REPO / "app.py")
        app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app_module)
        app_module.app.config["TESTING"] = True
        c = app_module.app.test_client()
        with c.session_transaction() as session:
            session["user_id"] = 1
        month = time.strftime("%Y-%m")
        feed = c.get(f"/api/activity?month={month}").get_json()["transactions"]
        joint = {t["description"]: t["joint"] for t in feed if t["source"] == "simplefin"}
        self.assertTrue(joint["Costco"])
        self.assertFalse(joint["Target"])   # still split the old way
        accts = c.get("/api/accounts").get_json()["accounts"]
        self.assertTrue(next(a for a in accts if a["account_id"] == "accC")["joint"])
        r = c.put("/api/accounts/accC/owner", json={"member_id": 1, "joint": True})
        self.assertEqual(400, r.status_code)
