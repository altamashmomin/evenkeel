"""Inventory verbs + derivations (INVENTORY-DESIGN, the pantry): add_item,
set_item_status (incl. the one-off auto-archive on 'bought'), archive_item,
and the shopping_list / low_stock reads. Household-scoped, three-state, no
money touched."""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
sys.path.insert(0, str(REPO))

import _seedbase

import actions
import derivations


class ItemVerbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-items-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=51, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add(self, **data):
        return actions.add_item(self.db, "ui:avery", data)

    def status_of(self, item_id):
        return self.db.execute(
            "SELECT status, active FROM items WHERE id = ?", (item_id,)).fetchone()

    # ------------------------------------------------------------- add_item
    def test_add_staple_defaults_stocked_and_audits(self):
        row = self.add(name="Coffee", category="Groceries")
        self.assertEqual("Coffee", row["name"])
        self.assertEqual("staple", row["kind"])
        self.assertEqual("stocked", row["status"])
        self.assertEqual(1, row["active"])
        audit = self.db.execute(
            "SELECT actor, action FROM audit_log WHERE target = ?",
            (f"item:{row['id']}",)).fetchone()
        self.assertEqual(("ui:avery", "add_item"), (audit["actor"], audit["action"]))

    def test_add_oneoff_defaults_out(self):
        row = self.add(name="Birthday candles", kind="oneoff")
        self.assertEqual("oneoff", row["kind"])
        self.assertEqual("out", row["status"])   # a one-off IS a need

    def test_add_validates_name_kind_status(self):
        with self.assertRaisesRegex(actions.ActionError, "name is required"):
            self.add(name="   ")
        with self.assertRaisesRegex(actions.ActionError, "kind must be"):
            self.add(name="X", kind="gadget")
        with self.assertRaisesRegex(actions.ActionError, "status must be one of"):
            self.add(name="X", status="plenty")

    # ------------------------------------------------------- set_item_status
    def test_set_status_updates_and_audits_before_after(self):
        item = self.add(name="Dish soap")
        updated = actions.set_item_status(self.db, "ui:blake", item["id"], "low")
        self.assertEqual("low", updated["status"])
        detail = json.loads(self.db.execute(
            "SELECT detail_json FROM audit_log WHERE action = 'set_item_status'"
        ).fetchone()["detail_json"])
        self.assertEqual("stocked", detail["before"])
        self.assertEqual("low", detail["after"])
        self.assertFalse(detail["archived"])

    def test_oneoff_bought_archives_itself(self):
        need = self.add(name="Party hats", kind="oneoff")     # status out
        row = actions.set_item_status(self.db, "ui:avery", need["id"], "stocked")
        # bought → leaves the list
        self.assertEqual(0, row["active"])
        self.assertNotIn(need["id"], [i["id"] for i in derivations.shopping_list(self.db)])

    def test_set_status_missing_or_inactive_is_not_found(self):
        with self.assertRaises(actions.NotFound):
            actions.set_item_status(self.db, "ui:avery", 999999, "low")
        item = self.add(name="Gone")
        actions.archive_item(self.db, "ui:avery", item["id"])
        with self.assertRaises(actions.NotFound):
            actions.set_item_status(self.db, "ui:avery", item["id"], "low")

    def test_set_status_rejects_bad_value(self):
        item = self.add(name="Rice")
        with self.assertRaisesRegex(actions.ActionError, "status must be one of"):
            actions.set_item_status(self.db, "ui:avery", item["id"], "loads")

    # -------------------------------------------------------- restock_items
    def test_restock_marks_the_set_stocked_with_an_audit_row_each(self):
        low = self.add(name="Coffee", status="low")
        out = self.add(name="Milk", status="out")
        need = self.add(name="Party hats", kind="oneoff")     # status out
        rows = actions.restock_items(
            self.db, "ui:avery", [low["id"], out["id"], need["id"]])
        self.assertEqual([low["id"], out["id"], need["id"]],
                         [r["id"] for r in rows])              # order given
        self.assertEqual(["stocked"] * 3, [r["status"] for r in rows])
        self.assertEqual(0, rows[2]["active"])                 # oneoff bought → archived
        self.assertEqual(1, rows[0]["active"])
        for r in rows:
            self.assertIsNotNone(r["last_stocked_at"])         # anchor re-set
        audits = self.db.execute(
            "SELECT target, detail_json FROM audit_log "
            "WHERE action = 'restock_items' ORDER BY id").fetchall()
        self.assertEqual([f"item:{r['id']}" for r in rows],
                         [a["target"] for a in audits])        # one row per item
        detail = json.loads(audits[0]["detail_json"])
        self.assertEqual({"before": "low", "after": "stocked", "archived": False},
                         detail)

    def test_restock_is_all_or_nothing(self):
        low = self.add(name="Coffee", status="low")
        with self.assertRaisesRegex(actions.NotFound, "999999"):
            actions.restock_items(self.db, "ui:avery", [low["id"], 999999])
        self.assertEqual("low", self.status_of(low["id"])["status"])  # untouched
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM audit_log WHERE action = 'restock_items'").fetchone())

    def test_restock_rejects_bad_shapes_and_collapses_duplicates(self):
        for bad in (None, [], "7", [7, "soap"]):
            with self.assertRaises(actions.ActionError):
                actions.restock_items(self.db, "ui:avery", bad)
        with self.assertRaisesRegex(actions.ActionError, "max 100"):
            actions.restock_items(self.db, "ui:avery", list(range(1, 102)))
        item = self.add(name="Rice", status="low")
        rows = actions.restock_items(self.db, "ui:avery", [item["id"], item["id"]])
        self.assertEqual(1, len(rows))                         # duplicates collapse
        self.assertEqual(1, len(self.db.execute(
            "SELECT 1 FROM audit_log WHERE action = 'restock_items'").fetchall()))

    # ------------------------- #014 metadata setters (store/need_by/snooze)
    def test_metadata_setters_set_clear_and_audit_without_touching_updated_at(self):
        item = self.add(name="Dog food")
        for verb, column, value in (
                (actions.set_item_store, "store", "Costco"),
                (actions.set_item_need_by, "need_by", "2026-08-22"),
                (actions.set_item_snooze, "snoozed_until", "2026-08-30")):
            row = verb(self.db, "ui:avery", item["id"], value)
            self.assertEqual(value, row[column])
            # Metadata is not a stock event: updated_at (restock inference's
            # since-bound) must not move.
            self.assertEqual(item["updated_at"], row["updated_at"])
            row = verb(self.db, "ui:avery", item["id"], "")     # blank clears
            self.assertIsNone(row[column])
        audits = [r["action"] for r in self.db.execute(
            "SELECT action FROM audit_log WHERE target = ? "
            "AND action LIKE 'set_item_%' ORDER BY id",
            (f"item:{item['id']}",)).fetchall()]
        self.assertEqual(["set_item_store"] * 2 + ["set_item_need_by"] * 2
                         + ["set_item_snooze"] * 2, audits)

    def test_metadata_setters_validate_dates_and_missing_items(self):
        item = self.add(name="Rice")
        for verb in (actions.set_item_need_by, actions.set_item_snooze):
            for bad in ("tomorrow", "2026-8-2", "08/30/2026"):
                with self.assertRaisesRegex(actions.ActionError, "date like"):
                    verb(self.db, "ui:avery", item["id"], bad)
            with self.assertRaises(actions.NotFound):
                verb(self.db, "ui:avery", 999999, "2026-08-30")
        with self.assertRaises(actions.NotFound):
            actions.set_item_store(self.db, "ui:avery", 999999, "Costco")

    def test_shopping_list_sorts_need_by_soonest_first(self):
        later = self.add(name="Zebra snacks", kind="oneoff")
        soon = self.add(name="Candles", kind="oneoff")
        undated = self.add(name="Milk", status="out")
        actions.set_item_need_by(self.db, "ui:avery", later["id"], "2026-09-15")
        actions.set_item_need_by(self.db, "ui:avery", soon["id"], "2026-08-22")
        names = [i["name"] for i in derivations.shopping_list(self.db)]
        # deadlined items first (soonest first), the undated after
        self.assertEqual(["Candles", "Zebra snacks", "Milk"], names)

    def test_snoozed_until_rides_along_on_the_nudge_derivations(self):
        item = self.add(name="Coffee", status="low")
        actions.set_item_snooze(self.db, "ui:avery", item["id"], "2026-09-01")
        row = next(i for i in derivations.shopping_list(self.db)
                   if i["id"] == item["id"])
        # clock-free: the row CARRIES the date; hiding it "now" is the view's
        # call against the client's today.
        self.assertEqual("2026-09-01", row["snoozed_until"])

    # ---------------- #inc3: item_history + the status-derived cadence rung
    def set_at(self, item_id, status, at):
        """A status change pinned to a date: the audit row's `at` (what cycles
        are computed from) and, for a stock event, last_stocked_at (the
        forecast's anchor) — both of which the verb stamps with the real now."""
        actions.set_item_status(self.db, "ui:avery", item_id, status)
        stamp = at + "T12:00:00+00:00"
        self.db.execute(
            "UPDATE audit_log SET at = ? WHERE id = "
            "(SELECT MAX(id) FROM audit_log WHERE target = ?)",
            (stamp, f"item:{item_id}"))
        if status == "stocked":
            self.db.execute("UPDATE items SET last_stocked_at = ? WHERE id = ?",
                            (stamp, item_id))
        self.db.commit()

    def test_item_history_is_the_status_timeline_only(self):
        item = self.add(name="Coffee")                      # add → stocked
        other = self.add(name="Milk")
        actions.set_item_status(self.db, "ui:blake", item["id"], "low")
        actions.set_item_store(self.db, "ui:avery", item["id"], "Costco")
        actions.restock_items(self.db, "ui:avery", [item["id"]])
        events = derivations.item_history(self.db, item["id"])
        self.assertEqual(
            [("add_item", None, "stocked"),
             ("set_item_status", "stocked", "low"),
             ("restock_items", "low", "stocked")],
            [(e["action"], e["before"], e["after"]) for e in events])
        self.assertEqual("ui:blake", events[1]["actor"])
        # the metadata setter never enters the timeline; the whole-map form
        # keys by item and includes the other item's add
        full = derivations.item_history(self.db)
        self.assertEqual(1, len(full[other["id"]]))

    def test_status_cycles_first_departure_only_and_same_day_dropped(self):
        item = self.add(name="Moon dust")
        self.set_at(item["id"], "stocked", "2026-06-01")
        self.set_at(item["id"], "low", "2026-06-11")        # cycle: 10 days
        self.set_at(item["id"], "out", "2026-06-14")        # later departure: no
        self.set_at(item["id"], "stocked", "2026-06-15")
        self.set_at(item["id"], "out", "2026-06-15")        # same-day: dropped
        self.set_at(item["id"], "stocked", "2026-06-16")
        self.set_at(item["id"], "out", "2026-06-28")        # cycle: 12 days
        cycles = derivations._status_cycle_days(
            derivations.item_history(self.db, item["id"]))
        self.assertEqual([10, 12], cycles)

    def test_forecast_status_rung_between_manual_and_purchases(self):
        item = self.add(name="Moon dust")
        # two completed cycles (10 and 12 days) then stocked on the 28th
        self.set_at(item["id"], "stocked", "2026-06-01")
        self.set_at(item["id"], "low", "2026-06-11")
        self.set_at(item["id"], "stocked", "2026-06-16")
        self.set_at(item["id"], "out", "2026-06-28")
        self.set_at(item["id"], "stocked", "2026-06-28")
        f = next(f for f in derivations.restock_forecast(self.db)
                 if f["item_id"] == item["id"])
        self.assertEqual("status", f["interval_source"])
        self.assertEqual(2, f["cycles_seen"])
        self.assertEqual(11, f["interval_days"])            # median of 10, 12
        # anchored at the last stocked event (last_stocked_at)
        self.assertEqual("2026-07-09", f["predicted_date"])
        # a manual interval still outranks the status history
        actions.set_item_interval(self.db, "ui:avery", item["id"], 30)
        f = next(f for f in derivations.restock_forecast(self.db)
                 if f["item_id"] == item["id"])
        self.assertEqual("manual", f["interval_source"])

    def test_forecast_status_rung_needs_two_cycles(self):
        item = self.add(name="Moon dust")
        self.set_at(item["id"], "stocked", "2026-06-01")
        self.set_at(item["id"], "low", "2026-06-11")        # one cycle only
        self.set_at(item["id"], "stocked", "2026-06-12")
        self.assertNotIn(item["id"], [
            f["item_id"] for f in derivations.restock_forecast(self.db)])

    # --------------------------------------------------------- archive_item
    def test_archive_soft_deletes_and_audits(self):
        item = self.add(name="Old thing")
        actions.archive_item(self.db, "ui:avery", item["id"])
        self.assertEqual(0, self.status_of(item["id"])["active"])
        self.assertTrue(self.db.execute(
            "SELECT 1 FROM audit_log WHERE action = 'archive_item' AND target = ?",
            (f"item:{item['id']}",)).fetchone())

    # ----------------------------------------------------------- derivations
    def test_shopping_list_and_low_stock(self):
        stocked = self.add(name="Olive oil")                    # stocked staple
        low = self.add(name="Coffee", status="low")             # low staple
        out = self.add(name="TP", status="out")                 # out staple
        oneoff = self.add(name="Cake", kind="oneoff")           # one-off need

        shop_ids = [i["id"] for i in derivations.shopping_list(self.db)]
        self.assertIn(low["id"], shop_ids)
        self.assertIn(out["id"], shop_ids)
        self.assertIn(oneoff["id"], shop_ids)
        self.assertNotIn(stocked["id"], shop_ids)   # a stocked staple isn't a need
        # most urgent first: 'out' rows precede 'low' rows
        self.assertLess(shop_ids.index(out["id"]), shop_ids.index(low["id"]))

        low_ids = [i["id"] for i in derivations.low_stock(self.db)]
        self.assertEqual({low["id"], out["id"]}, set(low_ids))   # staples only
        self.assertNotIn(oneoff["id"], low_ids)


    # ---- set_item_match (INVENTORY-DESIGN step 5) ----------------------------
    def test_set_item_match_sets_and_clears_with_audit(self):
        item = self.add(name="Coffee")
        row = actions.set_item_match(self.db, "ui:avery", item["id"], "  Blue Bottle  ")
        self.assertEqual("Blue Bottle", row["restock_match"])   # trimmed
        audit = self.db.execute(
            "SELECT actor, action, detail_json FROM audit_log WHERE target = ? "
            "AND action = 'set_item_match'", (f"item:{item['id']}",)).fetchone()
        self.assertEqual("ui:avery", audit["actor"])
        self.assertEqual({"before": None, "after": "Blue Bottle"},
                         json.loads(audit["detail_json"]))
        # blank clears it -> NULL (falls back to the name)
        cleared = actions.set_item_match(self.db, "ui:avery", item["id"], "   ")
        self.assertIsNone(cleared["restock_match"])

    def test_set_item_match_not_found(self):
        with self.assertRaises(actions.NotFound):
            actions.set_item_match(self.db, "ui:avery", 99999, "x")

    def test_add_item_accepts_restock_match(self):
        row = self.add(name="Dog food", restock_match="chewy")
        self.assertEqual("chewy", row["restock_match"])

    # ---- restock_suggestions derivation --------------------------------------
    def _purchase(self, desc, when, direction="out", income_type=None):
        """Raw-insert a transaction (test fixture) with a chosen date/direction —
        the pattern test_income_isolation uses to control the exact shape."""
        self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction, income_type) "
            "VALUES (?, 1299, ?, 'Groceries', 1, 0, 'simplefin', ?, ?)",
            (when, desc, direction, income_type))
        self.db.commit()

    def test_restock_suggestion_matches_by_name_after_it_ran_low(self):
        item = self.add(name="Coffee", status="out")
        day = item["updated_at"][:10]
        self._purchase("BLUE BOTTLE COFFEE", day)   # same day it went out, contains 'coffee'
        sugg = derivations.restock_suggestions(self.db)
        self.assertEqual(1, len(sugg))
        self.assertEqual(item["id"], sugg[0]["item_id"])
        self.assertEqual("name", sugg[0]["matched_by"])
        self.assertEqual("BLUE BOTTLE COFFEE", sugg[0]["purchase"]["description"])
        self.assertEqual(1299, sugg[0]["purchase"]["amount_cents"])

    def test_restock_suggestion_ignores_purchase_before_it_ran_low(self):
        item = self.add(name="Coffee", status="out")
        # anchor to the item's own clock (updated_at), not the wall clock — the
        # stamp is UTC while _purchase dates are naive, so date.today() can land
        # a calendar day off in a negative-offset tz and break "predates".
        yesterday = (date.fromisoformat(item["updated_at"][:10])
                     - timedelta(days=1)).isoformat()
        self._purchase("BLUE BOTTLE COFFEE", yesterday)   # predates going-out
        self.assertEqual([], derivations.restock_suggestions(self.db))

    def test_restock_suggestion_skips_stocked_staples(self):
        item = self.add(name="Coffee")   # stocked
        self._purchase("BLUE BOTTLE COFFEE", item["updated_at"][:10])
        self.assertEqual([], derivations.restock_suggestions(self.db))

    def test_restock_override_phrase_replaces_the_name_guess(self):
        item = self.add(name="Paper towels", status="low", restock_match="costco")
        day = item["updated_at"][:10]
        # A purchase that matches the NAME but not the phrase must NOT suggest.
        self._purchase("TARGET PAPER TOWELS", day)
        self.assertEqual([], derivations.restock_suggestions(self.db))
        # A purchase matching the phrase does — matched_by 'phrase'.
        self._purchase("COSTCO WHOLESALE", day)
        sugg = derivations.restock_suggestions(self.db)
        self.assertEqual(1, len(sugg))
        self.assertEqual("phrase", sugg[0]["matched_by"])
        self.assertEqual("COSTCO WHOLESALE", sugg[0]["purchase"]["description"])

    def test_restock_suggestion_ignores_inflows(self):
        # An inflow whose description contains the item name must never
        # fabricate a suggestion — the derivation's direction='out' filter
        # (tripwire-guarded), proven with a manufactured matching inflow.
        item = self.add(name="Coffee", status="out")
        self._purchase("COFFEE REFUND", item["updated_at"][:10],
                       direction="in", income_type="refund")
        self.assertEqual([], derivations.restock_suggestions(self.db))

    # ---- restock_forecast derivation (step 5, second half) -------------------
    def test_forecast_predicts_from_median_gap(self):
        item = self.add(name="Coffee")  # stocked
        for day in ("2026-01-01", "2026-01-31", "2026-03-02"):  # gaps 30, 30
            self._purchase("BLUE BOTTLE COFFEE", day)
        fc = derivations.restock_forecast(self.db)
        self.assertEqual(1, len(fc))
        f = fc[0]
        self.assertEqual(item["id"], f["item_id"])
        self.assertEqual(3, f["purchases_seen"])
        self.assertEqual(30, f["interval_days"])
        self.assertEqual("2026-03-02", f["last_purchase"])
        self.assertEqual("2026-04-01", f["predicted_date"])  # last + 30 days
        self.assertNotIn("days_until", f)  # clock-free: no 'today' in the derivation

    def test_forecast_needs_at_least_three_purchases(self):
        self.add(name="Coffee")
        for day in ("2026-01-01", "2026-01-31"):  # only 2 → 1 interval
            self._purchase("BLUE BOTTLE COFFEE", day)
        self.assertEqual([], derivations.restock_forecast(self.db))

    def test_forecast_median_is_robust_to_an_outlier(self):
        self.add(name="Coffee")
        # gaps 7, 7, 60 → median 7 (a mean would be skewed to ~25)
        for day in ("2026-01-01", "2026-01-08", "2026-01-15", "2026-03-16"):
            self._purchase("BLUE BOTTLE COFFEE", day)
        self.assertEqual(7, derivations.restock_forecast(self.db)[0]["interval_days"])

    def test_forecast_collapses_same_day_purchases(self):
        self.add(name="Coffee")
        # 3 rows but only 2 distinct DAYS (two bought Jan 1) → below the bar
        self._purchase("BLUE BOTTLE COFFEE", "2026-01-01")
        self._purchase("BLUE BOTTLE COFFEE", "2026-01-01")
        self._purchase("BLUE BOTTLE COFFEE", "2026-01-31")
        self.assertEqual([], derivations.restock_forecast(self.db))

    def test_forecast_even_gap_count_rounds_ties_to_even(self):
        self.add(name="Coffee")
        # gaps 10 and 21 → 15.5 → ties-to-even → 16 (float-free round_ratio)
        for day in ("2026-01-01", "2026-01-11", "2026-02-01"):
            self._purchase("BLUE BOTTLE COFFEE", day)
        self.assertEqual(16, derivations.restock_forecast(self.db)[0]["interval_days"])

    def test_forecast_ignores_inflows(self):
        # A matching INFLOW must not count toward the cadence: 2 outflows + a
        # matching inflow = 2 real purchases → still below the 3-buy bar, so no
        # forecast. Proves the outflows-only filter (tripwire-guarded too).
        self.add(name="Coffee")
        self._purchase("BLUE BOTTLE COFFEE", "2026-01-01")
        self._purchase("BLUE BOTTLE COFFEE", "2026-01-31")
        self._purchase("COFFEE REFUND", "2026-03-02", direction="in",
                       income_type="refund")
        self.assertEqual([], derivations.restock_forecast(self.db))

    def test_forecast_sorted_soonest_due_first(self):
        # Unique restock_match tokens so the seed's own transactions can't
        # collide with the item names (forecast reads ALL matching outflows,
        # unlike restock_suggestions' since-filter).
        later = self.add(name="Milk", restock_match="zzmilkzz")
        sooner = self.add(name="Soap", restock_match="zzsoapzz")
        for day in ("2026-02-01", "2026-02-08", "2026-02-15"):  # weekly, recent → predicted later
            self._purchase("ZZMILKZZ", day)
        for day in ("2026-01-01", "2026-01-08", "2026-01-15"):  # weekly, older → predicted sooner
            self._purchase("ZZSOAPZZ", day)
        fc = derivations.restock_forecast(self.db)
        self.assertEqual([sooner["id"], later["id"]], [f["item_id"] for f in fc])

    def test_forecast_cadence_source_is_labelled(self):
        self.add(name="Coffee")
        for day in ("2026-01-01", "2026-01-31", "2026-03-02"):
            self._purchase("BLUE BOTTLE COFFEE", day)
        self.assertEqual("cadence", derivations.restock_forecast(self.db)[0]["interval_source"])

    # ---- set_item_interval (INVENTORY-DESIGN step 5, user-set cadence) --------
    def test_set_item_interval_sets_and_clears_with_audit(self):
        item = self.add(name="Coffee")
        row = actions.set_item_interval(self.db, "ui:avery", item["id"], 14)
        self.assertEqual(14, row["restock_interval_days"])
        audit = self.db.execute(
            "SELECT actor, action, detail_json FROM audit_log WHERE target = ? "
            "AND action = 'set_item_interval'", (f"item:{item['id']}",)).fetchone()
        self.assertEqual("ui:avery", audit["actor"])
        self.assertEqual({"before": None, "after": 14}, json.loads(audit["detail_json"]))
        cleared = actions.set_item_interval(self.db, "ui:avery", item["id"], None)
        self.assertIsNone(cleared["restock_interval_days"])

    def test_set_item_interval_accepts_string_digits_and_rejects_bad_values(self):
        item = self.add(name="Coffee")
        # the route hands through JSON values; a numeric string coerces
        self.assertEqual(21, actions.set_item_interval(
            self.db, "ui:avery", item["id"], "21")["restock_interval_days"])
        for bad in (0, -3, 366, "soon"):
            with self.assertRaises(actions.ActionError):
                actions.set_item_interval(self.db, "ui:avery", item["id"], bad)
        # empty string clears, like a blank match
        self.assertIsNone(actions.set_item_interval(
            self.db, "ui:avery", item["id"], "")["restock_interval_days"])

    def test_set_item_interval_rejects_a_oneoff(self):
        oneoff = self.add(name="Birthday candles", kind="oneoff")
        with self.assertRaisesRegex(actions.ActionError, "cadence only applies to a staple"):
            actions.set_item_interval(self.db, "ui:avery", oneoff["id"], 7)

    def test_set_item_interval_not_found(self):
        with self.assertRaises(actions.NotFound):
            actions.set_item_interval(self.db, "ui:avery", 99999, 7)

    def test_add_stocked_seeds_last_stocked_at_and_lowout_does_not(self):
        staple = self.add(name="Coffee")                       # defaults stocked
        self.assertIsNotNone(staple["last_stocked_at"])
        need = self.add(name="Napkins", status="out")          # a need, not full
        self.assertIsNone(need["last_stocked_at"])

    def test_marking_stocked_reanchors_last_stocked_at(self):
        item = self.add(name="Coffee")
        low = actions.set_item_status(self.db, "ui:avery", item["id"], "low")
        anchored = low["last_stocked_at"]                       # unchanged by low
        restocked = actions.set_item_status(self.db, "ui:avery", item["id"], "stocked")
        self.assertGreaterEqual(restocked["last_stocked_at"], anchored)

    def test_manual_interval_forecasts_from_last_stocked_without_purchases(self):
        # No purchase history at all — the manual cadence still predicts, from
        # the anchor set when the staple was added stocked.
        item = self.add(name="Coffee")
        actions.set_item_interval(self.db, "ui:avery", item["id"], 30)
        f = derivations.restock_forecast(self.db)[0]
        self.assertEqual(item["id"], f["item_id"])
        self.assertEqual(30, f["interval_days"])
        self.assertEqual("manual", f["interval_source"])
        anchor = date.fromisoformat(item["last_stocked_at"][:10])
        self.assertEqual((anchor + timedelta(days=30)).isoformat(), f["predicted_date"])
        self.assertIsNone(f["last_purchase"])
        self.assertNotIn("days_until", f)     # still clock-free

    def test_manual_interval_overrides_the_inferred_cadence(self):
        item = self.add(name="Coffee")
        for day in ("2026-01-01", "2026-01-31", "2026-03-02"):  # median gap 30
            self._purchase("BLUE BOTTLE COFFEE", day)
        actions.set_item_interval(self.db, "ui:avery", item["id"], 7)
        f = derivations.restock_forecast(self.db)[0]
        self.assertEqual(7, f["interval_days"])                # manual, not 30
        self.assertEqual("manual", f["interval_source"])

    # ---- new_staple_suggestions derivation (step 5 sibling) ------------------
    # NOTE: this derivation scans ALL outflows, so the seed contributes its own
    # merchant clusters — these assertions target the merchants they inject and
    # never assume the seed produces none.
    def _outflow(self, desc, when, amt=1500, source="simplefin"):
        self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction) "
            "VALUES (?, ?, ?, 'Pets', 1, 0, ?, 'out')", (when, amt, desc, source))
        self.db.commit()

    def _find(self, sugg, merchant):
        return next((s for s in sugg if s["merchant"] == merchant), None)

    def test_new_staple_suggested_after_three_distinct_days(self):
        # A merchant bought on 3 distinct days, not tracked, not a subscription
        # (varied amounts) → offered as a new staple.
        for day, amt in (("2026-01-01", 1500), ("2026-01-20", 1799),
                         ("2026-02-10", 1299)):
            self._outflow("CHEWY.COM* NJ", day, amt)
        s = self._find(derivations.new_staple_suggestions(self.db), "Chewy")
        self.assertIsNotNone(s)
        self.assertEqual(3, s["purchases_seen"])
        self.assertEqual("2026-01-01", s["first_purchase"])
        self.assertEqual("2026-02-10", s["last_purchase"])
        self.assertEqual(1500 + 1799 + 1299, s["total_spent_cents"])
        self.assertEqual("chewy", s["suggested_match"])
        self.assertEqual("CHEWY.COM* NJ", s["example_description"])

    def test_new_staple_needs_three_distinct_days(self):
        self._outflow("CHEWY.COM* NJ", "2026-01-01")
        self._outflow("CHEWY.COM* NJ", "2026-01-01")   # same day → one restock
        self._outflow("CHEWY.COM* NJ", "2026-01-20")   # 3 rows, 2 distinct days
        self.assertIsNone(self._find(
            derivations.new_staple_suggestions(self.db), "Chewy"))

    def test_new_staple_excludes_already_tracked_merchant(self):
        # Track it as a staple (restock_match 'chewy'); it must NOT be re-offered.
        # Varied amounts so recurring_charges won't flag it — isolating the
        # already-tracked exclusion as the reason it's absent.
        self.add(name="Dog food", restock_match="chewy")
        for day, amt in (("2026-01-01", 1500), ("2026-01-20", 1799),
                         ("2026-02-10", 1299)):
            self._outflow("CHEWY.COM* NJ", day, amt)
        self.assertIsNone(self._find(
            derivations.new_staple_suggestions(self.db), "Chewy"))

    def test_new_staple_excludes_fixed_amount_subscription(self):
        # Identical amount on a regular monthly cadence → recurring_charges flags
        # it a subscription, so it's NOT offered as a pantry staple.
        for day in ("2026-01-05", "2026-02-05", "2026-03-05"):
            self._outflow("NETFLIX.COM", day, 1599)
        self.assertIn("Netflix",
                      [c["merchant"] for c in derivations.recurring_charges(self.db)])
        self.assertIsNone(self._find(
            derivations.new_staple_suggestions(self.db), "Netflix"))

    def test_new_staple_excludes_settlements(self):
        for day in ("2026-01-01", "2026-01-20", "2026-02-10"):
            self._outflow("SETTLE UP AVERY", day, source="settlement")
        self.assertIsNone(self._find(
            derivations.new_staple_suggestions(self.db), "Settle Up Avery"))

    def test_new_staple_ignores_inflows(self):
        # An inflow that names a merchant on 3 days must never fabricate a
        # suggestion — the outflows-only filter (tripwire-guarded too).
        for day in ("2026-01-01", "2026-01-20", "2026-02-10"):
            self._purchase("CHEWY REFUND", day, direction="in", income_type="refund")
        sugg = derivations.new_staple_suggestions(self.db)
        self.assertTrue(all("CHEWY" not in s["merchant"].upper() for s in sugg))

    def test_new_staple_sorted_most_bought_first(self):
        # Varied amounts so neither reads as a fixed-amount subscription.
        for day, amt in (("2026-01-01", 850), ("2026-01-08", 900),
                         ("2026-01-15", 875), ("2026-01-22", 950)):  # 4×
            self._outflow("BLUE BOTTLE COFFEE", day, amt)
        for day, amt in (("2026-02-01", 1400), ("2026-02-10", 1650),
                         ("2026-02-20", 1299)):  # 3×
            self._outflow("PETCO", day, amt)
        merchants = [s["merchant"] for s in derivations.new_staple_suggestions(self.db)]
        mine = [m for m in merchants if m in ("Blue Bottle Coffee", "Petco")]
        self.assertEqual(["Blue Bottle Coffee", "Petco"], mine)  # 4× before 3×

    # ---- unmatched_staples derivation (broken-match detector) ----------------
    def _unmatched(self, name):
        return [u for u in derivations.unmatched_staples(self.db) if u["name"] == name]

    def test_unmatched_surfaced_when_no_purchase_ever_matches(self):
        item = self.add(name="Dish soap")
        u = self._unmatched("Dish soap")
        self.assertEqual(1, len(u))
        self.assertEqual(item["id"], u[0]["item_id"])
        self.assertEqual("name", u[0]["matched_by"])         # no phrase → the name
        self.assertEqual(item["created_at"][:10], u[0]["tracked_since"])

    def test_unmatched_excludes_a_staple_with_a_matching_purchase(self):
        self.add(name="Coffee")
        self._purchase("BLUE BOTTLE COFFEE", "2026-05-01")   # contains 'coffee'
        self.assertEqual([], self._unmatched("Coffee"))

    def test_unmatched_uses_the_override_phrase_not_the_name(self):
        # Matched on the phrase 'costco': a purchase matching the NAME but not the
        # phrase leaves it unmatched; one matching the phrase clears it.
        self.add(name="Paper towels", restock_match="costco")
        self._purchase("TARGET PAPER TOWELS", "2026-05-01")
        u = self._unmatched("Paper towels")
        self.assertEqual(1, len(u))
        self.assertEqual("phrase", u[0]["matched_by"])
        self.assertEqual("costco", u[0]["restock_match"])
        self._purchase("COSTCO WHOLESALE", "2026-05-02")
        self.assertEqual([], self._unmatched("Paper towels"))

    def test_unmatched_ignores_inflows(self):
        # An inflow whose description contains the name must NOT count as a match,
        # so the staple stays surfaced — the direction='out' filter in
        # _matching_purchases (tripwire-guarded), proven with a matching inflow.
        self.add(name="Coffee")
        self._purchase("COFFEE REFUND", "2026-05-01",
                       direction="in", income_type="refund")
        self.assertEqual(1, len(self._unmatched("Coffee")))

    def test_unmatched_is_staples_only_not_oneoffs(self):
        self.add(name="Birthday candles", kind="oneoff")   # never a staple
        self.assertEqual([], self._unmatched("Birthday candles"))

    # ---- stale_shopping_items derivation (list-rot detector) -----------------
    def _stale(self, name):
        return [s for s in derivations.stale_shopping_items(self.db)
                if s["name"] == name]

    def test_stale_surfaces_low_or_out_with_no_purchase_since(self):
        item = self.add(name="Dish soap", status="out")
        s = self._stale("Dish soap")
        self.assertEqual(1, len(s))
        self.assertEqual("out", s[0]["status"])
        self.assertEqual(item["updated_at"][:10], s[0]["low_since"])

    def test_stale_excludes_a_staple_bought_since_it_ran_low(self):
        item = self.add(name="Coffee", status="out")
        # A matching purchase on/after it ran low → a restock, not rot.
        self._purchase("BLUE BOTTLE COFFEE", item["updated_at"][:10])
        self.assertEqual([], self._stale("Coffee"))

    def test_stale_excludes_stocked_staples(self):
        self.add(name="Rice")   # stocked → not on the list at all
        self.assertEqual([], self._stale("Rice"))

    def test_stale_ignores_inflows(self):
        # An inflow naming the item is NOT the missing restock — the item stays
        # surfaced (the direction='out' filter in _matching_purchases).
        item = self.add(name="Coffee", status="out")
        self._purchase("COFFEE REFUND", item["updated_at"][:10],
                       direction="in", income_type="refund")
        self.assertEqual(1, len(self._stale("Coffee")))

    # ---- staple_spend derivation (money tie-in) ------------------------------
    def _spend(self, name):
        return [x for x in derivations.staple_spend(self.db) if x["name"] == name]

    def test_staple_spend_monthly_rate_from_matching_purchases(self):
        self.add(name="Coffee")
        for day, amt in (("2026-01-10", 1000), ("2026-02-10", 2000),
                         ("2026-03-10", 3000)):
            self._outflow("BLUE BOTTLE COFFEE", day, amt)   # contains 'coffee'
        s = self._spend("Coffee")
        self.assertEqual(1, len(s))
        self.assertEqual(6000, s[0]["total_cents"])
        self.assertEqual(3, s[0]["months_spanned"])       # Jan→Mar inclusive
        self.assertEqual(2000, s[0]["monthly_cents"])     # 6000 / 3
        self.assertEqual(3, s[0]["purchases_seen"])

    def test_staple_spend_needs_three_distinct_days(self):
        self.add(name="Rice")
        for day in ("2026-01-01", "2026-02-01"):   # only 2 → below the bar
            self._outflow("RICE", day, 500)
        self.assertEqual([], self._spend("Rice"))

    def test_staple_spend_ignores_inflows(self):
        # An inflow naming the staple must NOT inflate its cost (outflows only).
        self.add(name="Coffee")
        for day in ("2026-01-10", "2026-02-10", "2026-03-10"):
            self._outflow("BLUE BOTTLE COFFEE", day, 1000)
        self._purchase("COFFEE REFUND", "2026-03-15",
                       direction="in", income_type="refund")
        self.assertEqual(3000, self._spend("Coffee")[0]["total_cents"])

    # ---- inc 4: price trend + list_estimate ---------------------------------
    def test_price_trend_recent_median_vs_earlier_in_basis_points(self):
        self.add(name="Coffee")
        # earlier restocks 1000,1000,1200 (median 1000); recent 1300,1150,1200
        # (median 1200) → +20% = +2000 bp
        for day, amt in (("2026-01-05", 1000), ("2026-02-05", 1000),
                         ("2026-03-05", 1200), ("2026-04-05", 1300),
                         ("2026-05-05", 1150), ("2026-06-05", 1200)):
            self._outflow("BLUE BOTTLE COFFEE", day, amt)
        s = self._spend("Coffee")[0]
        self.assertEqual((1200, 1000, 2000),
                         (s["recent_cents"], s["earlier_cents"], s["change_bp"]))

    def test_price_trend_needs_an_earlier_window_and_collapses_same_day(self):
        self.add(name="Coffee")
        for day in ("2026-01-05", "2026-02-05", "2026-03-05"):   # 3 days only
            self._outflow("BLUE BOTTLE COFFEE", day, 1000)
        self.assertIsNone(self._spend("Coffee")[0]["change_bp"])
        # a 4th day, bought twice → one restock of 2000 in the recent window
        self._outflow("BLUE BOTTLE COFFEE", "2026-04-05", 1000)
        self._outflow("BLUE BOTTLE COFFEE", "2026-04-05", 1000)
        s = self._spend("Coffee")[0]
        self.assertEqual(1000, s["earlier_cents"])               # the Jan buy
        self.assertEqual(1000, s["recent_cents"])                # median(1000,1000,2000)
        self.assertEqual(0, s["change_bp"])

    def test_list_estimate_prices_from_median_restock_and_is_honest_about_coverage(self):
        self.add(name="Coffee", status="low")
        for day, amt in (("2026-01-05", 1000), ("2026-02-05", 5000),   # bulk buy
                         ("2026-03-05", 1200)):
            self._outflow("BLUE BOTTLE COFFEE", day, amt)
        self.add(name="Party hats", kind="oneoff")               # no history
        est = derivations.list_estimate(self.db)
        by = {l["name"]: l for l in est["lines"]}
        self.assertEqual(1200, by["Coffee"]["typical_cents"])    # median, not mean
        self.assertTrue(by["Coffee"]["priced"])
        self.assertFalse(by["Party hats"]["priced"])
        self.assertIsNone(by["Party hats"]["typical_cents"])
        self.assertEqual((1200, 1, 1), (est["total_cents"], est["priced_count"],
                                        est["unpriced_count"]))
        self.assertEqual("2026-03-05", by["Coffee"]["last_purchase"])

    def test_list_estimate_ignores_inflows(self):
        self.add(name="Coffee", status="out")
        self._outflow("BLUE BOTTLE COFFEE", "2026-03-05", 1200)
        self._purchase("COFFEE REFUND", "2026-03-15",
                       direction="in", income_type="refund")
        est = derivations.list_estimate(self.db)
        coffee = next(l for l in est["lines"] if l["name"] == "Coffee")
        self.assertEqual((1200, 1), (coffee["typical_cents"], coffee["purchases_seen"]))

    # ---- inc 5: trip_plan + trip_closure ------------------------------------
    def test_trip_plan_composes_list_with_due_stocked_staples(self):
        on_list = self.add(name="Milk", status="out")
        coming = self.add(name="Moon dust")             # stocked, manual cadence
        actions.set_item_store(self.db, "ui:avery", coming["id"], "Costco")
        actions.set_item_interval(self.db, "ui:avery", coming["id"], 10)
        for day, amt in (("2026-05-01", 900), ("2026-06-01", 1100)):
            self._outflow("MOON DUST CO", day, amt)
        plan = derivations.trip_plan(self.db)
        self.assertIn(on_list["id"], [l["item_id"] for l in plan["list"]])
        due = {d["name"]: d for d in plan["due_soon"]}
        self.assertIn("Moon dust", due)
        self.assertNotIn("Milk", due)                     # listed items aren't "also"
        d = due["Moon dust"]
        self.assertEqual(("Costco", "manual", 1000),
                         (d["store"], d["interval_source"], d["typical_cents"]))
        self.assertEqual(coming["last_stocked_at"][:10] and
                         (date.fromisoformat(coming["last_stocked_at"][:10])
                          + timedelta(days=10)).isoformat(), d["predicted_date"])
        self.assertEqual(plan["list_total_cents"],
                         derivations.list_estimate(self.db)["total_cents"])

    def test_trip_closure_groups_two_plus_hints_by_purchase(self):
        a = self.add(name="Coffee", status="out")
        b = self.add(name="Milk", status="low")
        self.add(name="Eggs", status="low")            # no matching purchase
        lone = self.add(name="Rice", status="out")
        # anchor the trip to the items' own clock (updated_at) so "since it ran
        # low" matches — the stamp is UTC while _outflow dates are naive, so a
        # wall-clock date.today() can fall a day short in a negative-offset tz.
        today = a["updated_at"][:10]
        # one supermarket visit whose description names two staples
        self._outflow("FRESH MART COFFEE MILK", today, 4200)
        self._outflow("RICE BARN", today, 800)          # a lone hint
        groups = derivations.trip_closure(self.db)
        self.assertEqual(1, len(groups))
        g = groups[0]
        self.assertEqual({a["id"], b["id"]}, set(g["item_ids"]))
        self.assertEqual(("FRESH MART COFFEE MILK", 4200),
                         (g["purchase"]["description"], g["purchase"]["amount_cents"]))
        # the lone hint is still a per-item suggestion, not a trip
        self.assertIn(lone["id"],
                      [s["item_id"] for s in derivations.restock_suggestions(self.db)])

    def test_trip_closure_ignores_inflows(self):
        coffee = self.add(name="Coffee", status="out")
        self.add(name="Milk", status="low")
        # dated on the items' own clock so the row is in-window — proving it's
        # excluded as an inflow, not merely because a wall-clock date fell short.
        self._purchase("REFUND COFFEE MILK", coffee["updated_at"][:10],
                       direction="in", income_type="refund")
        self.assertEqual([], derivations.trip_closure(self.db))

    # ---- inc 6: stale_staples + pantry_pulse --------------------------------
    def test_stale_staples_last_activity_is_the_latest_sign_of_life(self):
        quiet = self.add(name="Moon dust")                  # stocked, add only
        touched = self.add(name="Star salt")
        bought = self.add(name="Comet chips")
        low = self.add(name="Milk", status="low")           # not stocked → excluded
        self.set_at(touched["id"], "stocked", "2026-06-01")  # a status event
        self._outflow("COMET CHIPS CO", "2026-07-04", 500)   # a matched purchase
        by = {s["name"]: s for s in derivations.stale_staples(self.db)}
        self.assertNotIn("Milk", by)
        self.assertEqual(quiet["created_at"][:10], by["Moon dust"]["last_activity"])
        self.assertIsNone(by["Moon dust"]["last_status_change"])
        self.assertEqual("2026-06-01", by["Star salt"]["last_status_change"])
        self.assertEqual("2026-07-04", by["Comet chips"]["last_purchase"])
        # last_activity is the max of the three signals
        self.assertEqual(max(bought["created_at"][:10], "2026-07-04"),
                         by["Comet chips"]["last_activity"])

    def test_pantry_pulse_composes_the_named_derivations(self):
        self.add(name="Milk", status="out")
        self.add(name="Moon dust")
        pulse = derivations.pantry_pulse(self.db)
        self.assertEqual(1, pulse["list_count"])
        self.assertEqual(derivations.list_estimate(self.db)["total_cents"],
                         pulse["list_total_cents"])
        self.assertEqual([s["item_id"] for s in derivations.stale_staples(self.db)],
                         [s["item_id"] for s in pulse["stale_staples"]])
        self.assertEqual(len(derivations.unmatched_staples(self.db)),
                         pulse["unmatched_count"])
        self.assertIn("new_staple_suggestion", pulse)

    # ---- #015: the 'ordered' status -----------------------------------------
    def test_ordered_leaves_the_list_without_being_stocked(self):
        staple = self.add(name="Dog food", status="out")
        need = self.add(name="Party hats", kind="oneoff")
        for it in (staple, need):
            row = actions.set_item_status(self.db, "ui:avery", it["id"], "ordered")
            self.assertEqual(("ordered", 1), (row["status"], row["active"]))
            self.assertIsNone(row["last_stocked_at"])       # not a stock event
        listed = {i["id"] for i in derivations.shopping_list(self.db)}
        self.assertNotIn(staple["id"], listed)
        self.assertNotIn(need["id"], listed)
        otw = [i["id"] for i in derivations.on_the_way(self.db)]
        self.assertEqual({staple["id"], need["id"]}, set(otw))
        self.assertEqual(0, len([i for i in derivations.low_stock(self.db)
                                 if i["id"] == staple["id"]]))   # handled, not "low"
        self.assertEqual(2, len(derivations.pantry_pulse(self.db)["on_the_way"]))

    def test_arrived_stocks_and_a_oneoff_archives_as_bought(self):
        need = self.add(name="Party hats", kind="oneoff")
        actions.set_item_status(self.db, "ui:avery", need["id"], "ordered")
        row = actions.set_item_status(self.db, "ui:avery", need["id"], "stocked")
        self.assertEqual(0, row["active"])                  # bought → gone
        staple = self.add(name="Dog food", status="out")
        actions.set_item_status(self.db, "ui:avery", staple["id"], "ordered")
        row = actions.set_item_status(self.db, "ui:avery", staple["id"], "out")  # didn't come
        self.assertEqual("out", row["status"])
        self.assertIn(staple["id"], [i["id"] for i in derivations.shopping_list(self.db)])

    def test_reorder_from_stocked_ends_a_consumption_cycle(self):
        item = self.add(name="Moon dust")
        self.set_at(item["id"], "stocked", "2026-06-01")
        self.set_at(item["id"], "ordered", "2026-06-11")    # re-ordered: running down
        self.set_at(item["id"], "stocked", "2026-06-15")
        self.set_at(item["id"], "low", "2026-06-27")
        self.assertEqual([10, 12], derivations._status_cycle_days(
            derivations.item_history(self.db, item["id"])))

    # ---- last_shopping_trip derivation (post-shopping review nudge) ----------
    def test_last_shopping_trip_returns_most_recent_grocery_outflow(self):
        # _purchase inserts a Groceries outflow; dated later than the seed's.
        self._purchase("FRESH MART", "2026-08-04")
        trip = derivations.last_shopping_trip(self.db)
        self.assertEqual("2026-08-04", trip["date"])
        self.assertEqual("FRESH MART", trip["merchant"])
        self.assertEqual("Groceries", trip["category"])

    def test_last_shopping_trip_ignores_inflows(self):
        self._purchase("FRESH MART", "2026-08-04")                 # outflow
        self._purchase("GROCERY REFUND", "2026-08-10",             # newer, inflow
                       direction="in", income_type="refund")
        self.assertEqual("FRESH MART",
                         derivations.last_shopping_trip(self.db)["merchant"])

    def test_last_shopping_trip_ignores_settlements(self):
        self._purchase("FRESH MART", "2026-08-04")
        # A newer settlement in a shopping category must not become "the trip".
        self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction) VALUES "
            "('2026-08-11', 5000, 'Settle Up', 'Groceries', 1, 0, 'settlement', 'out')")
        self.db.commit()
        self.assertEqual("FRESH MART",
                         derivations.last_shopping_trip(self.db)["merchant"])

    def test_matching_purchases_index_path_equals_sql_path(self):
        # #16: the prefetched-index path must return byte-identical rows to the
        # per-item SQL path — same substring filter, same `since` bound, same
        # DESC order — so the pantry N+1 optimization can't silently diverge.
        item = self.add(name="Tripwire Beans", kind="staple")
        for d in ("2026-07-05", "2026-07-20", "2026-07-12"):  # out of order on purpose
            self._purchase("TRIPWIRE BEANS CO", d)
        self._purchase("TRIPWIRE BEANS refund", "2026-07-15", direction="in",
                       income_type="refund")  # a matching INFLOW both paths ignore
        index = derivations._purchase_index(self.db)

        sql = [dict(r) for r in derivations._matching_purchases(self.db, item)]
        idx = [dict(r) for r in derivations._matching_purchases(self.db, item, index=index)]
        self.assertEqual(sql, idx)
        self.assertEqual(3, len(sql))  # 3 outflows, the inflow excluded by both

        sql_since = [dict(r) for r in
                     derivations._matching_purchases(self.db, item, since="2026-07-12")]
        idx_since = [dict(r) for r in
                     derivations._matching_purchases(self.db, item, since="2026-07-12",
                                                     index=index)]
        self.assertEqual(sql_since, idx_since)
        self.assertEqual(2, len(sql_since))  # 07-12 and 07-20 only


if __name__ == "__main__":
    unittest.main()
