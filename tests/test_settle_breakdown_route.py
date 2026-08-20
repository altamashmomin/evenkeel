"""GET /api/settle/breakdown: the settle-up screen's line-by-line explanation.
Money as {cents, display}; the signed line `owed` values sum to the same
figure /api/balance reports (the reconciliation contract, re-checked here at
the HTTP edge)."""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import _seedbase

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import actions  # noqa: E402


class SettleBreakdownRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-settle-bd-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=7, months=2)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "settle-bd-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_settle_bd_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, logged_in=True):
        client = self.app_module.app.test_client()
        if logged_in:
            with client.session_transaction() as session:
                session["user_id"] = 1
        return client

    def test_reconciles_to_the_balance_endpoint(self):
        c = self.client()
        bal = c.get("/api/balance").get_json()
        bd = c.get("/api/settle/breakdown").get_json()
        # The amount matches /api/balance exactly (taken from compute_balance).
        self.assertEqual(bal["amount"], bd["amount"]["cents"] / 100)
        # Reconciliation contract: signed lines + carryover == signed balance.
        signed = sum(ln["owed"]["cents"] for ln in bd["lines"])
        members = bd["members"]
        signed_bal = 0
        if bd["ower"] is not None:
            signed_bal = (bd["amount"]["cents"]
                          if bd["ower"]["id"] == members[1]["id"]
                          else -bd["amount"]["cents"])
        self.assertEqual(signed_bal, signed + bd["carryover"]["cents"])
        # Directional subtotals net to the itemized (pre-carryover) figure.
        self.assertEqual(
            bd["owed_to_first"]["cents"] - bd["owed_to_second"]["cents"], signed)

    def test_line_shape_and_money_at_the_edge(self):
        bd = self.client().get("/api/settle/breakdown").get_json()
        self.assertTrue(bd["lines"], "seed 7 should be owing with open expenses")
        ln = bd["lines"][0]
        self.assertEqual(
            ["amount", "category", "date", "description", "owed", "paid_by",
             "share_pct", "transaction_id"], sorted(ln))
        self.assertEqual(["cents", "display"], sorted(ln["amount"]))
        self.assertEqual(["id", "name"], sorted(ln["paid_by"]))
        self.assertIn("$", ln["owed"]["display"])

    def test_settled_household_has_empty_lines(self):
        c = self.client()
        # Settle the whole thing, then the breakdown is empty and settled.
        bal = c.get("/api/balance").get_json()
        c.post("/api/transactions", json={
            "date": date.today().isoformat(), "amount": bal["amount"],
            "description": "settle", "category": "Settlement",
            "paid_by": bal["owes"]["id"], "is_shared": True,
            "payer_share_pct": 0, "source": "settlement"})
        bd = c.get("/api/settle/breakdown").get_json()
        self.assertEqual("settled", bd["state"])
        self.assertEqual([], bd["lines"])
        self.assertEqual(0, bd["amount"]["cents"])

    def test_requires_login(self):
        self.assertEqual(401, self.client(logged_in=False)
                         .get("/api/settle/breakdown").status_code)


if __name__ == "__main__":
    unittest.main()
