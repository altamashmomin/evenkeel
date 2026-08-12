"""GET /api/forecast/baselines: the Forecast lab's measured inputs —
per-category average monthly net spend + average monthly paycheck income
over a trailing window. Money as {cents, display} at the JSON edge; the
what-if math itself is client-side and lives in render.js (seam-tested in
tests/test_render.js), so this only proves the facts pipe through."""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import _seedbase

REPO = Path(__file__).resolve().parents[1]
C = "2026-07"


class ForecastBaselinesRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-fc-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=79, months=1)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM splits")
        conn.execute("DELETE FROM transactions")
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, 240000, 'pay', 'Other', 1, 0, 'simplefin', 'in', 'paycheck')""",
            (f"{C}-05",))
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, 60000, 'rent', 'Housing', 1, 0, 'manual', 'out')""",
            (f"{C}-01",))
        conn.commit()
        conn.close()

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "fc-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_fc_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, logged_in=True):
        client = self.app_module.app.test_client()
        if logged_in:
            with client.session_transaction() as session:
                session["user_id"] = 1
        return client

    def test_shape_and_money_at_the_edge(self):
        body = self.client().get(
            f"/api/forecast/baselines?anchor={C}&months_back=2").get_json()
        self.assertEqual(["anchor", "categories", "income_avg", "months",
                          "months_back"], sorted(body))
        self.assertEqual(C, body["anchor"])
        self.assertEqual(["2026-06", C], body["months"])
        # 240000 cents over 2 months -> 120000, as {cents, display}
        self.assertEqual({"cents": 120000, "display": "$1,200.00"},
                         body["income_avg"])
        housing = body["categories"][0]
        self.assertEqual("Housing", housing["category"])
        self.assertEqual(30000, housing["avg"]["cents"])
        self.assertEqual(60000, housing["total"]["cents"])

    def test_bad_anchor_is_400(self):
        self.assertEqual(400, self.client().get(
            "/api/forecast/baselines?anchor=nope").status_code)

    def test_months_back_clamped_and_non_numeric_is_400(self):
        body = self.client().get(
            f"/api/forecast/baselines?anchor={C}&months_back=0").get_json()
        self.assertEqual(1, body["months_back"])
        self.assertEqual(400, self.client().get(
            "/api/forecast/baselines?months_back=six").status_code)

    def test_requires_login(self):
        self.assertEqual(401, self.client(logged_in=False)
                         .get("/api/forecast/baselines").status_code)


if __name__ == "__main__":
    unittest.main()
