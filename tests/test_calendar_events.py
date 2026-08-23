"""calendar_events — the derivation behind the .ics subscription feed.

Bills expand to one occurrence per month (due_day clamped to real month
lengths, paid ✓ from bill_payments existence only), goals contribute their
future target dates, and shopping deadlines come from the same shopping_list /
on_the_way derivations the SPA renders (hard rule 4 — one function per fact).
Clock-free: as_of is a parameter; as_of=None returns [] purely so the
derivation tripwire can call it bare. Reads bills/bill_payments/goals/items,
never transactions, so no inflow can move it."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _seedbase

from derivations import calendar_events

AS_OF = "2026-07-19"


class CalendarEventsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-calfeed-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=73, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        # Isolate from whatever the seed put in the dated tables.
        for table in ("bill_payments", "bills", "goals", "items"):
            self.db.execute(f"DELETE FROM {table}")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _bill(self, name, cents, due_day, active=1):
        cur = self.db.execute(
            "INSERT INTO bills (name, amount_cents, due_day, active) "
            "VALUES (?, ?, ?, ?)", (name, cents, due_day, active))
        self.db.commit()
        return cur.lastrowid

    def _item(self, name, status="low", kind="staple", need_by=None,
              store=None, active=1):
        cur = self.db.execute(
            "INSERT INTO items (name, kind, status, active, need_by, store, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, "
            "'2026-07-01T00:00:00', '2026-07-01T00:00:00')",
            (name, kind, status, active, need_by, store))
        self.db.commit()
        return cur.lastrowid

    def test_none_as_of_returns_empty(self):
        self._bill("Electric", 9000, 15)
        self.assertEqual([], calendar_events(self.db))

    def test_bill_expands_one_occurrence_per_month(self):
        bill_id = self._bill("Electric", 9000, 15)
        events = calendar_events(self.db, as_of=AS_OF, months_ahead=3)
        self.assertEqual(
            ["2026-07-15", "2026-08-15", "2026-09-15"],
            [e["date"] for e in events])
        first = events[0]
        self.assertEqual("bill", first["kind"])
        self.assertEqual(f"ledger-bill-{bill_id}-2026-07", first["uid"])
        self.assertEqual(9000, first["amount_cents"])
        self.assertFalse(first["paid"])

    def test_due_day_clamps_to_short_months(self):
        self._bill("Rent", 120000, 31)
        events = calendar_events(self.db, as_of="2026-01-10", months_ahead=4)
        # Jan 31, Feb 28 (2026 is not a leap year), Mar 31, Apr 30.
        self.assertEqual(
            ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"],
            [e["date"] for e in events])

    def test_paid_flag_comes_from_bill_payments_for_that_period_only(self):
        bill_id = self._bill("Internet", 6000, 5)
        self.db.execute(
            "INSERT INTO bill_payments (bill_id, period, paid_on) "
            "VALUES (?, '2026-07', '2026-07-05')", (bill_id,))
        self.db.commit()
        events = calendar_events(self.db, as_of=AS_OF, months_ahead=2)
        self.assertEqual([True, False], [e["paid"] for e in events])

    def test_inactive_bill_is_excluded(self):
        self._bill("Old gym", 3000, 3, active=0)
        self.assertEqual([], calendar_events(self.db, as_of=AS_OF))

    def test_goal_target_dates_future_only(self):
        self.db.execute(
            "INSERT INTO goals (name, target_cents, target_date, created_at) "
            "VALUES ('Trip', 120000, '2026-09-01', '2026-01-01')")
        self.db.execute(
            "INSERT INTO goals (name, target_cents, target_date, created_at) "
            "VALUES ('Done already', 50000, '2026-06-01', '2026-01-01')")
        self.db.execute(
            "INSERT INTO goals (name, target_cents, target_date, created_at) "
            "VALUES ('Undated', 50000, NULL, '2026-01-01')")
        self.db.commit()
        events = calendar_events(self.db, as_of=AS_OF)
        self.assertEqual(1, len(events))
        self.assertEqual(
            {"kind": "goal", "uid": "ledger-goal-1", "date": "2026-09-01",
             "name": "Trip", "target_cents": 120000}, events[0])

    def test_items_need_shopping_list_membership_and_a_need_by(self):
        self._item("Coffee", status="low", need_by="2026-07-22", store="Costco")
        self._item("Beans", status="out", need_by="2026-07-20")
        self._item("Cake stand", status="ordered", need_by="2026-07-25")
        self._item("Flour", status="stocked", need_by="2026-07-23")   # not on the list
        self._item("Sugar", status="low")                             # no deadline
        self._item("Gone", status="out", need_by="2026-07-21", active=0)
        events = calendar_events(self.db, as_of=AS_OF)
        self.assertEqual(["Beans", "Coffee", "Cake stand"],
                         [e["name"] for e in events])
        self.assertEqual("Costco", events[1]["store"])
        self.assertEqual("ordered", events[2]["status"])

    def test_item_deadlines_expire_14_days_after_passing(self):
        self._item("Recent miss", status="out", need_by="2026-07-06")  # 13 days ago
        self._item("List rot", status="out", need_by="2026-07-04")     # 15 days ago
        events = calendar_events(self.db, as_of=AS_OF)
        self.assertEqual(["Recent miss"], [e["name"] for e in events])

    def test_sorted_by_date_across_kinds(self):
        self._bill("Electric", 9000, 21)
        self._item("Coffee", status="low", need_by="2026-07-20")
        self.db.execute(
            "INSERT INTO goals (name, target_cents, target_date, created_at) "
            "VALUES ('Trip', 120000, '2026-07-22', '2026-01-01')")
        self.db.commit()
        events = calendar_events(self.db, as_of=AS_OF, months_ahead=1)
        self.assertEqual(["2026-07-20", "2026-07-21", "2026-07-22"],
                         [e["date"] for e in events])
        self.assertEqual(["item", "bill", "goal"], [e["kind"] for e in events])


if __name__ == "__main__":
    unittest.main()
