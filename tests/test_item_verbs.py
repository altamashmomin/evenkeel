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


if __name__ == "__main__":
    unittest.main()
