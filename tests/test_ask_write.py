"""Ask-tab WRITE tier, increment 1 (AGENT-DESIGN "Ask tab — write (tagging)"):
the loop can TAG an inflow when given a write `caller`, driven by a MOCKED
Anthropic client (no key, no live calls). The load-bearing assertion is the
real effect: the tool call flips the row's income_type in the db THROUGH the
same Flask route the SPA uses, attributed to the caller (`ui:avery`). Also:
write tools appear only when a caller is passed (read-only stays the default),
and a bad write is caught and handed back as a recoverable tool error."""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import ask_loop  # noqa: E402

SEED_AS_OF = "2026-07-19"


def text_block(t):
    return NS(type="text", text=t)


def tool_block(name, args, id="tu_1"):
    return NS(type="tool_use", id=id, name=name, input=args)


def resp(blocks, stop):
    return NS(content=blocks, stop_reason=stop)


class MockAnthropic:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = NS(create=self._create)

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._responses.pop(0)


class AskWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-askwrite-")
        self.db_path = Path(self.tmp.name) / "route.db"
        for cmd in (
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "72", "--months", "3", "--as-of", SEED_AS_OF],
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            [sys.executable, str(REPO / "seed_income.py"), str(self.db_path)],
        ):
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "askwrite-test-secret")
        spec = importlib.util.spec_from_file_location("app_askwrite_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True
        self.getter = ask_loop.make_app_getter(self.app_module.app, 1)
        self.caller = ask_loop.make_app_caller(self.app_module.app, 1)

    def tearDown(self):
        self.tmp.cleanup()

    def db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def an_unclassified_inflow(self):
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT id FROM transactions WHERE direction = 'in' "
                "AND income_type = 'unclassified' ORDER BY id LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "seed_income should leave some unclassified inflows")
        return row["id"]

    def income_type(self, txn_id):
        conn = self.db()
        try:
            return conn.execute(
                "SELECT income_type FROM transactions WHERE id = ?",
                (txn_id,)).fetchone()["income_type"]
        finally:
            conn.close()

    def ask(self, mock, msg="that deposit was my paycheck", **kw):
        return ask_loop.run_ask(mock, self.getter, msg,
                                model="claude-haiku-4-5", system="be helpful", **kw)

    # -- the effect: a tool call actually tags the row -----------------------
    def test_classify_tool_flips_the_row_and_logs_as_the_person(self):
        txn_id = self.an_unclassified_inflow()
        mock = MockAnthropic([
            resp([tool_block("ledger_classify_inflow",
                             {"transaction_id": txn_id, "income_type": "paycheck"})],
                 "tool_use"),
            resp([text_block("Tagged that deposit as your paycheck ✓")], "end_turn"),
        ])
        out = self.ask(mock, caller=self.caller)

        self.assertEqual("Tagged that deposit as your paycheck ✓", out["answer"])
        self.assertEqual(["ledger_classify_inflow"], out["tools_used"])
        # the row really changed, through the route
        self.assertEqual("paycheck", self.income_type(txn_id))
        # the model saw a NON-error tool_result carrying the updated row
        tr = mock.calls[1]["messages"][-1]["content"][0]
        self.assertFalse(tr["is_error"])
        self.assertEqual("paycheck", json.loads(tr["content"])["income_type"])
        # attributed to the person, not an mcp token
        conn = self.db()
        try:
            audit = conn.execute(
                "SELECT actor FROM audit_log WHERE action = 'classify_inflow' "
                "AND target = ?", (f"transaction:{txn_id}",)).fetchone()
        finally:
            conn.close()
        self.assertEqual("ui:avery", audit["actor"])

    # -- write tools are offered only with a caller --------------------------
    def test_write_tools_present_only_when_caller_given(self):
        read_only = MockAnthropic([resp([text_block("hi")], "end_turn")])
        self.ask(read_only)  # no caller
        self.assertEqual(13, len(read_only.calls[0]["tools"]))

        with_write = MockAnthropic([resp([text_block("hi")], "end_turn")])
        self.ask(with_write, caller=self.caller)
        tools = with_write.calls[0]["tools"]
        self.assertEqual(14, len(tools))
        self.assertIn("ledger_classify_inflow", [t["name"] for t in tools])
        # exactly one prompt-cache breakpoint, on the last (write) tool
        cached = [t for t in tools if "cache_control" in t]
        self.assertEqual([tools[-1]], cached)

    # -- a bad write is recoverable, not a crash -----------------------------
    def test_bad_write_is_caught_and_recovered(self):
        mock = MockAnthropic([
            resp([tool_block("ledger_classify_inflow",
                             {"transaction_id": 999999, "income_type": "paycheck"})],
                 "tool_use"),
            resp([text_block("I couldn't find that one — which deposit?")], "end_turn"),
        ])
        out = self.ask(mock, caller=self.caller)  # must not raise
        self.assertEqual("I couldn't find that one — which deposit?", out["answer"])
        tr = mock.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(tr["is_error"])
        self.assertIn("tool error", tr["content"])

    # -- an outflow can't be tagged (the verb's submission criterion holds) ---
    def test_outflow_write_is_refused_by_the_verb(self):
        conn = self.db()
        try:
            out_id = conn.execute(
                "SELECT id FROM transactions WHERE direction = 'out' "
                "ORDER BY id LIMIT 1").fetchone()["id"]
        finally:
            conn.close()
        mock = MockAnthropic([
            resp([tool_block("ledger_classify_inflow",
                             {"transaction_id": out_id, "income_type": "paycheck"})],
                 "tool_use"),
            resp([text_block("That one's a purchase, not income.")], "end_turn"),
        ])
        out = self.ask(mock, caller=self.caller)
        tr = mock.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(tr["is_error"])  # 400 from the verb → recoverable
        self.assertEqual("out", self.income_direction_out(out_id))

    def income_direction_out(self, txn_id):
        conn = self.db()
        try:
            return conn.execute(
                "SELECT direction FROM transactions WHERE id = ?",
                (txn_id,)).fetchone()["direction"]
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
