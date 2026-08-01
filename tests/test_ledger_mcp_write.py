"""ledger_mcp WRITE tier (AGENT-DESIGN step 4): two DIRECT writes
(classify one inflow, enable/disable one rule) + the TWO-PHASE tier
(propose_income_rule / apply_rules → confirm_action). Same seam as the read
tier — httpx WSGITransport at the Flask app in-process, tools driven through
FastMCP dispatch — but under a real `read,write` token, and each write's
EFFECT is checked against the database (the MCP layer only forwards to a verb;
these tests prove the verb ran). A `read` token is proven to be refused, so
the scope gate holds end to end."""
import asyncio
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

import httpx

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"

sys.path.insert(0, str(REPO))
import ledger_mcp  # noqa: E402
import actions  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

WRITE_TOOLS = {
    "ledger_classify_inflow", "ledger_set_rule_enabled",
    "ledger_propose_income_rule", "ledger_apply_rules", "ledger_confirm_action",
}


class LedgerMcpWriteTierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-mcp-write-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "72", "--months", "2", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)

        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "mcp-write-secret")
        spec = importlib.util.spec_from_file_location(
            "app_mcp_write_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.write_token = actions.create_api_token(
            conn, "ui:avery",
            {"label": "cc-write", "user_id": 1, "scopes": "read,write"})["token"]
        self.read_token = actions.create_api_token(
            conn, "ui:avery",
            {"label": "cc-read", "user_id": 1, "scopes": "read"})["token"]
        conn.close()

        os.environ["LEDGER_MCP_TOKEN"] = self.write_token
        ledger_mcp.set_client(httpx.Client(
            transport=httpx.WSGITransport(app=self.app_module.app),
            base_url="http://ledger.test"))

    def tearDown(self):
        ledger_mcp.set_client(None)
        os.environ.pop("LEDGER_MCP_TOKEN", None)
        self.tmp.cleanup()

    # -- helpers --------------------------------------------------------------
    def call(self, name, **args):
        _content, structured = asyncio.run(ledger_mcp.mcp.call_tool(name, args))
        return json.loads(structured["result"])

    def db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def insert_inflow(self, description="ADP PAYROLL 8842", amount_cents=320000):
        conn = self.db()
        cur = conn.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source, direction, income_type)
               VALUES (?, ?, ?, 'Other', 1, 0, 'simplefin', 'in', 'unclassified')""",
            (date.today().isoformat(), amount_cents, description))
        txn_id = cur.lastrowid
        conn.commit()
        conn.close()
        return txn_id

    def income_type(self, txn_id):
        conn = self.db()
        try:
            return conn.execute(
                "SELECT income_type FROM transactions WHERE id = ?",
                (txn_id,)).fetchone()["income_type"]
        finally:
            conn.close()

    # -- registration ---------------------------------------------------------
    def test_write_tools_registered_and_not_read_only(self):
        by_name = {t.name: t for t in asyncio.run(ledger_mcp.mcp.list_tools())}
        self.assertTrue(WRITE_TOOLS <= set(by_name),
                        "a write tool is missing from ledger_mcp")
        for name in WRITE_TOOLS:
            self.assertFalse(by_name[name].annotations.readOnlyHint,
                             f"{name} must not be marked read-only")

    # -- direct writes --------------------------------------------------------
    def test_classify_inflow_tags_one_row(self):
        txn_id = self.insert_inflow()
        result = self.call("ledger_classify_inflow",
                           transaction_id=txn_id, income_type="paycheck")
        self.assertEqual("paycheck", result["income_type"])
        self.assertEqual("paycheck", self.income_type(txn_id))

    def test_set_rule_enabled_toggles_one_rule(self):
        conn = self.db()
        rule_id = actions.create_income_rule(
            conn, "ui:avery", {"match_desc": "ADP", "set_type": "paycheck"})["id"]
        conn.close()
        result = self.call("ledger_set_rule_enabled",
                           rule_id=rule_id, enabled=False)
        self.assertFalse(result["enabled"])
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT enabled FROM income_rules WHERE id = ?",
                (rule_id,)).fetchone()["enabled"])
        finally:
            conn.close()

    # -- two-phase: rule ------------------------------------------------------
    def test_propose_rule_previews_then_confirm_creates_and_applies(self):
        txn_id = self.insert_inflow()
        proposal = self.call("ledger_propose_income_rule",
                             match_desc="ADP PAYROLL", set_type="paycheck")
        self.assertIn("confirmation_token", proposal)
        self.assertEqual(1, proposal["preview"]["would_match_now"])

        # PHASE 1 wrote nothing to the financial tables
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM income_rules").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual("unclassified", self.income_type(txn_id))

        # PHASE 2 executes exactly the frozen proposal
        result = self.call("ledger_confirm_action",
                           confirmation_token=proposal["confirmation_token"])
        self.assertEqual("create_rule", result["action_type"])
        self.assertEqual("ADP PAYROLL", result["rule"]["match_desc"])
        self.assertEqual("paycheck", self.income_type(txn_id))

    def test_confirm_is_single_use_through_the_tool(self):
        self.insert_inflow()
        token = self.call("ledger_propose_income_rule",
                          match_desc="ADP", set_type="paycheck"
                          )["confirmation_token"]
        self.call("ledger_confirm_action", confirmation_token=token)
        with self.assertRaises(ToolError) as ctx:
            self.call("ledger_confirm_action", confirmation_token=token)
        self.assertIn("already confirmed", str(ctx.exception))

    # -- two-phase: apply-rules backlog sweep ---------------------------------
    def test_apply_rules_propose_then_confirm(self):
        conn = self.db()
        actions.create_income_rule(
            conn, "ui:avery", {"match_desc": "ADP PAYROLL", "set_type": "paycheck"})
        conn.close()
        txn_id = self.insert_inflow()

        proposal = self.call("ledger_apply_rules")
        self.assertEqual(1, proposal["preview"]["rows_affected"])
        self.assertEqual("unclassified", self.income_type(txn_id))

        self.call("ledger_confirm_action",
                  confirmation_token=proposal["confirmation_token"])
        self.assertEqual("paycheck", self.income_type(txn_id))

    # -- scope gate holds end to end ------------------------------------------
    def test_read_token_is_refused_by_a_write_tool(self):
        os.environ["LEDGER_MCP_TOKEN"] = self.read_token
        txn_id = self.insert_inflow()
        with self.assertRaises(ToolError) as ctx:
            self.call("ledger_classify_inflow",
                      transaction_id=txn_id, income_type="paycheck")
        msg = str(ctx.exception)
        self.assertIn("403", msg)
        self.assertIn("write", msg.lower())
        # and nothing was written
        self.assertEqual("unclassified", self.income_type(txn_id))


if __name__ == "__main__":
    unittest.main()
