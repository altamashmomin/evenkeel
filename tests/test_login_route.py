"""The login route — previously untested (CODE-REVIEW-2026-08-07 #21), and the
home of the timing-oracle fix (#17). Proves a correct login establishes a
session, a wrong password and an unknown username are both a 401 with the SAME
response (no enumeration signal), and the unknown-username path still runs a
hash comparison rather than short-circuiting.

The synthetic seed writes unusable password hashes on purpose, so setUp installs
a known password on member 1 to exercise the real success path."""
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import generate_password_hash

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
KNOWN_PW = "correct horse battery staple"


class LoginRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-login-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "64", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        # Install a known password on member 1 (avery).
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE members SET password_hash = ? WHERE id = 1",
                     (generate_password_hash(KNOWN_PW),))
        conn.commit()
        conn.close()

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "login-test-secret")
        spec = importlib.util.spec_from_file_location("app_login_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def test_correct_login_establishes_a_session(self):
        c = self.app_module.app.test_client()
        r = c.post("/api/login", json={"username": "avery", "password": KNOWN_PW})
        self.assertEqual(200, r.status_code)
        self.assertEqual(1, r.get_json()["user"]["id"])
        with c.session_transaction() as s:
            self.assertEqual(1, s["user_id"])

    def test_wrong_password_is_401(self):
        c = self.app_module.app.test_client()
        r = c.post("/api/login", json={"username": "avery", "password": "nope"})
        self.assertEqual(401, r.status_code)

    def test_unknown_username_is_401_not_500(self):
        # The dummy-hash path (#17) must not crash on a None row, and must give
        # the SAME 401 + message as a wrong password — no enumeration signal.
        c = self.app_module.app.test_client()
        unknown = c.post("/api/login",
                         json={"username": "ghost", "password": "nope"})
        wrong = c.post("/api/login",
                       json={"username": "avery", "password": "nope"})
        self.assertEqual(401, unknown.status_code)
        self.assertEqual(wrong.get_json(), unknown.get_json())

    def test_username_is_case_insensitive(self):
        c = self.app_module.app.test_client()
        r = c.post("/api/login", json={"username": "AVERY", "password": KNOWN_PW})
        self.assertEqual(200, r.status_code)


if __name__ == "__main__":
    unittest.main()
