"""api-token verbs: create_api_token (hash-only storage, plaintext once,
audited, validated), revoke_api_token, and the find_active_api_token auth
helper. Security-sensitive, so the storage discipline is tested explicitly:
the plaintext token must never touch the database."""
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
sys.path.insert(0, str(REPO))

import _seedbase

import actions


class ApiTokenVerbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-token-verb-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=70, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_create_returns_plaintext_once_and_stores_only_the_hash(self):
        result = actions.create_api_token(
            self.db, "ui:avery", {"label": "claude-code", "user_id": 1})
        token = result["token"]
        self.assertTrue(token)
        row = self.db.execute(
            "SELECT * FROM api_tokens WHERE id = ?", (result["id"],)).fetchone()
        # The stored value is the hash, and the plaintext is nowhere in the row.
        self.assertEqual(hashlib.sha256(token.encode()).hexdigest(), row["token_hash"])
        self.assertNotIn(token, json.dumps({k: row[k] for k in row.keys()}))
        self.assertEqual("read", row["scopes"])
        self.assertEqual(1, row["user_id"])

    def test_create_audits_without_leaking_the_token(self):
        result = actions.create_api_token(
            self.db, "ui:avery", {"label": "laptop", "user_id": 1})
        audit = self.db.execute(
            "SELECT * FROM audit_log WHERE action = 'create_api_token'").fetchone()
        self.assertEqual("ui:avery", audit["actor"])
        self.assertEqual(f"api_token:{result['id']}", audit["target"])
        self.assertNotIn(result["token"], audit["detail_json"])
        detail = json.loads(audit["detail_json"])
        self.assertEqual({"label": "laptop", "user_id": 1, "scopes": "read"}, detail)

    def test_validation(self):
        with self.assertRaisesRegex(actions.ActionError, "label is required"):
            actions.create_api_token(self.db, "ui:avery", {"label": "", "user_id": 1})
        with self.assertRaisesRegex(actions.ActionError, "active member"):
            actions.create_api_token(self.db, "ui:avery", {"label": "x", "user_id": 999})
        with self.assertRaisesRegex(actions.ActionError, "scopes"):
            actions.create_api_token(
                self.db, "ui:avery", {"label": "x", "user_id": 1, "scopes": "admin"})

    def test_find_active_resolves_valid_bumps_last_used_and_rejects_bad(self):
        token = actions.create_api_token(
            self.db, "ui:avery", {"label": "x", "user_id": 1})["token"]
        row = actions.find_active_api_token(self.db, token)
        self.assertIsNotNone(row)
        persisted = self.db.execute(
            "SELECT last_used_at FROM api_tokens WHERE id = ?", (row["id"],)).fetchone()[0]
        self.assertIsNotNone(persisted)                    # bumped on use, persisted
        self.assertIsNone(actions.find_active_api_token(self.db, "not-a-real-token"))

    def test_revoke_disables_the_token_and_audits(self):
        created = actions.create_api_token(
            self.db, "ui:avery", {"label": "x", "user_id": 1})
        actions.revoke_api_token(self.db, "ui:avery", created["id"])
        # A revoked token no longer resolves.
        self.assertIsNone(actions.find_active_api_token(self.db, created["token"]))
        self.assertEqual(1, self.db.execute(
            "SELECT revoked FROM api_tokens WHERE id = ?", (created["id"],)).fetchone()[0])
        self.assertTrue(self.db.execute(
            "SELECT 1 FROM audit_log WHERE action = 'revoke_api_token'").fetchone())

    def test_revoke_missing_is_not_found(self):
        with self.assertRaises(actions.NotFound):
            actions.revoke_api_token(self.db, "ui:avery", 999999)

    def test_read_write_scope_is_accepted(self):
        row = actions.create_api_token(
            self.db, "ui:avery", {"label": "x", "user_id": 1, "scopes": "read,write"})
        self.assertEqual("read,write", row["scopes"])


if __name__ == "__main__":
    unittest.main()
