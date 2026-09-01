"""The two least-guarded auth routes, previously at zero coverage
(CODE-REVIEW 2026-08-08, Tier 2 #12):

* POST /api/setup — the ONE raw-write exception in the codebase (a direct
  INSERT INTO members, not routed through an actions.py verb). First-run only:
  it must create exactly two accounts, hash their passwords, and then disable
  itself (403) so the account table can never be re-seeded from the open route.
* POST /api/logout — clears the session so a later authenticated request fails.

/api/setup is gated on `COUNT(members) == 0`, so its tests run against a
migrated-but-empty database: the shared seed is loaded, then members are
cleared (FK off — the route only counts members, orphaned child rows are
irrelevant to what's under test)."""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import _seedbase

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"

VALID_TWO = [
    {"username": "Avery", "display_name": "Avery", "password": "pw-avery-123"},
    {"username": "blake", "display_name": "Blake", "password": "pw-blake-456"},
]


def _load_app(db_path):
    os.environ["DATABASE_PATH"] = str(db_path)
    os.environ.setdefault("SECRET_KEY", "setup-logout-test-secret")
    spec = importlib.util.spec_from_file_location(
        f"app_setuplogout_{db_path.stem}", REPO / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config["TESTING"] = True
    return module


class SetupRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-setup-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=64, months=1)
        # First-run state: an empty members table. FK off so the delete need
        # not chase every member-referencing child row — the route only counts.
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM members")
        conn.commit()
        conn.close()
        self.app_module = _load_app(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def members(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT username, display_name, password_hash, active "
                "FROM members ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_setup_creates_two_accounts_hashes_and_then_disables_itself(self):
        c = self.app_module.app.test_client()
        r = c.post("/api/setup", json={"users": VALID_TWO})
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.get_json()["ok"])

        rows = self.members()
        self.assertEqual(2, len(rows))
        self.assertEqual(["avery", "blake"], [m["username"] for m in rows])  # lowercased
        for m in rows:
            self.assertEqual(1, m["active"])                       # active by default
            self.assertNotIn("pw-", m["password_hash"])            # never stored plaintext

        # The account genuinely works: the raw-inserted, hashed credential logs in.
        login = c.post("/api/login", json={"username": "avery", "password": "pw-avery-123"})
        self.assertEqual(200, login.status_code)

        # Re-running the open route is refused now that accounts exist — the
        # table can't be re-seeded or a third account slipped in.
        again = c.post("/api/setup", json={"users": VALID_TWO})
        self.assertEqual(403, again.status_code)
        self.assertEqual("setup already completed", again.get_json()["error"])
        self.assertEqual(2, len(self.members()))                   # unchanged

    def test_setup_requires_exactly_two_users(self):
        c = self.app_module.app.test_client()
        for bad in ([VALID_TWO[0]], VALID_TWO + [VALID_TWO[0]], [], "nope"):
            r = c.post("/api/setup", json={"users": bad})
            self.assertEqual(400, r.status_code)
        self.assertEqual(0, len(self.members()))                   # nothing written

    def test_setup_rejects_a_short_password(self):
        c = self.app_module.app.test_client()
        users = [dict(VALID_TWO[0], password="short"), VALID_TWO[1]]
        r = c.post("/api/setup", json={"users": users})
        self.assertEqual(400, r.status_code)
        self.assertEqual(0, len(self.members()))

    def test_setup_rejects_missing_username_or_display_name(self):
        c = self.app_module.app.test_client()
        for users in (
            [dict(VALID_TWO[0], username=""), VALID_TWO[1]],
            [dict(VALID_TWO[0], display_name="  "), VALID_TWO[1]],
        ):
            r = c.post("/api/setup", json={"users": users})
            self.assertEqual(400, r.status_code)
        self.assertEqual(0, len(self.members()))

    def test_setup_rejects_duplicate_usernames(self):
        c = self.app_module.app.test_client()
        users = [VALID_TWO[0], dict(VALID_TWO[1], username="AVERY")]  # dup after lowercasing
        r = c.post("/api/setup", json={"users": users})
        self.assertEqual(400, r.status_code)
        self.assertEqual(0, len(self.members()))


class LogoutRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-logout-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=64, months=1)
        self.app_module = _load_app(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_logout_clears_the_session(self):
        c = self.app_module.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = 1
        # An authenticated route works while the session holds.
        self.assertEqual(200, c.get("/api/me").status_code)

        r = c.post("/api/logout")
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.get_json()["ok"])

        # Session is gone: the cleared key, and a login-required route now 401s.
        with c.session_transaction() as s:
            self.assertNotIn("user_id", s)
        self.assertEqual(401, c.get("/api/me").status_code)


if __name__ == "__main__":
    unittest.main()
