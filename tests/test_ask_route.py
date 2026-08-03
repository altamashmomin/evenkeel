"""POST /api/ask — Charlee's in-app assistant endpoint. Driven end-to-end
through a seeded app with a MOCKED Anthropic client (patched in at the client
factory, so no API key and no SDK are needed): a question runs the real
tool-use loop over the app's own read endpoints and returns the answer.
Also: empty question is a 400, a missing key is a 503, and the route is
session-only (a bearer token must never trigger paid API calls)."""
import importlib.util
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
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = NS(create=self._create)

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._responses.pop(0)


class AskRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-askroute-")
        self.db_path = Path(self.tmp.name) / "route.db"
        for cmd in (
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "72", "--months", "3", "--as-of", SEED_AS_OF],
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            [sys.executable, str(REPO / "seed_income.py"), str(self.db_path)],
        ):
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "askroute-secret")
        spec = importlib.util.spec_from_file_location("app_askroute_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True
        # save/patch the client factory + key so nothing hits the real SDK
        self._orig_factory = ask_loop._make_client
        self._orig_key = os.environ.get("ANTHROPIC_API_KEY")

    def tearDown(self):
        ask_loop._make_client = self._orig_factory
        if self._orig_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self._orig_key
        self.tmp.cleanup()

    def client(self, logged_in=True):
        c = self.app_module.app.test_client()
        if logged_in:
            with c.session_transaction() as s:
                s["user_id"] = 1
        return c

    def use_mock(self, mock):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        ask_loop._make_client = lambda key: mock

    def test_question_runs_the_loop_and_answers(self):
        mock = MockAnthropic([
            resp([tool_block("ledger_household_snapshot", {})], "tool_use"),
            resp([text_block("You're in good shape this month.")], "end_turn"),
        ])
        self.use_mock(mock)
        body = self.client().post("/api/ask", json={"message": "how are we doing?"})
        self.assertEqual(200, body.status_code)
        out = body.get_json()
        self.assertEqual("You're in good shape this month.", out["answer"])
        self.assertEqual(["ledger_household_snapshot"], out["tools_used"])
        # the loop fed the real endpoint's JSON back to the model
        tr = mock.calls[1]["messages"][-1]["content"][0]
        self.assertIn("spend", tr["content"])
        # the model saw the vocabulary system prompt
        self.assertIn("true_income", mock.calls[0]["system"])

    def test_tag_via_ask_writes_the_row(self):
        # The live route is now write-enabled: a classify tool_use through
        # POST /api/ask actually tags the row, under the session's identity.
        import sqlite3
        conn = sqlite3.connect(self.db_path); conn.row_factory = sqlite3.Row
        txn_id = conn.execute(
            "SELECT id FROM transactions WHERE direction = 'in' "
            "AND income_type = 'unclassified' ORDER BY id LIMIT 1").fetchone()["id"]
        conn.close()
        mock = MockAnthropic([
            resp([tool_block("ledger_classify_inflow",
                             {"transaction_id": txn_id, "income_type": "paycheck"})],
                 "tool_use"),
            resp([text_block("Tagged it as your paycheck ✓")], "end_turn"),
        ])
        self.use_mock(mock)
        r = self.client().post("/api/ask", json={"message": "that was my paycheck"})
        self.assertEqual(200, r.status_code)
        self.assertEqual(["ledger_classify_inflow"], r.get_json()["tools_used"])
        # the write tool was offered (14), and the prompt now grants tagging
        self.assertEqual(14, len(mock.calls[0]["tools"]))
        self.assertIn("ledger_classify_inflow", mock.calls[0]["system"])
        # the row really flipped, logged as the session's person
        conn = sqlite3.connect(self.db_path); conn.row_factory = sqlite3.Row
        try:
            self.assertEqual("paycheck", conn.execute(
                "SELECT income_type FROM transactions WHERE id = ?",
                (txn_id,)).fetchone()["income_type"])
            self.assertEqual("ui:avery", conn.execute(
                "SELECT actor FROM audit_log WHERE action = 'classify_inflow' "
                "AND target = ?", (f"transaction:{txn_id}",)).fetchone()["actor"])
        finally:
            conn.close()

    def test_empty_message_is_400(self):
        self.use_mock(MockAnthropic([]))
        self.assertEqual(400, self.client().post("/api/ask", json={"message": "  "}).status_code)

    def test_missing_key_is_503(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)  # not configured
        r = self.client().post("/api/ask", json={"message": "hi"})
        self.assertEqual(503, r.status_code)
        self.assertIn("isn't set up", r.get_json()["error"])

    def test_requires_a_session(self):
        self.use_mock(MockAnthropic([resp([text_block("x")], "end_turn")]))
        # no session cookie
        self.assertEqual(401, self.client(logged_in=False)
                         .post("/api/ask", json={"message": "hi"}).status_code)

    def test_bearer_token_cannot_use_ask(self):
        # A read token must never be able to spend on the Anthropic API.
        import sqlite3
        conn = sqlite3.connect(self.db_path); conn.row_factory = sqlite3.Row
        import actions
        token = actions.create_api_token(
            conn, "ui:avery", {"label": "x", "user_id": 1, "scopes": "read"})["token"]
        conn.close()
        self.use_mock(MockAnthropic([resp([text_block("x")], "end_turn")]))
        r = self.app_module.app.test_client().post(
            "/api/ask", json={"message": "hi"},
            headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(401, r.status_code)


if __name__ == "__main__":
    unittest.main()
