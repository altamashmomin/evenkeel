"""Goal pace / completion projection (analytics Tier B #16): completion-date
projection at the LIFETIME-AVERAGE contribution rate (net saved ÷ time since the
first contribution). Reads goals + goal_contributions only (never transactions),
so it's trivially tripwire-safe. Withdrawals net out; status is complete /
on_track / behind / projected / no_pace."""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
sys.path.insert(0, str(REPO))

import _seedbase

import derivations


class GoalPaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-goalpace-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=51, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("DELETE FROM goal_contributions")
        self.db.execute("DELETE FROM goals")            # isolate from seed goals
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _goal(self, name, target, target_date=None):
        cur = self.db.execute(
            "INSERT INTO goals (name, target_cents, target_date, created_at) "
            "VALUES (?, ?, ?, '2026-01-01')", (name, target, target_date))
        self.db.commit()
        return cur.lastrowid

    def _contrib(self, goal_id, cents, when):
        self.db.execute(
            "INSERT INTO goal_contributions (goal_id, user_id, amount_cents, c_date) "
            "VALUES (?, 1, ?, ?)", (goal_id, cents, when))
        self.db.commit()

    def _by_name(self, name, as_of):
        return next(g for g in derivations.goal_pace(self.db, as_of) if g["name"] == name)

    def test_projects_at_lifetime_average_and_marks_on_track(self):
        g = self._goal("Vacation", 100000, "2026-12-31")
        self._contrib(g, 20000, "2026-01-01")
        self._contrib(g, 20000, "2026-03-01")            # saved 40000 over ...
        got = self._by_name("Vacation", "2026-05-01")     # ... 120 days to as_of
        self.assertEqual(40000, got["saved_cents"])
        self.assertEqual(60000, got["remaining_cents"])
        self.assertEqual(10000, got["monthly_rate_cents"])   # 40000 / 120d * 30
        # days_to_go = 60000*120/40000 = 180
        expected = (date(2026, 5, 1) + timedelta(days=180)).isoformat()
        self.assertEqual(expected, got["projected_date"])
        self.assertEqual("on_track", got["status"])

    def test_behind_when_projection_passes_the_target_date(self):
        g = self._goal("Car", 100000, "2026-06-01")       # sooner deadline
        self._contrib(g, 20000, "2026-01-01")
        self._contrib(g, 20000, "2026-03-01")
        self.assertEqual("behind", self._by_name("Car", "2026-05-01")["status"])

    def test_no_target_date_is_just_projected(self):
        g = self._goal("Rainy day", 100000, None)
        self._contrib(g, 20000, "2026-01-01")
        self._contrib(g, 20000, "2026-03-01")
        got = self._by_name("Rainy day", "2026-05-01")
        self.assertEqual("projected", got["status"])
        self.assertIsNotNone(got["projected_date"])

    def test_complete_when_saved_meets_target(self):
        g = self._goal("Phone", 50000, "2026-12-31")
        self._contrib(g, 30000, "2026-01-01")
        self._contrib(g, 25000, "2026-03-01")            # 55000 >= 50000
        got = self._by_name("Phone", "2026-05-01")
        self.assertEqual("complete", got["status"])
        self.assertEqual(0, got["remaining_cents"])
        self.assertIsNone(got["projected_date"])

    def test_withdrawals_net_out_of_the_rate(self):
        g = self._goal("Fund", 100000, "2026-12-31")
        self._contrib(g, 50000, "2026-01-01")
        self._contrib(g, -20000, "2026-02-01")           # a withdrawal
        got = self._by_name("Fund", "2026-05-01")
        self.assertEqual(30000, got["saved_cents"])       # net
        self.assertEqual(70000, got["remaining_cents"])

    def test_no_pace_when_no_contributions_or_no_net_progress(self):
        self._goal("Empty", 100000, "2026-12-31")        # no contributions
        drained = self._goal("Drained", 100000, "2026-12-31")
        self._contrib(drained, 10000, "2026-01-01")
        self._contrib(drained, -10000, "2026-02-01")     # net 0
        self.assertEqual("no_pace", self._by_name("Empty", "2026-05-01")["status"])
        self.assertEqual("no_pace", self._by_name("Drained", "2026-05-01")["status"])
        self.assertIsNone(self._by_name("Empty", "2026-05-01")["projected_date"])


class GoalPaceRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-goalpace-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=61, months=1)
        db = sqlite3.connect(self.db_path)
        db.execute("DELETE FROM goal_contributions")
        db.execute("DELETE FROM goals")
        db.execute("INSERT INTO goals (id, name, target_cents, target_date, created_at) "
                   "VALUES (1, 'Vacation', 100000, '2026-12-31', '2026-01-01')")
        for cents, when in ((20000, "2026-01-01"), (20000, "2026-03-01")):
            db.execute("INSERT INTO goal_contributions (goal_id, user_id, amount_cents, c_date) "
                       "VALUES (1, 1, ?, ?)", (cents, when))
        db.commit()
        db.close()
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "goalpace-route-secret")
        spec = importlib.util.spec_from_file_location("app_goalpace_route_test", REPO / "app.py")
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
        r = self.client().get("/api/analytics/goal-pace?as_of=2026-05-01")
        self.assertEqual(200, r.status_code)
        body = r.get_json()
        self.assertEqual({"as_of", "goals"}, set(body))
        g = body["goals"][0]
        self.assertEqual("Vacation", g["name"])
        self.assertEqual({"cents", "display"}, set(g["saved"]))
        self.assertEqual("$400.00", g["saved"]["display"])
        self.assertEqual("on_track", g["status"])
        self.assertEqual({"cents", "display"}, set(g["monthly_rate"]))

    def test_requires_auth(self):
        c = self.app_module.app.test_client()
        self.assertEqual(401, c.get("/api/analytics/goal-pace").status_code)


if __name__ == "__main__":
    unittest.main()
