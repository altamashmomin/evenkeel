"""activity_digest (NOTIFICATIONS-DESIGN increment 1): the change-digest read
over audit_log + pending_actions. Counts every executed write in [since, now),
splits the routine 'sync' feed from the human/assistant writes the digest is
for, groups by actor + action, and reports the count awaiting approval now.
Reads NO transactions — money-neutral by construction. Plus the thin route."""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import _seedbase

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import actions  # noqa: E402
from derivations import activity_digest  # noqa: E402


class ActivityDigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-digest-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=77, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        # Isolate from whatever writes the seed logged.
        self.db.execute("DELETE FROM audit_log")
        self.db.execute("DELETE FROM pending_actions")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _audit(self, actor, action, at):
        self.db.execute(
            "INSERT INTO audit_log (at, actor, action, target, detail_json) "
            "VALUES (?, ?, ?, 't', '{}')", (at, actor, action))
        self.db.commit()

    def _pending(self, expires_at, status="pending"):
        self.db.execute(
            "INSERT INTO pending_actions (token, action_type, payload_json, "
            "preview_json, created_at, expires_at, status) VALUES "
            "(?, 'create_rule', '{}', '{}', '2026-07-01T00:00:00+00:00', ?, ?)",
            (f"tok-{expires_at}-{status}", expires_at, status))
        self.db.commit()

    def test_counts_group_split_and_window(self):
        self._audit("ui:avery", "classify_inflow", "2026-07-10T09:00:00+00:00")
        self._audit("mcp:cc", "set_rule_enabled", "2026-07-10T10:00:00+00:00")
        self._audit("mcp:cc", "set_budget", "2026-07-10T11:00:00+00:00")
        self._audit("sync", "record_transaction", "2026-07-10T12:00:00+00:00")
        self._audit("ui:avery", "add_item", "2026-07-01T00:00:00+00:00")  # before window
        self._audit("ui:avery", "add_item", "2026-07-25T00:00:00+00:00")  # after window
        d = activity_digest(self.db, "2026-07-05T00:00:00+00:00",
                            "2026-07-20T00:00:00+00:00")
        self.assertEqual(4, d["total"])                       # both out-of-window excluded
        self.assertEqual(3, d["assistant_and_human_writes"])  # 1 ui + 2 mcp
        self.assertEqual(1, d["sync_writes"])
        # by_actor sorted by count desc: mcp:cc (2) leads
        self.assertEqual({"actor": "mcp:cc", "count": 2}, d["by_actor"][0])
        by_action = {x["action"]: x["count"] for x in d["by_action"]}
        self.assertEqual({"classify_inflow": 1, "set_rule_enabled": 1,
                          "set_budget": 1, "record_transaction": 1}, by_action)

    def test_pending_approvals_counts_only_unexpired_pending(self):
        self._pending("2026-07-20T00:00:00+00:00")                       # future → counts
        self._pending("2026-07-01T00:00:00+00:00")                       # past → expired-out
        self._pending("2026-07-20T00:00:00+00:00", status="confirmed")  # not pending
        d = activity_digest(self.db, "2026-07-01T00:00:00+00:00",
                            "2026-07-10T00:00:00+00:00")
        self.assertEqual(1, d["pending_approvals"])

    def test_empty_window_is_money_neutral(self):
        # A window with no writes is empty regardless of how many transactions
        # the seed holds — the digest never reads the transactions table.
        d = activity_digest(self.db, "2026-07-01T00:00:00+00:00",
                            "2026-07-02T00:00:00+00:00")
        self.assertEqual(0, d["total"])
        self.assertEqual([], d["by_actor"])
        self.assertEqual([], d["by_action"])

    def test_the_audit_helper_lands_in_the_digest(self):
        # End-to-end through the real write helper, not a hand-rolled INSERT.
        actions._write_audit(self.db, "mcp:cc", "set_rule_enabled", "rule:6",
                             {"enabled": False}, at="2026-07-10T08:00:00+00:00")
        self.db.commit()
        d = activity_digest(self.db, "2026-07-01T00:00:00+00:00",
                            "2026-07-20T00:00:00+00:00")
        self.assertEqual(1, d["assistant_and_human_writes"])


class ActivityDigestRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-digest-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=78, months=1)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "digest-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_digest_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def session_client(self):
        c = self.app_module.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = 1
        return c

    def token(self, scopes):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return actions.create_api_token(
                conn, "ui:avery",
                {"label": "cc", "user_id": 1, "scopes": scopes})["token"]
        finally:
            conn.close()

    def test_since_is_required(self):
        self.assertEqual(400, self.session_client().get("/api/activity/digest").status_code)

    def test_returns_the_digest_shape_to_a_read_token(self):
        tok = self.token("read")
        r = self.app_module.app.test_client().get(
            "/api/activity/digest?since=2026-01-01T00:00:00+00:00",
            headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(200, r.status_code)
        body = r.get_json()
        for key in ("total", "assistant_and_human_writes", "sync_writes",
                    "by_actor", "by_action", "pending_approvals"):
            self.assertIn(key, body)

    def test_requires_auth(self):
        self.assertEqual(401, self.app_module.app.test_client().get(
            "/api/activity/digest?since=2026-01-01T00:00:00+00:00").status_code)


if __name__ == "__main__":
    unittest.main()
