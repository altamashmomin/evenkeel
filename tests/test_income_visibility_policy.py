"""Income-visibility policy: FULL TRANSPARENCY, ratified.

The household's settled policy (INCOME-DESIGN open problem #1 / AGENT-DESIGN
"income visibility question") is that both members see all income — the same
pooled visibility the rest of the app already has. AGENT-DESIGN fixes *where*
any such policy must live: enforced in the Flask API, keyed to the
authenticated identity (`g.auth["user_id"]`), so every door — the SPA session,
an MCP/agent bearer token, the Ask loop — inherits it. The chosen policy is the
absence of per-owner filtering, so this test pins that absence as a contract.

It is deliberately load-bearing, not a tautology: it seeds a paycheck owned by
member 1 (avery) and another owned by member 2 (blake), then views the income
surfaces as blake — through BOTH doors — and asserts blake sees avery's income
and the household total. The day someone adds `WHERE paid_by = <viewer>` (or any
owner-scoping) to an income surface, this fails, forcing that change to be a
deliberate policy decision rather than a silent one.
"""
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
M = "2026-06"

AVERY, BLAKE = 1, 2
AVERY_PAY, BLAKE_PAY = 400000, 250000  # cents; distinct so a scoped total differs


class IncomeVisibilityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-income-vis-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=93, months=1)

        # Start from a clean slate so the household total is exactly the two
        # paychecks we seed, owned by two different members.
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM splits")
        conn.execute("DELETE FROM transactions")
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, ?, 'Avery paycheck', 'Other', ?, 0, 'simplefin',
                   'in', 'paycheck')""",
            (f"{M}-05", AVERY_PAY, AVERY))
        conn.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, ?, 'Blake paycheck', 'Other', ?, 0, 'simplefin',
                   'in', 'paycheck')""",
            (f"{M}-06", BLAKE_PAY, BLAKE))
        conn.commit()
        conn.close()

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "income-vis-secret")
        spec = importlib.util.spec_from_file_location(
            "app_income_vis_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def session_client(self, user_id):
        client = self.app_module.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
        return client

    def read_token_for(self, user_id, label="agent"):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        import actions
        token = actions.create_api_token(
            conn, f"ui:{user_id}",
            {"label": label, "user_id": user_id, "scopes": "read"})["token"]
        conn.close()
        return token

    # ---- the household total is everyone's income, to either viewer ----

    def test_income_summary_is_household_wide_for_the_other_member(self):
        """Blake's view of income includes Avery's paycheck (no owner scope)."""
        both = (AVERY_PAY + BLAKE_PAY) / 100.0
        resp = self.session_client(BLAKE).get(f"/api/income/summary?month={M}")
        self.assertEqual(200, resp.status_code)
        body = resp.get_json()
        self.assertEqual(both, body["true_income"])       # not just Blake's 2500
        self.assertEqual(both, body["gross_inflows"])

    def test_activity_income_feed_shows_the_other_members_inflow(self):
        """The income filter shows Avery's row to Blake — rows aren't scoped."""
        resp = self.session_client(BLAKE).get(f"/api/activity?month={M}&filter=income")
        self.assertEqual(200, resp.status_code)
        owners = {t["paid_by"] for t in resp.get_json()["transactions"]}
        self.assertEqual({AVERY, BLAKE}, owners)          # both members' income

    def test_read_token_inherits_full_visibility(self):
        """The MCP/agent door sees the same household total as the session —
        the policy lives at the API, so every door inherits it."""
        both = (AVERY_PAY + BLAKE_PAY) / 100.0
        token = self.read_token_for(BLAKE, label="blakes-agent")
        resp = self.app_module.app.test_client().get(
            f"/api/income/summary?month={M}",
            headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(200, resp.status_code)
        self.assertEqual(both, resp.get_json()["true_income"])


if __name__ == "__main__":
    unittest.main()
