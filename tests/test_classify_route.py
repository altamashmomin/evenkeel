"""PUT /api/transactions/<id>/classify: thin caller over classify_inflow,
response extends the v1 shape without touching the frozen listing JSON."""
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"


class ClassifyRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-classifyroute-test-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "43", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "classify-route-test-secret")
        spec = importlib.util.spec_from_file_location(
            "app_classify_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source, direction, income_type)
               VALUES (?, 320000, 'ADP PAYROLL 8842', 'Other', 1, 0,
                       'simplefin', 'in', 'unclassified')""",
            (date.today().isoformat(),))
        self.inflow_id = cur.lastrowid
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def client(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
        return client

    def test_classify_returns_extended_v1_shape_and_writes_audit(self):
        client = self.client()
        response = client.put(
            f"/api/transactions/{self.inflow_id}/classify",
            json={"income_type": "paycheck"})
        self.assertEqual(200, response.status_code)
        body = response.get_json()
        self.assertEqual(
            ["amount", "category", "date", "description", "direction", "id",
             "income_type", "is_shared", "paid_by", "payer_share_pct",
             "rule_suggestion", "source"],
            sorted(body))
        self.assertEqual("paycheck", body["income_type"])
        self.assertEqual("in", body["direction"])
        # First tag of this type (count == 1): no rule offered yet.
        self.assertIsNone(body["rule_suggestion"])

    def test_second_same_type_tag_offers_a_prefilled_rule(self):
        client = self.client()
        # A second, independent inflow tagged the same way trips the offer.
        conn = sqlite3.connect(self.db_path)
        second = conn.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source, direction, income_type)
               VALUES (?, 411000, 'ACME CORP DIRECT DEP', 'Other', 1, 0,
                       'simplefin', 'in', 'unclassified')""",
            (date.today().isoformat(),)).lastrowid
        conn.commit()
        conn.close()

        client.put(f"/api/transactions/{self.inflow_id}/classify",
                   json={"income_type": "paycheck"})
        body = client.put(f"/api/transactions/{second}/classify",
                          json={"income_type": "paycheck"}).get_json()
        self.assertEqual(
            {"match_desc": "ACME CORP DIRECT DEP", "set_type": "paycheck"},
            body["rule_suggestion"])

        conn = sqlite3.connect(self.db_path)
        try:
            audit = conn.execute(
                "SELECT actor, action FROM audit_log WHERE target = ?",
                (f"transaction:{self.inflow_id}",)).fetchall()
        finally:
            conn.close()
        self.assertEqual([("ui:avery", "classify_inflow")], audit)

    def test_classify_missing_row_is_404(self):
        client = self.client()
        response = client.put(
            "/api/transactions/999999/classify", json={"income_type": "paycheck"})
        self.assertEqual(404, response.status_code)
        self.assertEqual({"error": "not found"}, response.get_json())

    def test_classify_outflow_is_400(self):
        client = self.client()
        outflow_id = client.post("/api/transactions", json={
            "date": date.today().isoformat(), "amount": 10,
            "description": "Coffee", "category": "Dining",
            "paid_by": 1, "is_shared": False,
        }).get_json()["id"]
        response = client.put(
            f"/api/transactions/{outflow_id}/classify",
            json={"income_type": "paycheck"})
        self.assertEqual(400, response.status_code)
        self.assertEqual(
            {"error": "only inflows can be classified"}, response.get_json())


if __name__ == "__main__":
    unittest.main()
