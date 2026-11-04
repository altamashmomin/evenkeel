"""Ask check-ins (#019): the assistant starts the conversation.

Covers the `check_ins` / `unusual_charges` derivations rule by rule, the
per-person mute verbs (dismiss, snooze, whole-kind off, back on), and the
three routes. The load-bearing claims:
  - listing check-ins is a pure read (no model call, no write);
  - mutes are per member and never touch money;
  - a dismissed instance stays quiet but the NEXT instance (next month's
    bill, the next restock) asks again.
"""
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

ACTOR = "ui:avery"
TODAY = "2026-11-04"   # well after the seed's July data, so the seed adds no noise

MUTE_ONLY = {"check_in_mutes", "audit_log", "sqlite_sequence"}


def snapshot_rest(db):
    out = {}
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        if name in MUTE_ONLY:
            continue
        out[name] = [tuple(r) for r in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
    return out


class CheckInBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-checkins-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=53, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        # A quiet household: the seed's own bills and budgets would raise
        # check-ins of their own and blur what each test asserts.
        self.db.execute("UPDATE bills SET active = 0")
        self.db.execute("UPDATE budgets SET active = 0")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    # ── fixture helpers (raw rows are fine in a test fixture) ──
    def txn(self, day, cents, desc, category="Shopping", direction="out",
            source="simplefin", is_transfer=0):
        cur = self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, created_at, direction, "
            "is_transfer) VALUES (?, ?, ?, ?, 1, 1, ?, '2026-10-01T00:00:00', ?, ?)",
            (day, cents, desc, category, source, direction, is_transfer))
        self.db.commit()
        return cur.lastrowid

    def bill(self, name, dollars, due_day, autopay=False):
        row = actions.create_bill(self.db, ACTOR, {"name": name, "amount": dollars,
                                                   "due_day": due_day})
        if autopay:
            actions.set_bill_pay_details(self.db, ACTOR, row["id"], {"autopay": True})
        return row["id"]

    def staple(self, name, every_days, stocked_on, status="stocked"):
        row = actions.add_item(self.db, ACTOR, {"name": name, "kind": "staple",
                                                "status": status})
        actions.set_item_interval(self.db, ACTOR, row["id"], every_days)
        self.db.execute("UPDATE items SET last_stocked_at = ? WHERE id = ?",
                        (stocked_on + "T12:00:00+00:00", row["id"]))
        self.db.commit()
        return row["id"]

    def found(self, today=TODAY, mutes=None):
        return derivations.check_ins(self.db, today, mutes)

    def keys(self, today=TODAY, mutes=None):
        return [c["key"] for c in self.found(today, mutes)]


class BillRuleTests(CheckInBase):
    def test_due_soon_and_overdue(self):
        soon = self.bill("Internet", 70, 6)        # Nov 6 = in 2 days
        late = self.bill("Phone", 45, 2)           # Nov 2 = 2 days ago
        later = self.bill("Gym", 30, 20)           # Nov 20 = not yet
        by_key = {c["key"]: c for c in self.found()}
        self.assertEqual("soon", by_key[f"bill_due:{soon}:2026-11"]["level"])
        self.assertEqual(2, by_key[f"bill_due:{soon}:2026-11"]["days"])
        self.assertEqual("overdue", by_key[f"bill_due:{late}:2026-11"]["level"])
        self.assertNotIn(f"bill_due:{later}:2026-11", by_key)
        # Overdue outranks due-soon.
        keys = self.keys()
        self.assertLess(keys.index(f"bill_due:{late}:2026-11"),
                        keys.index(f"bill_due:{soon}:2026-11"))

    def test_next_months_first_days_count(self):
        rent = self.bill("Rent", 2655, 1)
        keys = self.keys(today="2026-11-29")       # Dec 1 in 2 days
        self.assertIn(f"bill_due:{rent}:2026-12", keys)

    def test_paid_and_autopay_bills_stay_quiet(self):
        paid = self.bill("Water", 40, 3)
        actions.mark_bill_paid(self.db, ACTOR, paid, {"period": "2026-11"}, 1)
        auto = self.bill("Loan", 300, 5, autopay=True)
        keys = self.keys()
        self.assertFalse([k for k in keys if k.startswith(f"bill_due:{paid}:")])
        self.assertFalse([k for k in keys if k.startswith(f"bill_due:{auto}:")])

    def test_looks_paid_replaces_due(self):
        bid = self.bill("Electric", 85, 3)
        self.txn("2026-11-02", 8500, "PSEG ELECTRIC PAYMENT", category="Bills")
        found = {c["key"]: c for c in self.found()}
        self.assertNotIn(f"bill_due:{bid}:2026-11", found)
        lp = found[f"bill_looks_paid:{bid}:2026-11"]
        self.assertEqual(8500, lp["txn_amount_cents"])
        self.assertEqual("bill_looks_paid", lp["kind"])


