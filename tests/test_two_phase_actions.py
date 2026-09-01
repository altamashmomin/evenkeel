"""The agent write tier's two-phase choreography (AGENT-DESIGN step 4):
propose_action parks a frozen payload + preview and returns a single-use
token; confirm_action executes exactly that frozen payload. Covers the
propose/confirm lifecycle, single-use + expiry enforcement, the new-rule-only
apply scope (a confirmed rule proposal classifies only its OWN matches, never
a full-queue pass), and the propose/confirm routes incl. bearer scope gating.
"""
import importlib.util
import json
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
sys.path.insert(0, str(REPO))

import actions


def _seed(db_path, seed):
    subprocess.run(
        [sys.executable, str(REPO / "seed_db.py"), str(db_path),
         "--seed", str(seed), "--months", "1", "--as-of", SEED_AS_OF],
        check=True, capture_output=True, text=True)
    subprocess.run(
        [sys.executable, str(REPO / "migrate.py"), "apply", str(db_path)],
        check=True, capture_output=True, text=True)


class TwoPhaseVerbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-twophase-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seed(self.db_path, 51)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def count(self, sql, *params):
        return self.db.execute(sql, params).fetchone()[0]

    def an_inflow(self, description="ADP PAYROLL 8842", amount_cents=320000,
                  external_id=None):
        cur = self.db.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source, external_id, direction, income_type)
               VALUES (?, ?, ?, 'Other', 1, 0, 'simplefin', ?, 'in', 'unclassified')""",
            (date.today().isoformat(), amount_cents, description, external_id))
        self.db.commit()
        return cur.lastrowid

    def pending(self, token):
        return self.db.execute(
            "SELECT * FROM pending_actions WHERE token = ?", (token,)).fetchone()

    # ------------------------------------------------------------ propose

    def test_propose_create_rule_parks_without_writing(self):
        txn_id = self.an_inflow()
        result = actions.propose_action(
            self.db, "mcp:cc", created_by=None, action_type="create_rule",
            payload={"match_desc": "ADP PAYROLL", "set_type": "paycheck"})

        # a token + a preview that saw the matching inflow
        self.assertIn("confirmation_token", result)
        self.assertEqual("create_rule", result["action_type"])
        self.assertEqual(1, result["preview"]["would_match_now"])
        self.assertEqual([txn_id],
                         [r["id"] for r in result["preview"]["sample_rows"]])

        # nothing financial happened: no rule, row still unclassified, no audit
        self.assertEqual(0, self.count("SELECT COUNT(*) FROM income_rules"))
        self.assertEqual("unclassified", self.db.execute(
            "SELECT income_type FROM transactions WHERE id = ?", (txn_id,)
        ).fetchone()["income_type"])
        self.assertEqual(0, self.count("SELECT COUNT(*) FROM audit_log"))

        # ... but the action is parked, pending, frozen
        row = self.pending(result["confirmation_token"])
        self.assertEqual("pending", row["status"])
        frozen = json.loads(row["payload_json"])
        self.assertEqual("ADP PAYROLL", frozen["match_desc"])
        self.assertEqual("paycheck", frozen["set_type"])
        self.assertTrue(frozen["also_apply_to_existing"])

    def test_propose_rejects_invalid_action_type(self):
        with self.assertRaisesRegex(actions.ActionError, "action_type must be one of"):
            actions.propose_action(self.db, "mcp:cc", None, "delete_everything", {})

    def test_propose_rejects_bad_rule_before_parking(self):
        # the same validator create runs — a hard duplicate-criteria conflict
        # is caught at propose, and nothing is parked.
        actions.create_income_rule(
            self.db, "ui:avery", {"match_desc": "ADP", "set_type": "paycheck"})
        with self.assertRaisesRegex(actions.ActionError, "already exists"):
            actions.propose_action(
                self.db, "mcp:cc", None, "create_rule",
                {"match_desc": "ADP", "set_type": "gift"})
        self.assertEqual(0, self.count("SELECT COUNT(*) FROM pending_actions"))

    # ------------------------------------------------ broad-rule warning (F3)

    def test_preview_flags_broad_transfer_rule(self):
        # A short transfer match with nothing else narrowing it matches NOTHING
        # right now — would_match_now is 0, so the count gives the human no
        # signal — yet it will silently mark FUTURE deposits (a paycheck next
        # cycle) as transfers, hiding them from both income and spending. The
        # preview must flag it broad regardless of the current-queue count.
        result = actions.propose_action(
            self.db, "mcp:cc", created_by=None, action_type="create_rule",
            payload={"match_desc": "zzq", "set_transfer": 1})
        preview = result["preview"]
        self.assertEqual(0, preview["would_match_now"])
        self.assertTrue(preview["broad"])
        self.assertIsNotNone(preview["warning"])
        self.assertIn("transfer", preview["warning"].lower())
        # the flag rides the FROZEN preview the human's confirm reads back
        parked = json.loads(
            self.pending(result["confirmation_token"])["preview_json"])
        self.assertTrue(parked["broad"])
        self.assertEqual(preview["warning"], parked["warning"])

    def test_preview_flags_broad_income_rule(self):
        # A short income match (no bounds, no account) over-matches future
        # inflows too — flagged, though with the milder non-transfer wording.
        result = actions.propose_action(
            self.db, "mcp:cc", created_by=None, action_type="create_rule",
            payload={"match_desc": "irs", "set_type": "paycheck"})
        self.assertTrue(result["preview"]["broad"])
        self.assertIn("future", result["preview"]["warning"].lower())

    def test_preview_does_not_flag_specific_rule(self):
        # A long, specific match classifies future inflows too — but that's the
        # intended, non-alarming case, so no warning.
        result = actions.propose_action(
            self.db, "mcp:cc", created_by=None, action_type="create_rule",
            payload={"match_desc": "ADP PAYROLL", "set_type": "paycheck"})
        self.assertFalse(result["preview"]["broad"])
        self.assertIsNone(result["preview"]["warning"])

    def test_preview_amount_bounds_narrow_a_short_match(self):
        # Amount bounds narrow a short match beyond a bare substring — not broad.
        result = actions.propose_action(
            self.db, "mcp:cc", created_by=None, action_type="create_rule",
            payload={"match_desc": "zzq", "set_type": "paycheck",
                     "min_cents": 300000})
        self.assertFalse(result["preview"]["broad"])
        self.assertIsNone(result["preview"]["warning"])

    def test_preview_mid_length_transfer_still_broad(self):
        # Transfer rules use a stricter floor: a 5-char transfer match (e.g.
        # 'venmo') is still broad, though the same length as an income match
        # would not be — a broad transfer rule hides money in both directions.
        transfer = actions.propose_action(
            self.db, "mcp:cc", created_by=None, action_type="create_rule",
            payload={"match_desc": "zzqxy", "set_transfer": 1})
        self.assertTrue(transfer["preview"]["broad"])
        income = actions.propose_action(
            self.db, "mcp:cc", created_by=None, action_type="create_rule",
            payload={"match_desc": "zzqxy", "set_type": "paycheck"})
        self.assertFalse(income["preview"]["broad"])

    # ------------------------------------------------------------ confirm

    def test_confirm_create_rule_creates_and_applies(self):
        txn_id = self.an_inflow()
        token = actions.propose_action(
            self.db, "mcp:cc", None, "create_rule",
            {"match_desc": "ADP PAYROLL", "set_type": "paycheck"}
        )["confirmation_token"]

        result = actions.confirm_action(self.db, "ui:avery", token)
        self.assertEqual("create_rule", result["action_type"])
        self.assertEqual("ADP PAYROLL", result["rule"]["match_desc"])
        self.assertEqual(1, len(result["applied"]))

        # rule created, row classified, hit_count bumped
        self.assertEqual("paycheck", self.db.execute(
            "SELECT income_type FROM transactions WHERE id = ?", (txn_id,)
        ).fetchone()["income_type"])
        self.assertEqual(1, self.count(
            "SELECT hit_count FROM income_rules WHERE id = ?",
            result["rule"]["id"]))
        # both underlying verbs left their audit rows
        self.assertEqual(1, self.count(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'create_income_rule'"))
        self.assertEqual(1, self.count(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'apply_rules'"))
        # the pending row is consumed
        self.assertEqual("confirmed", self.pending(token)["status"])

    def test_confirm_without_also_apply_leaves_existing_rows(self):
        txn_id = self.an_inflow()
        token = actions.propose_action(
            self.db, "mcp:cc", None, "create_rule",
            {"match_desc": "ADP PAYROLL", "set_type": "paycheck",
             "also_apply_to_existing": False}
        )["confirmation_token"]

        result = actions.confirm_action(self.db, "ui:avery", token)
        self.assertEqual([], result["applied"])
        self.assertEqual(1, self.count("SELECT COUNT(*) FROM income_rules"))
        self.assertEqual("unclassified", self.db.execute(
            "SELECT income_type FROM transactions WHERE id = ?", (txn_id,)
        ).fetchone()["income_type"])

    def test_confirm_applies_only_the_new_rule_not_a_full_pass(self):
        # An older ENABLED rule matches B but was never applied (rows inserted
        # directly stay unclassified). Confirming a NEW rule that matches A
        # must classify A only — B stays unclassified, proving the confirm is
        # scoped to the new rule, not a full apply_rules over the whole queue.
        actions.create_income_rule(
            self.db, "ui:avery", {"match_desc": "RENT", "set_type": "other"})
        a = self.an_inflow(description="ADP PAYROLL 8842")
        b = self.an_inflow(description="RENT DEPOSIT 55")

        token = actions.propose_action(
            self.db, "mcp:cc", None, "create_rule",
            {"match_desc": "ADP", "set_type": "paycheck"}
        )["confirmation_token"]
        result = actions.confirm_action(self.db, "ui:avery", token)

        self.assertEqual(1, len(result["applied"]))
        types = {r["id"]: r["income_type"] for r in self.db.execute(
            "SELECT id, income_type FROM transactions WHERE id IN (?, ?)", (a, b))}
        self.assertEqual("paycheck", types[a])
        self.assertEqual("unclassified", types[b])

    def test_confirm_is_single_use(self):
        token = actions.propose_action(
            self.db, "mcp:cc", None, "create_rule",
            {"match_desc": "ADP", "set_type": "paycheck"}
        )["confirmation_token"]
        actions.confirm_action(self.db, "ui:avery", token)
        with self.assertRaisesRegex(actions.ActionError, "already confirmed"):
            actions.confirm_action(self.db, "ui:avery", token)
        # exactly one rule, not two
        self.assertEqual(1, self.count("SELECT COUNT(*) FROM income_rules"))

    def test_confirm_expired_is_refused_and_creates_nothing(self):
        token = actions.propose_action(
            self.db, "mcp:cc", None, "create_rule",
            {"match_desc": "ADP", "set_type": "paycheck"}
        )["confirmation_token"]
        self.db.execute(
            "UPDATE pending_actions SET expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE token = ?", (token,))
        self.db.commit()
        with self.assertRaisesRegex(actions.ActionError, "expired"):
            actions.confirm_action(self.db, "ui:avery", token)
        self.assertEqual("expired", self.pending(token)["status"])
        self.assertEqual(0, self.count("SELECT COUNT(*) FROM income_rules"))

    def test_confirm_unknown_token_is_not_found(self):
        with self.assertRaises(actions.NotFound):
            actions.confirm_action(self.db, "ui:avery", "no-such-token")

    def test_mcp_actor_cannot_confirm_a_proposal(self):
        # MIRAGE F-1 backstop: an automation (actor 'mcp:*') may propose but not
        # confirm — a human does that in the app. Refused in the money layer,
        # independent of the route; the proposal stays pending for the human.
        token = actions.propose_action(
            self.db, "mcp:cc", None, "create_rule",
            {"match_desc": "ADP PAYROLL", "set_type": "paycheck"}
        )["confirmation_token"]
        with self.assertRaisesRegex(actions.ActionError, "signed-in person"):
            actions.confirm_action(self.db, "mcp:cc", token)
        # nothing executed; the proposal is still pending for a human to approve
        self.assertEqual(0, self.count("SELECT COUNT(*) FROM income_rules"))
        self.assertEqual("pending", self.pending(token)["status"])
        # ...and a human (ui:*) actor CAN confirm the very same token
        actions.confirm_action(self.db, "ui:avery", token)
        self.assertEqual(1, self.count("SELECT COUNT(*) FROM income_rules"))

    # ---------------------------------------- proposer binding (MIRAGE F2)

    def test_confirm_rejects_a_different_proposer(self):
        # Member 1 parks a proposal; member 2 must not be able to confirm it.
        # Refused as NotFound (indistinguishable from a bogus token), nothing
        # executed, and the row stays pending so the real proposer still can.
        txn_id = self.an_inflow()
        token = actions.propose_action(
            self.db, "ui:avery", created_by=None, action_type="create_rule",
            payload={"match_desc": "ADP PAYROLL", "set_type": "paycheck"},
            proposed_by_user=1)["confirmation_token"]

        with self.assertRaises(actions.NotFound):
            actions.confirm_action(self.db, "ui:blake", token, confirming_user=2)

        # nothing ran, and the proposal is still claimable
        self.assertEqual(0, self.count("SELECT COUNT(*) FROM income_rules"))
        self.assertEqual("unclassified", self.db.execute(
            "SELECT income_type FROM transactions WHERE id = ?", (txn_id,)
        ).fetchone()["income_type"])
        self.assertEqual("pending", self.pending(token)["status"])

        # the proposer themselves confirms fine
        result = actions.confirm_action(self.db, "ui:avery", token, confirming_user=1)
        self.assertEqual("create_rule", result["action_type"])
        self.assertEqual("confirmed", self.pending(token)["status"])

    def test_confirm_unbound_legacy_row_still_confirms(self):
        # A row parked without a proposer (legacy / pre-migration) is unbound:
        # any confirming identity may execute it — the check never blocks NULL.
        token = actions.propose_action(
            self.db, "mcp:cc", created_by=None, action_type="create_rule",
            payload={"match_desc": "ADP", "set_type": "paycheck"},
            proposed_by_user=None)["confirmation_token"]
        result = actions.confirm_action(self.db, "ui:blake", token, confirming_user=2)
        self.assertEqual("create_rule", result["action_type"])

    # ------------------------------------------------- apply_rules variant

    def test_propose_and_confirm_apply_rules(self):
        actions.create_income_rule(
            self.db, "ui:avery", {"match_desc": "ADP PAYROLL", "set_type": "paycheck"})
        txn_id = self.an_inflow()

        proposal = actions.propose_action(
            self.db, "mcp:cc", None, "apply_rules", {})
        self.assertEqual("apply_rules", proposal["action_type"])
        self.assertEqual(1, proposal["preview"]["rows_affected"])
        self.assertEqual([{"rule_id": 1, "count": 1}],
                         proposal["preview"]["by_rule"])
        # preview must not have written
        self.assertEqual("unclassified", self.db.execute(
            "SELECT income_type FROM transactions WHERE id = ?", (txn_id,)
        ).fetchone()["income_type"])

        result = actions.confirm_action(
            self.db, "ui:avery", proposal["confirmation_token"])
        self.assertEqual(1, len(result["applied"]))
        self.assertEqual("paycheck", self.db.execute(
            "SELECT income_type FROM transactions WHERE id = ?", (txn_id,)
        ).fetchone()["income_type"])

    def test_confirm_apply_rules_only_touches_the_previewed_rows(self):
        # The two-phase contract is "confirm executes exactly what was
        # previewed." A new unclassified inflow arriving between propose and
        # confirm (a sync during the ~24h approval window) must NOT be swept in:
        # confirm classifies only the id-set frozen at propose time
        # (CODE-REVIEW 2026-08-08 Tier 2 #6). Before the fix, confirm re-ran the
        # match live and classified both rows.
        actions.create_income_rule(
            self.db, "ui:avery", {"match_desc": "ADP PAYROLL", "set_type": "paycheck"})
        first = self.an_inflow(external_id="simplefin:acc:1")   # matches the rule

        proposal = actions.propose_action(self.db, "mcp:cc", None, "apply_rules", {})
        self.assertEqual(1, proposal["preview"]["rows_affected"])
        # the frozen payload pins the exact previewed id-set
        frozen = json.loads(self.pending(proposal["confirmation_token"])["payload_json"])
        self.assertEqual([first], frozen["transaction_ids"])

        # a sync lands a SECOND matching inflow before the human approves
        second = self.an_inflow(external_id="simplefin:acc:2")

        result = actions.confirm_action(
            self.db, "ui:avery", proposal["confirmation_token"])
        self.assertEqual(1, len(result["applied"]))             # the frozen row only, not 2
        self.assertEqual("paycheck", self.db.execute(
            "SELECT income_type FROM transactions WHERE id = ?", (first,)
        ).fetchone()["income_type"])
        self.assertEqual("unclassified", self.db.execute(       # the newcomer is untouched
            "SELECT income_type FROM transactions WHERE id = ?", (second,)
        ).fetchone()["income_type"])


class TwoPhaseRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-twophase-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seed(self.db_path, 61)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "twophase-route-secret")
        spec = importlib.util.spec_from_file_location(
            "app_twophase_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def session_client(self, user_id=1):
        client = self.app_module.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
        return client

    def token(self, scopes):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return actions.create_api_token(
                conn, "ui:avery",
                {"label": "cc", "user_id": 1, "scopes": scopes})["token"]
        finally:
            conn.close()

    def insert_inflow(self, description="ADP PAYROLL 8842"):
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source, direction, income_type)
               VALUES (?, 320000, ?, 'Other', 1, 0, 'simplefin', 'in', 'unclassified')""",
            (date.today().isoformat(), description))
        txn_id = cur.lastrowid
        conn.commit()
        conn.close()
        return txn_id

    def test_propose_then_confirm_happy_path(self):
        client = self.session_client()
        txn_id = self.insert_inflow()
        proposal = client.post("/api/actions/propose", json={
            "action_type": "create_rule",
            "match_desc": "ADP PAYROLL", "set_type": "paycheck"})
        self.assertEqual(201, proposal.status_code)
        token = proposal.get_json()["confirmation_token"]

        confirmed = client.post("/api/actions/confirm",
                                json={"confirmation_token": token})
        self.assertEqual(200, confirmed.status_code)
        self.assertEqual("create_rule", confirmed.get_json()["action_type"])

        conn = sqlite3.connect(self.db_path)
        try:
            income_type = conn.execute(
                "SELECT income_type FROM transactions WHERE id = ?",
                (txn_id,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("paycheck", income_type)

    def test_propose_requires_auth(self):
        anon = self.app_module.app.test_client()
        self.assertEqual(401, anon.post(
            "/api/actions/propose",
            json={"action_type": "apply_rules"}).status_code)

    def test_bearer_scope_gates_propose(self):
        read_token = self.token("read")
        write_token = self.token("read,write")
        client = self.app_module.app.test_client()

        denied = client.post(
            "/api/actions/propose", json={"action_type": "apply_rules"},
            headers={"Authorization": f"Bearer {read_token}"})
        self.assertEqual(403, denied.status_code)

        allowed = client.post(
            "/api/actions/propose", json={"action_type": "apply_rules"},
            headers={"Authorization": f"Bearer {write_token}"})
        self.assertEqual(201, allowed.status_code)

    def test_a_second_member_cannot_confirm_anothers_proposal(self):
        # The MIRAGE F2 scenario end to end: member 1 proposes; member 2's
        # session must not be able to confirm it. 404 (like a bogus token),
        # nothing executed; then member 1 confirms and it runs.
        txn_id = self.insert_inflow()
        proposer = self.session_client(user_id=1)
        proposal = proposer.post("/api/actions/propose", json={
            "action_type": "create_rule",
            "match_desc": "ADP PAYROLL", "set_type": "paycheck"})
        self.assertEqual(201, proposal.status_code)
        token = proposal.get_json()["confirmation_token"]

        intruder = self.session_client(user_id=2)
        denied = intruder.post("/api/actions/confirm",
                               json={"confirmation_token": token})
        self.assertEqual(404, denied.status_code)

        conn = sqlite3.connect(self.db_path)
        try:
            still_unclassified = conn.execute(
                "SELECT income_type FROM transactions WHERE id = ?",
                (txn_id,)).fetchone()[0]
            rule_count = conn.execute(
                "SELECT COUNT(*) FROM income_rules").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("unclassified", still_unclassified)
        self.assertEqual(0, rule_count)

        # the real proposer can still confirm
        ok = proposer.post("/api/actions/confirm",
                           json={"confirmation_token": token})
        self.assertEqual(200, ok.status_code)

    def test_confirm_missing_and_unknown_token(self):
        client = self.session_client()
        missing = client.post("/api/actions/confirm", json={})
        self.assertEqual(400, missing.status_code)
        unknown = client.post("/api/actions/confirm",
                              json={"confirmation_token": "nope"})
        self.assertEqual(404, unknown.status_code)

    def _income_type(self, txn_id):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT income_type FROM transactions WHERE id = ?",
                (txn_id,)).fetchone()[0]
        finally:
            conn.close()

    def test_bearer_cannot_confirm_but_a_session_can(self):
        # MIRAGE F-1: a bearer/MCP client PROPOSES; only a session (human)
        # CONFIRMS. Propose via a write bearer; bearer confirm -> 403; a session
        # confirm of the SAME token executes.
        write_token = self.token("read,write")
        client = self.app_module.app.test_client()
        txn_id = self.insert_inflow()
        proposal = client.post(
            "/api/actions/propose",
            json={"action_type": "create_rule",
                  "match_desc": "ADP PAYROLL", "set_type": "paycheck"},
            headers={"Authorization": f"Bearer {write_token}"})
        self.assertEqual(201, proposal.status_code)
        token = proposal.get_json()["confirmation_token"]

        denied = client.post(
            "/api/actions/confirm", json={"confirmation_token": token},
            headers={"Authorization": f"Bearer {write_token}"})
        self.assertEqual(403, denied.status_code)
        self.assertIn("signed-in person", denied.get_json()["error"])
        self.assertEqual("unclassified", self._income_type(txn_id))  # nothing ran

        ok = self.session_client().post(
            "/api/actions/confirm", json={"confirmation_token": token})
        self.assertEqual(200, ok.status_code)
        self.assertEqual("paycheck", self._income_type(txn_id))  # human approved

    def test_pending_queue_lists_a_bearer_proposal_for_the_human(self):
        write_token = self.token("read,write")
        client = self.app_module.app.test_client()
        self.insert_inflow()
        client.post(
            "/api/actions/propose",
            json={"action_type": "create_rule",
                  "match_desc": "ADP PAYROLL", "set_type": "paycheck"},
            headers={"Authorization": f"Bearer {write_token}"})

        listed = self.session_client().get("/api/actions/pending")
        self.assertEqual(200, listed.status_code)
        rows = listed.get_json()
        self.assertEqual(1, len(rows))
        self.assertIn("ADP PAYROLL", rows[0]["summary"])
        self.assertEqual("cc", rows[0]["proposed_by"])   # the token's label
        self.assertIn("token", rows[0])                  # so the human can approve

        # a bearer cannot read the approvals queue — approving is a human act
        denied = client.get(
            "/api/actions/pending",
            headers={"Authorization": f"Bearer {write_token}"})
        self.assertEqual(401, denied.status_code)


if __name__ == "__main__":
    unittest.main()
