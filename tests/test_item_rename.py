"""rename_item (the Staples drop-down's ✎): validation (blank, non-text,
twins of other active items, missing/archived items), the audit row, and the
promise that a rename keeps every link to the item intact — receipt mapping
memory, status/store/cadence, and the shopping list — because the row keeps
its id. Pantry only; never money."""
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

ACTOR = "ui:avery"


class RenameItemVerbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-rename-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=71, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add(self, **data):
        return actions.add_item(self.db, ACTOR, data)

    def audits(self, item_id):
        return self.db.execute(
            "SELECT actor, action, detail_json FROM audit_log "
            "WHERE target = ? AND action = 'rename_item' ORDER BY id",
            (f"item:{item_id}",)).fetchall()

    def test_rename_changes_name_trims_and_audits_before_after(self):
        item = self.add(name="Mlik")
        row = actions.rename_item(self.db, ACTOR, item["id"], "  Milk  ")
        self.assertEqual("Milk", row["name"])
        self.assertEqual(item["id"], row["id"])
        audits = self.audits(item["id"])
        self.assertEqual(1, len(audits))
        self.assertEqual((ACTOR, "rename_item"), (audits[0]["actor"], audits[0]["action"]))
        self.assertEqual({"before": "Mlik", "after": "Milk"},
                         json.loads(audits[0]["detail_json"]))

    def test_rename_keeps_status_store_cadence_and_updated_at(self):
        item = self.add(name="Dog food", status="low")
        actions.set_item_store(self.db, ACTOR, item["id"], "Costco")
        actions.set_item_interval(self.db, ACTOR, item["id"], 14)
        before = dict(self.db.execute(
            "SELECT * FROM items WHERE id = ?", (item["id"],)).fetchone())
        row = dict(actions.rename_item(self.db, ACTOR, item["id"], "Kibble"))
        # Only the name moved. updated_at is restock inference's since-bound;
        # a rename is not a stock event, so it must not shift.
        before["name"] = "Kibble"
        self.assertEqual(before, row)

    def test_blank_and_non_text_names_are_refused(self):
        item = self.add(name="Rice")
        for bad in ("", "   ", None):
            with self.assertRaisesRegex(actions.ActionError, "can't be empty"):
                actions.rename_item(self.db, ACTOR, item["id"], bad)
        with self.assertRaisesRegex(actions.ActionError, "must be text"):
            actions.rename_item(self.db, ACTOR, item["id"], 42)
        self.assertEqual("Rice", self.db.execute(
            "SELECT name FROM items WHERE id = ?", (item["id"],)).fetchone()["name"])
        self.assertEqual([], self.audits(item["id"]))

    def test_twin_of_another_active_item_is_refused_ignoring_case(self):
        self.add(name="Coffee")
        tea = self.add(name="Tea")
        for twin in ("Coffee", "coffee", " COFFEE "):
            with self.assertRaisesRegex(actions.ActionError,
                                        "already have an item called Coffee"):
                actions.rename_item(self.db, ACTOR, tea["id"], twin)
        # a one-off on the shopping list counts too (same items table)
        self.add(name="Candles", kind="oneoff")
        with self.assertRaisesRegex(actions.ActionError, "already have"):
            actions.rename_item(self.db, ACTOR, tea["id"], "candles")

    def test_archived_items_do_not_block_a_name(self):
        old = self.add(name="Oat milk")
        actions.archive_item(self.db, ACTOR, old["id"])
        milk = self.add(name="Milk")
        row = actions.rename_item(self.db, ACTOR, milk["id"], "Oat milk")
        self.assertEqual("Oat milk", row["name"])

    def test_case_only_change_to_own_name_is_allowed(self):
        item = self.add(name="paper towels")
        row = actions.rename_item(self.db, ACTOR, item["id"], "Paper towels")
        self.assertEqual("Paper towels", row["name"])
        self.assertEqual(1, len(self.audits(item["id"])))

    def test_same_name_is_a_no_op_without_an_audit_row(self):
        item = self.add(name="Eggs")
        row = actions.rename_item(self.db, ACTOR, item["id"], " Eggs ")
        self.assertEqual("Eggs", row["name"])
        self.assertEqual([], self.audits(item["id"]))

    def test_long_names_are_cut_to_the_add_item_cap(self):
        item = self.add(name="Soap")
        row = actions.rename_item(self.db, ACTOR, item["id"], "x" * 150)
        self.assertEqual(actions.ITEM_NAME_MAX, len(row["name"]))

    def test_missing_or_archived_item_is_not_found(self):
        with self.assertRaises(actions.NotFound):
            actions.rename_item(self.db, ACTOR, 999999, "Milk")
        gone = self.add(name="Bread")
        actions.archive_item(self.db, ACTOR, gone["id"])
        with self.assertRaises(actions.NotFound):
            actions.rename_item(self.db, ACTOR, gone["id"], "Sourdough")

    def test_receipt_mapping_memory_follows_the_rename(self):
        # A confirmed receipt line remembers the ITEM (by id), so the next
        # review sheet at that store suggests the renamed item for the same
        # printed text.
        milk = self.add(name="Milk")
        actions.record_receipt(self.db, ACTOR, {
            "store": "ShopRite", "purchased_on": "2026-07-18", "total_cents": 399,
            "lines": [{"raw_text": "GV 2% MLK GAL", "price_cents": 399,
                       "action": "item", "item_id": milk["id"]}]})
        actions.rename_item(self.db, ACTOR, milk["id"], "2% milk")
        [pick] = derivations.receipt_line_suggestions(
            self.db, store="ShopRite",
            lines=[{"raw_text": "GV 2% MLK GAL", "name": "Milk",
                    "kind": "product", "item_hint": None}])
        self.assertEqual({"action": "item", "item_id": milk["id"],
                          "name": "2% milk", "source": "remembered"}, pick)

    def test_shopping_list_shows_the_new_name(self):
        item = self.add(name="TP", status="out")
        actions.rename_item(self.db, ACTOR, item["id"], "Toilet paper")
        names = {r["name"] for r in derivations.shopping_list(self.db)}
        self.assertIn("Toilet paper", names)
        self.assertNotIn("TP", names)


class RenameItemRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-rename-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=72, months=1)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "rename-route-secret")
        spec = importlib.util.spec_from_file_location("app_rename_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True
        self.c = self.app_module.app.test_client()
        with self.c.session_transaction() as s:
            s["user_id"] = 1

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_name_renames_and_refusals_are_400_or_404(self):
        a = self.c.post("/api/inventory", json={"name": "Cofee"}).get_json()
        self.c.post("/api/inventory", json={"name": "Tea"})
        r = self.c.put(f"/api/inventory/{a['id']}", json={"name": "Coffee"})
        self.assertEqual(200, r.status_code)
        self.assertEqual("Coffee", r.get_json()["name"])
        names = {i["name"] for i in self.c.get("/api/inventory").get_json()["items"]}
        self.assertIn("Coffee", names)
        self.assertNotIn("Cofee", names)

        blank = self.c.put(f"/api/inventory/{a['id']}", json={"name": "  "})
        self.assertEqual(400, blank.status_code)
        self.assertIn("can't be empty", blank.get_json()["error"])
        twin = self.c.put(f"/api/inventory/{a['id']}", json={"name": "tea"})
        self.assertEqual(400, twin.status_code)
        self.assertIn("already have an item called Tea", twin.get_json()["error"])
        self.assertEqual(404, self.c.put("/api/inventory/999999",
                                         json={"name": "X"}).status_code)


if __name__ == "__main__":
    unittest.main()
