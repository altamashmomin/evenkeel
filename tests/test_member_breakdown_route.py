"""GET /api/analytics/member-breakdown: per-member paid/owed/net for shared
expenses in a month, money as {cents, display}."""
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
M = "2026-06"


class MemberBreakdownRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-member-bd-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "100", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM splits")
        conn.execute("DELETE FROM transactions")
        cur = conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, 6000, 'Rent', 'Rent', 1, 1, 'manual', 'out')""",
            (f"{M}-01",))
        conn.executemany(
            "INSERT INTO splits (transaction_id, member_id, share_bp) VALUES (?, ?, ?)",
            [(cur.lastrowid, 1, 5000), (cur.lastrowid, 2, 5000)])
        conn.commit()
        conn.close()

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "member-bd-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_member_bd_route_test", REPO / "app.py")
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

    def test_breakdown_shape_and_dual_money(self):
        body = self.client().get(
            f"/api/analytics/member-breakdown?month={M}").get_json()
        self.assertEqual(["members", "month"], sorted(body))
        self.assertEqual(2, len(body["members"]))
        avery, blake = body["members"]   # ordered by member id (1, 2)
        self.assertEqual("avery", avery["username"])
        self.assertEqual({"cents": 6000, "display": "$60.00"}, avery["paid"])
        self.assertEqual({"cents": 3000, "display": "$30.00"}, avery["owed"])
        self.assertEqual({"cents": 3000, "display": "$30.00"}, avery["net"])
        self.assertEqual({"cents": -3000, "display": "-$30.00"}, blake["net"])

    def test_default_month_is_current_period(self):
        body = self.client().get("/api/analytics/member-breakdown").get_json()
        self.assertEqual(self.app_module.current_period(), body["month"])

    def test_requires_login(self):
        self.assertEqual(401, self.client(logged_in=False)
                         .get("/api/analytics/member-breakdown").status_code)


if __name__ == "__main__":
    unittest.main()
