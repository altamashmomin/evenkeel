"""Bearer-token authentication: a read token can GET the read tier but not
write (scope enforced by HTTP method), revoked/garbage/no tokens are 401,
and a token identity is attributed as 'mcp:<label>' in the audit trail."""
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


class BearerAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-bearer-test-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "72", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "bearer-test-secret")
        spec = importlib.util.spec_from_file_location(
            "app_bearer_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def mint(self, scopes="read", label="agent"):
        """Mint directly via the verb (write tokens aren't issuable via the
        route yet), returning the plaintext."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        import actions
        token = actions.create_api_token(
            conn, "ui:avery", {"label": label, "user_id": 1, "scopes": scopes})["token"]
        conn.close()
        return token

    def client(self):
        return self.app_module.app.test_client()

    def bearer(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_read_token_can_get_the_read_tier(self):
        resp = self.client().get("/api/household_snapshot",
                                 headers=self.bearer(self.mint()))
        self.assertEqual(200, resp.status_code)
        self.assertIn("spend", resp.get_json())

    def test_read_token_cannot_write(self):
        resp = self.client().post("/api/transactions",
            json={"date": date.today().isoformat(), "amount": 10,
                  "description": "x", "category": "Dining", "paid_by": 1,
                  "is_shared": False},
            headers=self.bearer(self.mint()))
        self.assertEqual(403, resp.status_code)
        self.assertIn("write", resp.get_json()["error"])

    def test_revoked_token_is_401(self):
        conn = sqlite3.connect(self.db_path); conn.row_factory = sqlite3.Row
        import actions
        created = actions.create_api_token(
            conn, "ui:avery", {"label": "x", "user_id": 1})
        actions.revoke_api_token(conn, "ui:avery", created["id"])
        conn.close()
        resp = self.client().get("/api/household_snapshot",
                                 headers=self.bearer(created["token"]))
        self.assertEqual(401, resp.status_code)

    def test_garbage_and_missing_tokens_are_401(self):
        self.assertEqual(401, self.client().get(
            "/api/household_snapshot", headers=self.bearer("nope")).status_code)
        self.assertEqual(401, self.client().get(
            "/api/household_snapshot").status_code)

    def test_write_token_is_attributed_as_mcp_label_in_audit(self):
        token = self.mint(scopes="read,write", label="claude-code")
        resp = self.client().post("/api/transactions",
            json={"date": date.today().isoformat(), "amount": 10,
                  "description": "agent entry", "category": "Dining",
                  "paid_by": 1, "is_shared": False},
            headers=self.bearer(token))
        self.assertEqual(201, resp.status_code)
        conn = sqlite3.connect(self.db_path)
        actor = conn.execute(
            "SELECT actor FROM audit_log WHERE action = 'record_transaction' "
            "ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.close()
        self.assertEqual("mcp:claude-code", actor)


if __name__ == "__main__":
    unittest.main()