class BudgetAndSpendRuleTests(CheckInBase):
    def test_budget_near_and_over(self):
        actions.set_budget(self.db, ACTOR, {"category": "Dining", "amount": 100})
        actions.set_budget(self.db, ACTOR, {"category": "Groceries", "amount": 100})
        actions.set_budget(self.db, ACTOR, {"category": "Fun", "amount": 100})
        self.txn("2026-11-02", 9500, "CHIPOTLE", category="Dining")      # 95% near
        self.txn("2026-11-02", 12000, "SHOPRITE", category="Groceries")  # over
        self.txn("2026-11-02", 2000, "AMC", category="Fun")              # 20% quiet
        by_key = {c["key"]: c for c in self.found()}
        self.assertIn("budget:Dining:2026-11:near", by_key)
        self.assertIn("budget:Groceries:2026-11:over", by_key)
        self.assertFalse([k for k in by_key if k.startswith("budget:Fun")])
        self.assertEqual(9500, by_key["budget:Dining:2026-11:near"]["actual_cents"])

    def test_spend_spike_when_no_budget_covers_it(self):
        for m in ("2026-08-10", "2026-09-10", "2026-10-10"):
            self.txn(m, 5000, f"PETCO {m}", category="Pets")
        self.txn("2026-11-02", 15000, "PETCO VET", category="Pets")
        keys = self.keys()
        self.assertIn("spend_spike:Pets:2026-11", keys)
        # A budget check-in on the same category takes its place.
        actions.set_budget(self.db, ACTOR, {"category": "Pets", "amount": 100})
        keys = self.keys()
        self.assertIn("budget:Pets:2026-11:over", keys)
        self.assertNotIn("spend_spike:Pets:2026-11", keys)


class UnusualChargeTests(CheckInBase):
    def history(self, cents=8500):
        for d in ("2026-08-15", "2026-09-15", "2026-10-15"):
            self.txn(d, cents, "VERIZON WIRELESS", category="Bills")

    def test_big_jump_at_a_known_merchant(self):
        self.history()
        tid = self.txn("2026-11-03", 21200, "VERIZON WIRELESS", category="Bills")
        u = derivations.unusual_charges(self.db, TODAY)
        self.assertEqual([tid], [x["txn_id"] for x in u])
        self.assertEqual(8500, u[0]["usual_cents"])
        self.assertIn(f"unusual_charge:{tid}", self.keys())

    def test_quiet_cases(self):
        self.history()
        # under 2x
        self.txn("2026-11-03", 15000, "VERIZON WIRELESS", category="Bills")
        # a big inflow, a transfer, an old charge, and a merchant with no history
        self.txn("2026-11-03", 50000, "VERIZON WIRELESS", direction="in")
        self.txn("2026-11-03", 50000, "VERIZON WIRELESS", is_transfer=1)
        self.txn("2026-10-20", 50000, "VERIZON WIRELESS")
        self.txn("2026-11-03", 90000, "BRAND NEW SHOP")
        self.assertEqual([], [x for x in derivations.unusual_charges(self.db, TODAY)
                              if x["txn_date"] >= "2026-10-29"])

    def test_small_jump_is_quiet(self):
        self.history(cents=300)
        self.txn("2026-11-03", 900, "VERIZON WIRELESS")   # 3x but only $6 more
        self.assertEqual([], derivations.unusual_charges(self.db, TODAY))

    def test_bare_calls_return_nothing(self):
        self.assertEqual([], derivations.unusual_charges(self.db))
        self.assertEqual([], derivations.check_ins(self.db))


