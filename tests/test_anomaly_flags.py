"""Anomaly flags (analytics Tier B #15): categories whose month spend is
threshold% over their trailing 3-month (exclusive) average. Reads
spending_summary, so spend is refund-netted (EXEMPT in the tripwire — proven
here that a refund can clear a flag). Two noise guards: positive baseline + a
minimum dollar jump."""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
MONTH = "2026-04"
sys.path.insert(0, str(REPO))

import _seedbase

import derivations


class AnomalyFlagsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-anomaly-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=51, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("DELETE FROM transactions")     # fully controlled window
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _spend(self, category, when, cents):
        self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, category, "
            "paid_by, is_shared, source, direction, income_type) "
            "VALUES (?, ?, 'x', ?, 1, 0, 'simplefin', 'out', NULL)",
            (when, cents, category))
        self.db.commit()

    def _refund(self, category, when, cents):
        self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, category, "
            "paid_by, is_shared, source, direction, income_type) "
            "VALUES (?, ?, 'refund', ?, 1, 0, 'simplefin', 'in', 'refund')",
            (when, cents, category))
        self.db.commit()

    def _baseline_three(self, category, cents):
        for m in ("2026-01", "2026-02", "2026-03"):
            self._spend(category, m + "-15", cents)

    def _flag(self, category, month=MONTH, **kw):
        return next((a for a in derivations.anomaly_flags(self.db, month, **kw)
                     if a["category"] == category), None)

    def test_flags_a_category_over_its_trailing_average(self):
        self._baseline_three("Groceries", 10000)         # avg 10000
        self._spend("Groceries", "2026-04-10", 20000)     # +100%
        a = self._flag("Groceries")
        self.assertIsNotNone(a)
        self.assertEqual(20000, a["current_cents"])
        self.assertEqual(10000, a["baseline_cents"])
        self.assertEqual(10000, a["delta_cents"])
        self.assertEqual(100, a["pct_over"])

    def test_flat_category_not_flagged(self):
        for m in ("2026-01", "2026-02", "2026-03", "2026-04"):
            self._spend("Rent", m + "-01", 100000)
        self.assertIsNone(self._flag("Rent"))

    def test_small_dollar_jump_suppressed_by_noise_floor(self):
        self._baseline_three("Coffee", 100)              # avg 100
        self._spend("Coffee", "2026-04-10", 300)          # +200% but only +$2
        self.assertIsNone(self._flag("Coffee"))

    def test_new_category_with_no_baseline_not_flagged(self):
        self._spend("Vacation", "2026-04-10", 50000)      # nothing in baseline months
        self.assertIsNone(self._flag("Vacation"))

    def test_threshold_controls_sensitivity(self):
        self._baseline_three("Dining", 10000)
        self._spend("Dining", "2026-04-10", 16000)        # +60%
        self.assertIsNotNone(self._flag("Dining", threshold_pct=50))
        self.assertIsNone(self._flag("Dining", threshold_pct=75))

    def test_sorted_biggest_overage_first(self):
        self._baseline_three("A", 10000)
        self._baseline_three("B", 10000)
        self._spend("A", "2026-04-10", 15000)             # +50%
        self._spend("B", "2026-04-10", 25000)             # +150%
        cats = [f["category"] for f in derivations.anomaly_flags(self.db, MONTH)]
        self.assertEqual(["B", "A"], cats)

    def test_a_refund_nets_down_the_month_and_can_clear_a_flag(self):
        # justifies the tripwire EXEMPTION: spend is refund-netted
        self._baseline_three("Shopping", 10000)
        self._spend("Shopping", "2026-04-05", 20000)      # +100% → flagged
        self.assertIsNotNone(self._flag("Shopping"))
        self._refund("Shopping", "2026-04-20", 11000)     # nets April to 9000
        self.assertIsNone(self._flag("Shopping"))


class AnomalyRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-anomaly-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=61, months=1)
        db = sqlite3.connect(self.db_path)
        db.execute("DELETE FROM transactions")
        for m in ("2026-01", "2026-02", "2026-03"):
            db.execute("INSERT INTO transactions (txn_date, amount_cents, description, "
                       "category, paid_by, is_shared, source, direction, income_type) "
                       "VALUES (?, 10000, 'x', 'Groceries', 1, 0, 'simplefin', 'out', NULL)",
                       (m + "-15",))
        db.execute("INSERT INTO transactions (txn_date, amount_cents, description, "
                   "category, paid_by, is_shared, source, direction, income_type) "
                   "VALUES ('2026-04-10', 20000, 'x', 'Groceries', 1, 0, 'simplefin', 'out', NULL)")
        db.commit()
        db.close()
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "anomaly-route-secret")
        spec = importlib.util.spec_from_file_location("app_anomaly_route_test", REPO / "app.py")
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

    def test_endpoint_shape_and_money(self):
        r = self.client().get("/api/analytics/anomalies?month=2026-04&threshold=50")
        self.assertEqual(200, r.status_code)
        body = r.get_json()
        self.assertEqual({"month", "threshold_pct", "anomalies"}, set(body))
        self.assertEqual(50, body["threshold_pct"])
        g = next(a for a in body["anomalies"] if a["category"] == "Groceries")
        self.assertEqual({"cents", "display"}, set(g["current"]))
        self.assertEqual("$200.00", g["current"]["display"])
        self.assertEqual(100, g["pct_over"])

    def test_bad_threshold_is_400(self):
        r = self.client().get("/api/analytics/anomalies?threshold=abc")
        self.assertEqual(400, r.status_code)

    def test_requires_auth(self):
        c = self.app_module.app.test_client()
        self.assertEqual(401, c.get("/api/analytics/anomalies").status_code)


if __name__ == "__main__":
    unittest.main()
