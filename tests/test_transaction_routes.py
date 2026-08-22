"""PUT/DELETE /api/transactions/<id> stay byte-shaped like v1 while
dispatching through edit_transaction / delete_transaction."""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import _seedbase

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"


class TransactionRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-txnroute-test-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=23, months=2)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "txn-route-test-secret")
        spec = importlib.util.spec_from_file_location(
            "app_txn_route_test", REPO / "app.py")
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

    def a_manual_txn(self, client):
        created = client.post("/api/transactions", json={
            "date": date.today().isoformat(), "amount": 25,
            "description": "Route test row", "category": "Groceries",
            "paid_by": 1, "is_shared": True, "payer_share_pct": 50,
        })
        self.assertEqual(201, created.status_code)
        return created.get_json()["id"]

    def test_update_keeps_v1_shape_and_applies_partial_edit(self):
        client = self.client()
        txn_id = self.a_manual_txn(client)
        response = client.put(f"/api/transactions/{txn_id}", json={"amount": 30})
        self.assertEqual(200, response.status_code)
        body = response.get_json()
        self.assertEqual(
            ["amount", "category", "date", "description", "id", "is_shared",
             "paid_by", "payer_share_pct", "source"], sorted(body))
        self.assertEqual(30.0, body["amount"])
        self.assertEqual(50.0, body["payer_share_pct"])

    def test_update_missing_row_is_404(self):
        client = self.client()
        response = client.put("/api/transactions/999999", json={"amount": 5})
        self.assertEqual(404, response.status_code)
        self.assertEqual({"error": "not found"}, response.get_json())

    def test_update_empty_payload_is_400(self):
        client = self.client()
        txn_id = self.a_manual_txn(client)
        response = client.put(f"/api/transactions/{txn_id}", json={})
        self.assertEqual(400, response.status_code)
        self.assertEqual({"error": "nothing to update"}, response.get_json())

    def test_update_settlement_row_is_400(self):
        client = self.client()
        balance = client.get("/api/balance").get_json()
        settled = client.post("/api/transactions", json={
            "date": date.today().isoformat(), "amount": balance["amount"],
            "description": "settlement", "category": "Settlement",
            "paid_by": balance["owes"]["id"], "is_shared": True,
            "payer_share_pct": 0, "source": "settlement",
        }).get_json()
        response = client.put(
            f"/api/transactions/{settled['id']}", json={"amount": 1})
        self.assertEqual(400, response.status_code)
        self.assertEqual(
            {"error": "settlement rows cannot be edited; delete and recreate"},
            response.get_json())

    # -- the recategorize door (B1) + its mirage F1/F2/F3 hardening ----------
    def test_recategorize_route_relabels_only(self):
        client = self.client()
        txn_id = self.a_manual_txn(client)
        r = client.put(f"/api/transactions/{txn_id}/recategorize",
                       json={"category": "Household"})
        self.assertEqual(200, r.status_code)
        body = r.get_json()
        self.assertEqual("Household", body["category"])
        self.assertEqual(25.0, body["amount"])          # amount untouched

    def test_recategorize_route_ignores_smuggled_fields(self):
        # the HTTP edge reads only `category` — extra fields can't over-reach.
        client = self.client()
        txn_id = self.a_manual_txn(client)
        r = client.put(f"/api/transactions/{txn_id}/recategorize",
                       json={"category": "Household", "amount": 1, "description": "X"})
        self.assertEqual(200, r.status_code)
        body = r.get_json()
        self.assertEqual("Household", body["category"])
        self.assertEqual(25.0, body["amount"])          # smuggled amount ignored
        self.assertEqual("Route test row", body["description"])  # smuggled desc ignored

    def test_recategorize_route_bad_type_is_400_not_500(self):
        # mirage F1: a non-string category is a clean 400, never a 500.
        client = self.client()
        txn_id = self.a_manual_txn(client)
        for bad in ([1, 2], 123, {"a": 1}):
            r = client.put(f"/api/transactions/{txn_id}/recategorize",
                           json={"category": bad})
            self.assertEqual(400, r.status_code, f"bad category {bad!r}")
            self.assertEqual({"error": "category must be text"}, r.get_json())

    def test_recategorize_route_blank_or_missing_is_400(self):
        # mirage F2: no silent clobber to 'Other'.
        client = self.client()
        txn_id = self.a_manual_txn(client)
        for payload in ({}, {"category": None}, {"category": "   "}):
            r = client.put(f"/api/transactions/{txn_id}/recategorize", json=payload)
            self.assertEqual(400, r.status_code, f"payload {payload}")
            self.assertEqual({"error": "a category is required"}, r.get_json())

    def test_recategorize_route_missing_row_is_404(self):
        client = self.client()
        r = client.put("/api/transactions/999999/recategorize",
                       json={"category": "Household"})
        self.assertEqual(404, r.status_code)

    def test_create_writes_v1_shape_and_an_audit_row(self):
        client = self.client()
        txn_id = self.a_manual_txn(client)
        conn = sqlite3.connect(self.db_path)
        try:
            audit = conn.execute(
                "SELECT actor, action FROM audit_log WHERE target = ?",
                (f"transaction:{txn_id}",)).fetchall()
        finally:
            conn.close()
        self.assertEqual([("ui:avery", "record_transaction")], audit)

    def test_manual_post_cannot_create_an_inflow(self):
        # The manual route forces source='manual' and never passes
        # direction, so a client slipping "direction":"in" into the body is
        # ignored — a manual entry is always a spend (Finding 3). If a
        # designed manual-income feature ever wants otherwise, that's a new,
        # explicit path, not this one silently allowing it.
        client = self.client()
        created = client.post("/api/transactions", json={
            "date": date.today().isoformat(), "amount": 500,
            "description": "Sneaky paycheck", "category": "Other",
            "paid_by": 1, "is_shared": True, "payer_share_pct": 50,
            "direction": "in",
        })
        self.assertEqual(201, created.status_code)
        txn_id = created.get_json()["id"]
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT direction, income_type FROM transactions WHERE id = ?",
                (txn_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(("out", None), row)

    def test_delete_keeps_v1_shape_and_writes_audit(self):
        client = self.client()
        txn_id = self.a_manual_txn(client)
        response = client.delete(f"/api/transactions/{txn_id}")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"ok": True}, response.get_json())
        self.assertEqual(404, client.delete(f"/api/transactions/{txn_id}").status_code)

        conn = sqlite3.connect(self.db_path)
        try:
            audit = conn.execute(
                "SELECT actor, action FROM audit_log WHERE target = ? "
                "ORDER BY id", (f"transaction:{txn_id}",)).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [("ui:avery", "record_transaction"), ("ui:avery", "delete_transaction")],
            audit)


if __name__ == "__main__":
    unittest.main()
