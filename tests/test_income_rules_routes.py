"""Income rules routes: thin callers over create_income_rule /
set_rule_enabled / apply_rules, JSON shapes, and audit coverage."""
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


class IncomeRuleRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-rulesroute-test-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=61, months=1)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "rules-route-test-secret")
        spec = importlib.util.spec_from_file_location(
            "app_rules_route_test", REPO / "app.py")
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

    def insert_inflow(self, description="ADP PAYROLL 8842", amount_cents=320000):
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source, direction, income_type)
               VALUES (?, ?, ?, 'Other', 1, 0, 'simplefin', 'in', 'unclassified')""",
            (date.today().isoformat(), amount_cents, description))
        txn_id = cur.lastrowid
        conn.commit()
        conn.close()
        return txn_id

    def test_create_list_and_audit(self):
        client = self.client()
        created = client.post("/api/income/rules", json={
            "match_desc": "ADP PAYROLL", "set_type": "paycheck"})
        self.assertEqual(201, created.status_code)
        body = created.get_json()
        self.assertEqual(
            ["created_at", "enabled", "hit_count", "id", "match_account",
             "match_desc", "max_cents", "min_cents", "priority", "set_paid_by",
             "set_transfer", "set_type"], sorted(body))
        self.assertTrue(body["enabled"])

        listing = client.get("/api/income/rules").get_json()
        self.assertEqual([body["id"]], [r["id"] for r in listing])

        missing = client.post("/api/income/rules", json={"set_type": "paycheck"})
        self.assertEqual(400, missing.status_code)
        self.assertEqual(
            {"error": "at least one match criterion (description, account, "
                       "or amount bounds) is required"}, missing.get_json())

        conn = sqlite3.connect(self.db_path)
        try:
            audit = conn.execute(
                "SELECT actor, action FROM audit_log WHERE target = ?",
                (f"rule:{body['id']}",)).fetchall()
        finally:
            conn.close()
        self.assertEqual([("ui:avery", "create_income_rule")], audit)

    def test_update_only_supports_enabled(self):
        client = self.client()
        rule_id = client.post("/api/income/rules", json={
            "match_desc": "ADP", "set_type": "paycheck"}).get_json()["id"]

        missing_field = client.put(f"/api/income/rules/{rule_id}", json={"priority": 5})
        self.assertEqual(400, missing_field.status_code)
        self.assertEqual(
            {"error": "only 'enabled' can be changed"}, missing_field.get_json())

        disabled = client.put(f"/api/income/rules/{rule_id}", json={"enabled": False})
        self.assertEqual(200, disabled.status_code)
        self.assertFalse(disabled.get_json()["enabled"])

        not_found = client.put("/api/income/rules/999999", json={"enabled": True})
        self.assertEqual(404, not_found.status_code)

    def test_apply_dry_run_previews_then_apply_commits(self):
        client = self.client()
        client.post("/api/income/rules", json={
            "match_desc": "ADP PAYROLL", "set_type": "paycheck"})
        txn_id = self.insert_inflow()

        preview = client.post("/api/income/rules/apply", json={"dry_run": True})
        self.assertEqual(200, preview.status_code)
        preview_body = preview.get_json()
        self.assertTrue(preview_body["dry_run"])
        self.assertEqual(1, len(preview_body["changes"]))

        # dry run must not have written anything
        conn = sqlite3.connect(self.db_path)
        try:
            income_type = conn.execute(
                "SELECT income_type FROM transactions WHERE id = ?",
                (txn_id,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("unclassified", income_type)

        applied = client.post("/api/income/rules/apply", json={})
        self.assertEqual(200, applied.status_code)
        applied_body = applied.get_json()
        self.assertFalse(applied_body["dry_run"])
        self.assertEqual(1, len(applied_body["changes"]))

        conn = sqlite3.connect(self.db_path)
        try:
            income_type = conn.execute(
                "SELECT income_type FROM transactions WHERE id = ?",
                (txn_id,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("paycheck", income_type)


if __name__ == "__main__":
    unittest.main()
