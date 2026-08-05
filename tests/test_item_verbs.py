"""Inventory verbs + derivations (INVENTORY-DESIGN, the pantry): add_item,
set_item_status (incl. the one-off auto-archive on 'bought'), archive_item,
and the shopping_list / low_stock reads. Household-scoped, three-state, no
money touched."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
sys.path.insert(0, str(REPO))

import actions
import derivations


class ItemVerbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-items-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "51", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
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
        yesterday = (date.today() - timedelta(days=1)).isoformat()
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


if __name__ == "__main__":
    unittest.main()
