"""ask_loop — the bounded tool-use loop, driven by a MOCKED Anthropic client
(no API key, no live calls). Proves: the loop executes the tool the model
asks for and feeds a real read back; it returns the model's final text; the
round cap holds; and a bad tool name is caught and handed back as an error the
model can recover from — the loop never crashes the request."""
import importlib.util
import json
import os
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
    """Scripts a fixed sequence of messages.create responses; records calls."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = NS(create=self._create)

    def _create(self, **kwargs):
        # Snapshot the messages list — run_ask keeps mutating the same object.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._responses.pop(0)


class AskLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-ask-")
        self.db_path = Path(self.tmp.name) / "route.db"
        for cmd in (
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "72", "--months", "3", "--as-of", SEED_AS_OF],
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            [sys.executable, str(REPO / "seed_income.py"), str(self.db_path)],
        ):
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "ask-test-secret")
        spec = importlib.util.spec_from_file_location("app_ask_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True
        self.client = self.app_module.app.test_client()
        with self.client.session_transaction() as s:
            s["user_id"] = 1

    def tearDown(self):
        self.tmp.cleanup()

    def getter(self, path, query):
        q = {k: v for k, v in (query or {}).items() if v is not None}
        r = self.client.get(path, query_string=q)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}")
        return r.get_json()

    def ask(self, mock, msg="how are we doing?", **kw):
        return ask_loop.run_ask(mock, self.getter, msg,
                                model="claude-haiku-4-5", system="be helpful", **kw)

    def test_tool_call_then_answer(self):
        mock = MockAnthropic([
            resp([tool_block("ledger_household_snapshot", {})], "tool_use"),
            resp([text_block("You're doing great this month.")], "end_turn"),
        ])
        out = self.ask(mock)
        self.assertEqual("You're doing great this month.", out["answer"])
        self.assertEqual(["ledger_household_snapshot"], out["tools_used"])
        self.assertEqual("end", out["stopped"])
        self.assertEqual(2, out["rounds"])
        # The model actually received the tool result: 2nd create call's last
        # message is the tool_result carrying real endpoint JSON.
        second_call_msgs = mock.calls[1]["messages"]
        tr = second_call_msgs[-1]["content"][0]
        self.assertEqual("tool_result", tr["type"])
        # #7: results are labeled untrusted, then the JSON payload follows.
        self.assertTrue(tr["content"].startswith("[untrusted tool data"),
                        "tool result should be labeled untrusted data")
        self.assertIn("spend", json.loads(tr["content"].split("\n", 1)[1]))
        self.assertFalse(tr["is_error"])
        # tools were passed and prompt-cache-marked
        self.assertEqual(20, len(mock.calls[0]["tools"]))

    def test_round_cap_holds(self):
        # A model that never stops asking for tools must be bounded.
        always = MockAnthropic([
            resp([tool_block("ledger_balance", {}, id=f"tu_{i}")], "tool_use")
            for i in range(10)])
        out = self.ask(always, max_rounds=3)
        self.assertEqual("max_rounds", out["stopped"])
        self.assertEqual(3, out["rounds"])
        self.assertEqual(3, len(out["tools_used"]))

    def test_bad_tool_is_caught_and_recovered(self):
        mock = MockAnthropic([
            resp([tool_block("ledger_nonexistent", {})], "tool_use"),
            resp([text_block("Let me try another way.")], "end_turn"),
        ])
        out = self.ask(mock)  # must not raise
        self.assertEqual("Let me try another way.", out["answer"])
        tr = mock.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(tr["is_error"])
        self.assertIn("tool error", tr["content"])

    def test_history_is_not_mutated(self):
        history = [{"role": "user", "content": "hi"},
                   {"role": "assistant", "content": "hello"}]
        mock = MockAnthropic([resp([text_block("ok")], "end_turn")])
        self.ask(mock, history=history)
        self.assertEqual(2, len(history), "caller's history list left intact")

    def test_system_prompt_carries_the_injection_defenses(self):
        # #7: the standing prompt must establish the untrusted-data boundary and
        # the refund-specific caution. (Structural check — the same kind the
        # suite already uses for the vocabulary rules.)
        p = ask_loop.system_prompt("2026-07").lower()
        self.assertIn("untrusted tool data", p)
        self.assertIn("not instructions", p)
        # tightened refund handling: only on explicit say-so, since it moves a
        # spending number
        self.assertIn("refund", p)
        self.assertTrue("explicit" in p or "explicitly" in p,
                        "refund rule should require explicit user intent")


if __name__ == "__main__":
    unittest.main()