class PantryRuleTests(CheckInBase):
    def test_restock_due(self):
        coffee = self.staple("Coffee", 14, "2026-10-20")    # due Nov 3
        tea = self.staple("Tea", 14, "2026-10-24")          # due Nov 7 (5 days out)
        soap = self.staple("Soap", 30, "2026-10-01", status="low")  # already on the list
        by_key = {c["key"]: c for c in self.found()}
        hit = by_key[f"restock_due:{coffee}:2026-11-03"]
        self.assertEqual((14, 1), (hit["interval_days"], hit["days_late"]))
        self.assertFalse([k for k in by_key if k.startswith(f"restock_due:{tea}:")])
        self.assertFalse([k for k in by_key if k.startswith(f"restock_due:{soap}:")])

    def test_item_snooze_and_staleness_quiet_it(self):
        coffee = self.staple("Coffee", 14, "2026-10-20")
        actions.set_item_snooze(self.db, ACTOR, coffee, "2026-11-10")
        self.assertFalse([k for k in self.keys() if k.startswith("restock_due:")])
        ancient = self.staple("Flour", 7, "2026-08-01")      # 90 days late
        self.assertFalse([k for k in self.keys() if k.startswith(f"restock_due:{ancient}:")])

    def test_order_waiting(self):
        row = actions.add_item(self.db, ACTOR, {"name": "Dog food", "kind": "staple"})
        actions.set_item_status(self.db, ACTOR, row["id"], "ordered")
        self.db.execute("UPDATE items SET updated_at = '2026-10-25T10:00:00+00:00' "
                        "WHERE id = ?", (row["id"],))
        self.db.commit()
        by_key = {c["key"]: c for c in self.found()}
        self.assertEqual(10, by_key[f"order_waiting:{row['id']}:2026-10-25"]["days_waiting"])
        self.assertFalse([k for k in self.keys(today="2026-10-30")
                          if k.startswith("order_waiting:")])


class MuteFilterTests(CheckInBase):
    def setUp(self):
        super().setUp()
        self.soon = self.bill("Internet", 70, 6)
        self.key = f"bill_due:{self.soon}:2026-11"

    def test_dismiss_snooze_and_kind_off(self):
        self.assertIn(self.key, self.keys())
        self.assertNotIn(self.key, self.keys(mutes=[{"key": self.key, "until": None}]))
        snooze = [{"key": self.key, "until": "2026-11-05"}]
        self.assertNotIn(self.key, self.keys(mutes=snooze))
        self.assertIn(self.key, self.keys(today="2026-11-05", mutes=snooze))
        off = [{"key": "kind:bill_due", "until": None}]
        self.assertNotIn(self.key, self.keys(mutes=off))
        # Another kind is untouched by a bill_due mute.
        coffee = self.staple("Coffee", 14, "2026-10-20")
        self.assertIn(f"restock_due:{coffee}:2026-11-03", self.keys(mutes=off))

    def test_next_instance_asks_again(self):
        mutes = [{"key": self.key, "until": None}]
        self.assertIn(f"bill_due:{self.soon}:2026-12",
                      self.keys(today="2026-12-04", mutes=mutes))


class MuteVerbTests(CheckInBase):
    def rows(self, member=1):
        return [dict(r) for r in self.db.execute(
            "SELECT key, until FROM check_in_mutes WHERE member_id = ? ORDER BY key",
            (member,))]

    def test_upsert_unmute_and_audit(self):
        before = snapshot_rest(self.db)
        actions.mute_check_in(self.db, ACTOR, 1, "bill_due:3:2026-11", "2026-11-06")
        actions.mute_check_in(self.db, ACTOR, 1, "bill_due:3:2026-11")  # snooze → dismiss
        actions.mute_check_in(self.db, ACTOR, 1, "kind:budget")
        self.assertEqual([{"key": "bill_due:3:2026-11", "until": None},
                          {"key": "kind:budget", "until": None}], self.rows())
        self.assertEqual([], self.rows(member=2))   # per person
        actions.unmute_check_in(self.db, ACTOR, 1, "kind:budget")
        self.assertEqual(["bill_due:3:2026-11"], [r["key"] for r in self.rows()])
        audits = [r["action"] for r in self.db.execute(
            "SELECT action FROM audit_log WHERE action LIKE '%check_in' ORDER BY id")]
        self.assertEqual(["mute_check_in"] * 3 + ["unmute_check_in"], audits)
        self.assertEqual(before, snapshot_rest(self.db), "a mute touched something else")

    def test_validation(self):
        for bad in (None, 5, "", "nokind", "bill_due:", "kind:nope", "nope:1",
                    "bill_due:1\nX", "x" * 200):
            with self.assertRaises(actions.ActionError, msg=repr(bad)):
                actions.mute_check_in(self.db, ACTOR, 1, bad)
        with self.assertRaises(actions.ActionError):
            actions.mute_check_in(self.db, ACTOR, 1, "kind:budget", "next week")
        with self.assertRaises(actions.NotFound):
            actions.mute_check_in(self.db, ACTOR, 999, "kind:budget")
        with self.assertRaises(actions.NotFound):
            actions.unmute_check_in(self.db, ACTOR, 1, "kind:budget")
        self.assertEqual([], self.rows())

    def test_category_subject_may_hold_spaces_and_colons(self):
        actions.mute_check_in(self.db, ACTOR, 1, "budget:Dining Out:2026-11:near")
        self.assertEqual(["budget:Dining Out:2026-11:near"],
                         [r["key"] for r in self.rows()])


