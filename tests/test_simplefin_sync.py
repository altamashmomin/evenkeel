"""simplefin_sync.py's insert path is now a thin caller of
record_transaction: dedupe on external_id, the legacy 50/50 split (money
out) or direction='in' handling and rule-matching (money in), and the
audit row all live in the verb (CORE-DESIGN's record_transaction entry —
"sync's insert path becomes a call to this; dedupe stays inside it").
The sync flip (INCOME-DESIGN build-order step 2) removed the old
deposit-skip branch: money in now inserts as an inflow instead of being
discarded."""
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeRequests:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, params=None, timeout=None):
        return FakeResponse(self.payload)


def simplefin_payload():
    posted = int(time.time())
    return {"accounts": [{"id": "acc1", "name": "Checking", "transactions": [
        {"id": "tx1", "amount": "-42.50", "posted": posted,
         "description": "Coffee Shop"},
        {"id": "tx-deposit", "amount": "500.00", "posted": posted,
         "description": "Paycheck"},
    ]}]}


class SimplefinSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-sync-test-")
        self.db_path = Path(self.tmp.name) / "sync.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "5", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        self.access_file = Path(self.tmp.name) / "access.url"
        self.access_file.write_text(
            "https://bridge.simplefin.org/simplefin/access/test\n")

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ["SIMPLEFIN_ACCESS_FILE"] = str(self.access_file)
        os.environ["SYNC_PAID_BY"] = "1"
        spec = importlib.util.spec_from_file_location(
            "simplefin_sync_test", REPO / "simplefin_sync.py")
        self.sync_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sync_module)

    def tearDown(self):
        for key in ("DATABASE_PATH", "SIMPLEFIN_ACCESS_FILE", "SYNC_PAID_BY"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def run_sync(self, payload, force=True):
        # force=True by default: these tests exercise sync LOGIC (insert,
        # dedupe) and call sync() repeatedly, so they bypass the min-interval
        # budget guard (tested separately in test_budget_guard_*).
        with mock.patch.object(self.sync_module, "requests", FakeRequests(payload)):
            self.sync_module.sync(force=force)

    def test_budget_guard_skips_a_too_soon_sync_and_force_overrides(self):
        # A fresh stamp -> the guard skips before any network call (FakeRequests
        # would raise if hit) and inserts nothing; --force runs anyway.
        with open(self.sync_module.STAMP_FILE, "w") as f:
            f.write(str(int(__import__("time").time())))
        before = len(self.query("SELECT id FROM transactions"))
        with mock.patch.object(self.sync_module, "requests",
                               FakeRequests(simplefin_payload())):
            self.sync_module.sync(force=False)           # too soon -> skip
        self.assertEqual(before, len(self.query("SELECT id FROM transactions")))
        self.run_sync(simplefin_payload(), force=True)   # override -> inserts
        self.assertGreater(len(self.query("SELECT id FROM transactions")), before)

    def query(self, sql, *params):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def count(self, sql, *params):
        return self.query(sql, *params)[0][0]

    def test_inserts_outflow_writes_audit_and_splits(self):
        self.run_sync(simplefin_payload())
        rows = self.query(
            "SELECT * FROM transactions WHERE external_id = ?",
            "simplefin:acc1:tx1")
        self.assertEqual(1, len(rows))
        txn = rows[0]
        self.assertEqual(4250, txn["amount_cents"])
        self.assertEqual("simplefin:acc1:tx1", txn["external_id"])
        self.assertEqual(1, txn["paid_by"])

        splits = self.query(
            "SELECT member_id, share_bp FROM splits WHERE transaction_id = ? "
            "ORDER BY member_id", txn["id"])
        self.assertEqual([5000, 5000], [s["share_bp"] for s in splits])

        audit = self.query(
            "SELECT actor, action FROM audit_log WHERE target = ?",
            f"transaction:{txn['id']}")
        self.assertEqual(
            [("sync", "record_transaction")],
            [(a["actor"], a["action"]) for a in audit])

    def test_rerun_dedupes_on_external_id_and_writes_no_extra_audit(self):
        self.run_sync(simplefin_payload())
        first_count = self.query(
            "SELECT COUNT(*) AS c FROM transactions")[0]["c"]
        self.run_sync(simplefin_payload())
        second_count = self.query(
            "SELECT COUNT(*) AS c FROM transactions")[0]["c"]
        self.assertEqual(first_count, second_count)

        # One audit row per row actually inserted on the first run (one
        # outflow, one inflow) -- the rerun's dedupe no-ops write none.
        audit_count = self.query(
            "SELECT COUNT(*) AS c FROM audit_log "
            "WHERE action = 'record_transaction'")[0]["c"]
        self.assertEqual(2, audit_count)

    def test_deposit_inserts_as_an_unclassified_inflow(self):
        self.run_sync(simplefin_payload())
        rows = self.query(
            "SELECT * FROM transactions WHERE external_id = ?",
            "simplefin:acc1:tx-deposit")
        self.assertEqual(1, len(rows))
        txn = rows[0]
        self.assertEqual("in", txn["direction"])
        self.assertEqual("unclassified", txn["income_type"])
        self.assertEqual(50000, txn["amount_cents"])
        self.assertEqual(1, txn["paid_by"])
        self.assertEqual(0, txn["is_shared"])

        splits = self.count(
            "SELECT COUNT(*) FROM splits WHERE transaction_id = ?", txn["id"])
        self.assertEqual(0, splits)

    def test_inflow_is_auto_classified_when_a_rule_matches(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO income_rules
               (priority, match_desc, set_type, enabled, created_at, hit_count)
               VALUES (0, 'Paycheck', 'paycheck', 1, '2026-07-19T00:00:00+00:00', 0)""")
        conn.commit()
        conn.close()

        self.run_sync(simplefin_payload())
        row = self.query(
            "SELECT income_type FROM transactions WHERE external_id = ?",
            "simplefin:acc1:tx-deposit")[0]
        self.assertEqual("paycheck", row["income_type"])
        hit_count = self.query(
            "SELECT hit_count FROM income_rules WHERE match_desc = 'Paycheck'"
        )[0]["hit_count"]
        self.assertEqual(1, hit_count)


if __name__ == "__main__":
    unittest.main()
