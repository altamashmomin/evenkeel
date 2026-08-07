"""Inventory routes: thin callers over add_item / set_item_status /
archive_item, the composed GET view, and bearer write-scope gating."""
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"


class InventoryRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-inv-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "61", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "inv-route-secret")
        spec = importlib.util.spec_from_file_location("app_inv_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def client(self):
        c = self.app_module.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = 1
        return c

    def test_add_list_update_delete(self):
        c = self.client()
        # add a staple
        created = c.post("/api/inventory", json={"name": "Coffee", "category": "Groceries"})
        self.assertEqual(201, created.status_code)
        item = created.get_json()
        self.assertEqual(
            ["category", "id", "kind", "last_stocked_at", "name", "note",
             "restock_interval_days", "restock_match", "status", "updated_at"],
            sorted(item))
        self.assertEqual("stocked", item["status"])

        # add a one-off need + a low staple, then read the composed view
        c.post("/api/inventory", json={"name": "Cake", "kind": "oneoff"})
        c.put(f"/api/inventory/{item['id']}", json={"status": "low"})
        view = c.get("/api/inventory").get_json()
        self.assertEqual(
            {"items", "shopping", "low_count", "restock_suggestions",
             "restock_forecast", "new_staple_suggestions", "unmatched_staples",
             "stale_shopping_items", "staple_spend", "last_shopping_trip"},
            set(view))
        self.assertEqual(1, view["low_count"])                 # the low staple
        names_on_list = {i["name"] for i in view["shopping"]}
        self.assertEqual({"Coffee", "Cake"}, names_on_list)    # low staple + one-off

        # delete (archive) the staple → gone from the list
        self.assertEqual(200, c.delete(f"/api/inventory/{item['id']}").status_code)
        after = c.get("/api/inventory").get_json()
        self.assertNotIn("Coffee", {i["name"] for i in after["items"]})

    def test_add_validation_and_update_guards(self):
        c = self.client()
        self.assertEqual(400, c.post("/api/inventory", json={"name": " "}).status_code)
        item_id = c.post("/api/inventory", json={"name": "TP"}).get_json()["id"]
        # PUT with no status
        self.assertEqual(400, c.put(f"/api/inventory/{item_id}", json={"note": "x"}).status_code)
        # PUT on a missing item
        self.assertEqual(404, c.put("/api/inventory/999999", json={"status": "low"}).status_code)
        # DELETE missing
        self.assertEqual(404, c.delete("/api/inventory/999999").status_code)

    def test_restock_match_and_suggestion_surface(self):
        c = self.client()
        item = c.post("/api/inventory", json={"name": "Dog food", "status": "out"}).get_json()
        # set the override phrase via PUT (its own verb, same route)
        r = c.put(f"/api/inventory/{item['id']}", json={"restock_match": "chewy"})
        self.assertEqual(200, r.status_code)
        self.assertEqual("chewy", r.get_json()["restock_match"])
        day = r.get_json()["updated_at"][:10]
        # a matching purchase since it ran out -> a restock suggestion in the view
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction) "
            "VALUES (?, 4200, 'CHEWY.COM', 'Pets', 1, 0, 'simplefin', 'out')", (day,))
        conn.commit(); conn.close()
        sugg = c.get("/api/inventory").get_json()["restock_suggestions"]
        self.assertEqual(1, len(sugg))
        self.assertEqual(item["id"], sugg[0]["item_id"])
        self.assertEqual("phrase", sugg[0]["matched_by"])
        # purchase amount is dollars at the JSON edge
        self.assertEqual({"cents": 4200, "display": "$42.00"}, sugg[0]["purchase"]["amount"])

    def test_manual_interval_via_put_drives_the_forecast(self):
        c = self.client()
        item = c.post("/api/inventory", json={"name": "Coffee"}).get_json()
        # set a manual cadence via PUT (its own verb, same route)
        r = c.put(f"/api/inventory/{item['id']}", json={"restock_interval_days": 10})
        self.assertEqual(200, r.status_code)
        self.assertEqual(10, r.get_json()["restock_interval_days"])
        # it drives a 'manual' forecast with no purchase history at all
        fc = c.get("/api/inventory").get_json()["restock_forecast"]
        mine = [f for f in fc if f["item_id"] == item["id"]]
        self.assertEqual(1, len(mine))
        self.assertEqual("manual", mine[0]["interval_source"])
        self.assertEqual(10, mine[0]["interval_days"])
        # a bad value is a 400 from the verb; blank clears it (back to inferred)
        self.assertEqual(400, c.put(
            f"/api/inventory/{item['id']}", json={"restock_interval_days": 0}).status_code)
        cleared = c.put(f"/api/inventory/{item['id']}", json={"restock_interval_days": None})
        self.assertIsNone(cleared.get_json()["restock_interval_days"])

    def test_staple_spend_money_is_dollars_at_the_edge(self):
        c = self.client()
        item = c.post("/api/inventory", json={"name": "Coffee"}).get_json()
        conn = sqlite3.connect(self.db_path)
        for day, amt in (("2026-01-10", 1000), ("2026-02-10", 2000),
                         ("2026-03-10", 3000)):
            conn.execute(
                "INSERT INTO transactions (txn_date, amount_cents, description, "
                "category, paid_by, is_shared, source, direction) "
                "VALUES (?, ?, 'BLUE BOTTLE COFFEE', 'Dining', 1, 0, 'simplefin', 'out')",
                (day, amt))
        conn.commit(); conn.close()
        mine = [s for s in c.get("/api/inventory").get_json()["staple_spend"]
                if s["name"] == "Coffee"]
        self.assertEqual(1, len(mine))
        self.assertEqual({"cents": 6000, "display": "$60.00"}, mine[0]["total"])
        self.assertEqual({"cents": 2000, "display": "$20.00"}, mine[0]["monthly"])

    def test_requires_auth_and_write_scope(self):
        anon = self.app_module.app.test_client()
        self.assertEqual(401, anon.get("/api/inventory").status_code)

        import actions
        conn = sqlite3.connect(self.db_path); conn.row_factory = sqlite3.Row
        read_tok = actions.create_api_token(
            conn, "ui:avery", {"label": "r", "user_id": 1, "scopes": "read"})["token"]
        write_tok = actions.create_api_token(
            conn, "ui:avery", {"label": "w", "user_id": 1, "scopes": "read,write"})["token"]
        conn.close()
        # a read token can read but not add
        self.assertEqual(200, anon.get(
            "/api/inventory", headers={"Authorization": f"Bearer {read_tok}"}).status_code)
        self.assertEqual(403, anon.post(
            "/api/inventory", json={"name": "X"},
            headers={"Authorization": f"Bearer {read_tok}"}).status_code)
        self.assertEqual(201, anon.post(
            "/api/inventory", json={"name": "X"},
            headers={"Authorization": f"Bearer {write_tok}"}).status_code)


if __name__ == "__main__":
    unittest.main()
