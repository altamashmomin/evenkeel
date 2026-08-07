"""reset_money — the fresh-start wipe (CLI-only Pi maintenance verb).

The load-bearing claims: (1) exactly the money-movement tables empty; (2) the
configured structure — members, bills, goals, budgets, income_rules, items,
api_tokens — is BYTE-untouched (full-row snapshot compare, not counts); (3) a
wrong confirm phrase deletes nothing; (4) the wipe writes one audit row with
per-table before-counts while audit history is kept; (5) the balance derivation
reads settled-zero afterwards; (6) no HTTP route reaches it."""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import actions        # noqa: E402
import derivations    # noqa: E402

SEED_AS_OF = "2026-07-19"

KEEP_TABLES = ("members", "bills", "goals", "budgets", "income_rules",
               "items", "api_tokens")


class ResetMoneyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-reset-")
        self.db_path = Path(self.tmp.name) / "reset.db"
        for cmd in (
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "83", "--months", "3", "--as-of", SEED_AS_OF],
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            [sys.executable, str(REPO / "seed_income.py"), str(self.db_path)],
        ):
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        # Give every keep-table real content so "untouched" is a real claim.
        actions.add_item(self.db, "ui:test", {"name": "Coffee"})
        actions.set_budget(self.db, "ui:test", {"category": "Groceries",
                                                "amount": "200"})
        actions.create_income_rule(self.db, "ui:test", {
            "name": "payroll", "match_desc": "ACME", "set_type": "paycheck"})
        self.db.execute("UPDATE income_rules SET hit_count = 7")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def snapshot(self, table):
        return [dict(r) for r in self.db.execute(
            f"SELECT * FROM {table} ORDER BY 1").fetchall()]

    def count(self, table):
        return self.db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]

    def test_wrong_phrase_deletes_nothing(self):
        before = self.count("transactions")
        self.assertGreater(before, 0)
        for bad in (None, "", "reset", "RESET ALL MONEY ROWS", "yes"):
            with self.assertRaises(actions.ActionError):
                actions.reset_money(self.db, "ui:avery", bad)
        self.assertEqual(before, self.count("transactions"))

    def test_reset_clears_money_and_keeps_structure_byte_identical(self):
        keep_before = {t: self.snapshot(t) for t in KEEP_TABLES}
        # normalize: hit_count will be zeroed by design — that is the ONE
        # allowed change inside a keep-table.
        for row in keep_before["income_rules"]:
            row["hit_count"] = 0
        audit_before = self.count("audit_log")
        self.assertGreater(self.count("transactions"), 0)
        self.assertGreater(self.count("splits"), 0)

        out = actions.reset_money(self.db, "ui:avery",
                                  actions.RESET_CONFIRM_PHRASE)

        for t in actions.RESET_TABLES:
            self.assertEqual(0, self.count(t), f"{t} should be empty")
        for t in KEEP_TABLES:
            self.assertEqual(keep_before[t], self.snapshot(t),
                             f"{t} should be byte-untouched")
        # audit kept + exactly one new row, carrying the before-counts
        self.assertEqual(audit_before + 1, self.count("audit_log"))
        row = self.db.execute(
            "SELECT actor, detail_json FROM audit_log WHERE action = 'reset_money'"
        ).fetchone()
        self.assertEqual("ui:avery", row["actor"])
        detail = json.loads(row["detail_json"])
        self.assertEqual(out["transactions"], detail["cleared"]["transactions"])
        self.assertGreater(detail["cleared"]["transactions"], 0)
        # the ledger now reads settled-zero
        bal = derivations.compute_balance(self.db)
        self.assertEqual(0, bal["amount_cents"])

    def test_no_route_reaches_reset(self):
        # The verb is CLI-only by design: no Flask route may call it.
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "reset-test-secret")
        spec = importlib.util.spec_from_file_location("app_reset_test", REPO / "app.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        src = (REPO / "app.py").read_text()
        self.assertNotIn("reset_money", src)


if __name__ == "__main__":
    unittest.main()
