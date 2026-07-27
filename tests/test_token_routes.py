"""Token-management routes: mint/list/revoke are session-only (a bearer
token must never mint tokens), the plaintext is returned once, and the list
never leaks the token or its hash."""
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


class TokenRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-token-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "73", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "token-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_token_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def session_client(self):
        client = self.app_module.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
        return client

    def test_mint_returns_plaintext_once_and_the_token_works_as_bearer(self):
        resp = self.session_client().post("/api/tokens", json={"label": "laptop"})
        self.assertEqual(201, resp.status_code)
        body = resp.get_json()
        self.assertEqual({"created_at", "id", "label", "scopes", "token"}, set(body))
        self.assertEqual("read", body["scopes"])
        # The returned plaintext authenticates a read request.
        got = self.app_module.app.test_client().get(
            "/api/me", headers={"Authorization": f"Bearer {body['token']}"})
        self.assertEqual(200, got.status_code)
        self.assertEqual(1, got.get_json()["user_id"])

    def test_list_never_leaks_token_or_hash(self):
        client = self.session_client()
        client.post("/api/tokens", json={"label": "one"})
        rows = client.get("/api/tokens").get_json()
        self.assertEqual(1, len(rows))
        self.assertEqual("one", rows[0]["label"])
        self.assertNotIn("token", rows[0])
        self.assertNotIn("token_hash", rows[0])

    def test_revoke_disables_the_token(self):
        client = self.session_client()
        token = client.post("/api/tokens", json={"label": "x"}).get_json()
        self.assertEqual(200, client.post(
            f"/api/tokens/{token['id']}/revoke").status_code)
        # The token no longer authenticates.
        got = self.app_module.app.test_client().get(
            "/api/me", headers={"Authorization": f"Bearer {token['token']}"})
        self.assertEqual(401, got.status_code)
        self.assertTrue(client.get("/api/tokens").get_json()[0]["revoked"])

    def test_mint_requires_a_label(self):
        self.assertEqual(400, self.session_client()
                         .post("/api/tokens", json={"label": ""}).status_code)

    def test_token_management_is_session_only(self):
        # A bearer token (even a hypothetical write one) cannot reach the
        # token routes — they are session_required.
        token = self.session_client().post(
            "/api/tokens", json={"label": "x"}).get_json()["token"]
        anon = self.app_module.app.test_client()
        hdr = {"Authorization": f"Bearer {token}"}
        self.assertEqual(401, anon.post("/api/tokens", json={"label": "y"},
                                        headers=hdr).status_code)
        self.assertEqual(401, anon.get("/api/tokens", headers=hdr).status_code)

    def test_unauthenticated_is_401(self):
        anon = self.app_module.app.test_client()
        self.assertEqual(401, anon.post("/api/tokens", json={"label": "x"}).status_code)


if __name__ == "__main__":
    unittest.main()
