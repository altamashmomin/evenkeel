"""change_password — the first write verb over members — and its session-only
route. The verb: current password must verify, new one >= 8 chars and
different, the audit row carries no secret. The route: session only (a bearer
token must never change a password), wrong current passwords rate-limited."""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _seedbase

import actions


def set_known_password(db_path, member_id, password):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE members SET password_hash = ? WHERE id = ?",
                 (generate_password_hash(password), member_id))
    conn.commit(); conn.close()


class ChangePasswordVerbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-pw-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=51, months=1)
        set_known_password(self.db_path, 1, "old-password-1")
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def hash_of(self, member_id):
        return self.db.execute("SELECT password_hash FROM members WHERE id = ?",
                               (member_id,)).fetchone()["password_hash"]

    def test_changes_the_hash_and_audits_without_secrets(self):
        out = actions.change_password(self.db, "ui:avery", 1, "old-password-1", "new-password-9")
        self.assertEqual({"member_id": 1}, out)
        self.assertTrue(check_password_hash(self.hash_of(1), "new-password-9"))
        self.assertFalse(check_password_hash(self.hash_of(1), "old-password-1"))
        audit = self.db.execute(
            "SELECT actor, detail_json FROM audit_log WHERE action = 'change_password' "
            "AND target = 'member:1'").fetchone()
        self.assertEqual("ui:avery", audit["actor"])
        for secret in ("old-password-1", "new-password-9", "pbkdf2", "scrypt", "$"):
            self.assertNotIn(secret, audit["detail_json"])

    def test_rejects_wrong_current_short_new_and_unchanged(self):
        before = self.hash_of(1)
        with self.assertRaisesRegex(actions.ActionError, "current password is wrong"):
            actions.change_password(self.db, "ui:avery", 1, "nope", "new-password-9")
        with self.assertRaisesRegex(actions.ActionError, "at least 8"):
            actions.change_password(self.db, "ui:avery", 1, "old-password-1", "short")
        with self.assertRaisesRegex(actions.ActionError, "must differ"):
            actions.change_password(self.db, "ui:avery", 1, "old-password-1", "old-password-1")
        self.assertEqual(before, self.hash_of(1))                 # untouched
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM audit_log WHERE action = 'change_password'").fetchone())

    def test_missing_or_inactive_member_is_not_found(self):
        with self.assertRaises(actions.NotFound):
            actions.change_password(self.db, "ui:avery", 999, "x", "new-password-9")


class ChangePasswordRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-pw-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=61, months=1)
        set_known_password(self.db_path, 1, "old-password-1")
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "pw-route-secret")
        spec = importlib.util.spec_from_file_location("app_pw_route_test", REPO / "app.py")
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

    def test_session_changes_password_and_login_follows(self):
        c = self.client()
        r = c.post("/api/me/password", json={"current_password": "old-password-1",
                                             "new_password": "new-password-9"})
        self.assertEqual(200, r.status_code)
        anon = self.app_module.app.test_client()
        username = sqlite3.connect(self.db_path).execute(
            "SELECT username FROM members WHERE id = 1").fetchone()[0]
        self.assertEqual(401, anon.post("/api/login", json={
            "username": username, "password": "old-password-1"}).status_code)
        self.assertEqual(200, anon.post("/api/login", json={
            "username": username, "password": "new-password-9"}).status_code)

    def test_anon_and_bearer_are_refused(self):
        anon = self.app_module.app.test_client()
        body = {"current_password": "old-password-1", "new_password": "new-password-9"}
        self.assertEqual(401, anon.post("/api/me/password", json=body).status_code)
        conn = sqlite3.connect(self.db_path); conn.row_factory = sqlite3.Row
        tok = actions.create_api_token(
            conn, "ui:avery", {"label": "w", "user_id": 1, "scopes": "read,write"})["token"]
        conn.close()
        r = anon.post("/api/me/password", json=body,
                      headers={"Authorization": f"Bearer {tok}"})
        self.assertIn(r.status_code, (401, 403))          # session only, even with write scope

    def test_wrong_current_is_400_then_rate_limited(self):
        c = self.client()
        body = {"current_password": "wrong", "new_password": "new-password-9"}
        for _ in range(self.app_module.PASSWORD_MAX_FAILS):
            self.assertEqual(400, c.post("/api/me/password", json=body).status_code)
        self.assertEqual(429, c.post("/api/me/password", json=body).status_code)


if __name__ == "__main__":
    unittest.main()