class CheckInRouteTests(CheckInBase):
    def setUp(self):
        super().setUp()
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "check-in-route-secret")
        spec = importlib.util.spec_from_file_location("app_checkin_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def client(self, user_id=1):
        c = self.app_module.app.test_client()
        if user_id is not None:
            with c.session_transaction() as s:
                s["user_id"] = user_id
        return c

    def test_list_caps_converts_money_and_reports_kinds(self):
        for name, day in (("A", 2), ("B", 3), ("C", 5), ("D", 6)):
            self.bill(name, 50, day)
        res = self.client().get(f"/api/check-ins?today={TODAY}")
        self.assertEqual(200, res.status_code, res.get_json())
        body = res.get_json()
        self.assertEqual(4, body["total"])
        self.assertEqual(3, len(body["check_ins"]))
        first = body["check_ins"][0]
        self.assertEqual({"cents": 5000, "display": "$50.00"}, first["amount"])
        self.assertNotIn("amount_cents", first)
        self.assertEqual(list(derivations.CHECK_IN_KINDS),
                         [k["kind"] for k in body["kinds"]])
        self.assertTrue(all(k["on"] for k in body["kinds"]))

    def test_mutes_are_per_person_end_to_end(self):
        bid = self.bill("Internet", 70, 6)
        key = f"bill_due:{bid}:2026-11"
        c1, c2 = self.client(1), self.client(2)
        res = c1.post("/api/check-ins/mute", json={"key": "kind:bill_due"})
        self.assertEqual(201, res.status_code, res.get_json())
        one = c1.get(f"/api/check-ins?today={TODAY}").get_json()
        two = c2.get(f"/api/check-ins?today={TODAY}").get_json()
        self.assertNotIn(key, [c["key"] for c in one["check_ins"]])
        self.assertIn(key, [c["key"] for c in two["check_ins"]])
        self.assertFalse(next(k for k in one["kinds"] if k["kind"] == "bill_due")["on"])
        self.assertEqual([{"key": "kind:bill_due", "until": None}], one["mutes"])
        res = c1.delete("/api/check-ins/mute", json={"key": "kind:bill_due"})
        self.assertEqual(200, res.status_code)
        again = c1.get(f"/api/check-ins?today={TODAY}").get_json()
        self.assertIn(key, [c["key"] for c in again["check_ins"]])

    def test_expired_snoozes_are_not_reported(self):
        c = self.client()
        c.post("/api/check-ins/mute", json={"key": "kind:budget", "until": "2026-11-01"})
        body = c.get(f"/api/check-ins?today={TODAY}").get_json()
        self.assertEqual([], body["mutes"])

    def test_errors_and_auth(self):
        c = self.client()
        self.assertEqual(400, c.get("/api/check-ins?today=soon").status_code)
        self.assertEqual(400, c.post("/api/check-ins/mute", json={"key": "nope"}).status_code)
        self.assertEqual(404, c.delete("/api/check-ins/mute",
                                       json={"key": "kind:budget"}).status_code)
        anon = self.client(user_id=None)
        self.assertEqual(401, anon.get("/api/check-ins").status_code)
        self.assertEqual(401, anon.post("/api/check-ins/mute",
                                        json={"key": "kind:budget"}).status_code)

    def test_listing_writes_nothing(self):
        self.bill("Internet", 70, 6)
        before = snapshot_rest(self.db)
        audits = self.db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        self.client().get(f"/api/check-ins?today={TODAY}")
        self.assertEqual(before, snapshot_rest(self.db))
        self.assertEqual(audits, self.db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
