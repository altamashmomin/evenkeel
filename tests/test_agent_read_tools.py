"""agent_read_tools — the shared read-tool registry both assistant doors use.

Proves: every tool bottoms out in a real read endpoint (200, dict back) via a
getter over a seeded app (the same getter shape the in-app Ask endpoint will
provide); the tool set matches ledger_mcp's (one surface, no drift); the
Anthropic tool schema is well-formed and prompt-cache-marked; an unknown tool
name raises rather than silently no-ops."""
import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import agent_read_tools as art  # noqa: E402

SEED_AS_OF = "2026-07-19"

# args that satisfy each tool's required inputs (category_trend needs one)
TOOL_ARGS = {
    "ledger_category_trend": {"category": "Groceries", "months_back": 3},
    "ledger_income_trend": {"months_back": 3},
    "ledger_savings_rate_trend": {"months_back": 3},
    "ledger_search_transactions": {"limit": 5},
}


class AgentReadToolsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-art-")
        self.db_path = Path(self.tmp.name) / "route.db"
        for cmd in (
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "72", "--months", "3", "--as-of", SEED_AS_OF],
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            [sys.executable, str(REPO / "seed_income.py"), str(self.db_path)],
        ):
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "art-test-secret")
        spec = importlib.util.spec_from_file_location("app_art_test", REPO / "app.py")
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
        resp = self.client.get(path, query_string=q)
        if resp.status_code >= 400:
            raise RuntimeError(f"{resp.status_code}: {resp.get_json()}")
        return resp.get_json()

    def test_every_tool_reaches_a_real_endpoint(self):
        for t in art.READ_TOOLS:
            data = art.call_read_tool(self.getter, t["name"], TOOL_ARGS.get(t["name"], {}))
            # a JSON value back (dict for most; a list for rules/goals endpoints)
            self.assertIsInstance(data, (dict, list), f"{t['name']} -> {type(data)}")

    def test_unclassified_inflows_are_in_and_unclassified(self):
        data = art.call_read_tool(self.getter, "ledger_unclassified_inflows", {})
        self.assertGreater(data["total_matches"], 0)  # seed_income makes some
        for tx in data["transactions"]:
            self.assertEqual("in", tx["direction"])
            self.assertEqual("unclassified", tx["income_type"])

    def test_goals_and_bills_merges_both(self):
        data = art.call_read_tool(self.getter, "ledger_list_goals_and_bills", {})
        self.assertEqual({"goals", "bills"}, set(data))
        self.assertEqual(self.getter("/api/goals", {}), data["goals"])

    def test_search_matches_the_endpoint(self):
        # call_read_tool bottoms out in the endpoint, so it can only reshape.
        via_tool = art.call_read_tool(self.getter, "ledger_search_transactions",
                                      {"direction": "out", "limit": 5})
        direct = self.getter("/api/transactions/search",
                             {"direction": "out", "limit": 5})
        self.assertEqual(direct, via_tool)

    def test_tool_set_matches_ledger_mcp(self):
        import asyncio
        import ledger_mcp
        mcp_tools = asyncio.run(ledger_mcp.mcp.list_tools())
        mcp_desc = {t.name: t.description for t in mcp_tools}
        # The READ tools share one description source with the Ask loop — no
        # drift. If someone re-adds a docstring to ledger_mcp, this fails. The
        # write tools are MCP-only (the Ask loop is read-only), so they live in
        # ledger_mcp and are excluded here rather than forced into the shared
        # read registry.
        self.assertTrue(set(art.DESCRIPTIONS) <= set(mcp_desc),
                        "a read tool is missing from ledger_mcp")
        read_desc = {n: d for n, d in mcp_desc.items() if n in art.DESCRIPTIONS}
        self.assertEqual(art.DESCRIPTIONS, read_desc,
                         "the two doors must share one copy of the read-tool docs")

    def test_anthropic_tools_schema_and_caching(self):
        tools = art.anthropic_tools()
        self.assertEqual(18, len(tools))
        for t in tools:
            self.assertEqual({"name", "description", "input_schema"} <= set(t), True)
            self.assertEqual("object", t["input_schema"]["type"])
        self.assertEqual({"type": "ephemeral"}, tools[-1]["cache_control"],
                         "last tool marks the cache breakpoint")
        self.assertNotIn("cache_control", art.anthropic_tools(cache=False)[-1])

    def test_unknown_tool_raises(self):
        with self.assertRaises(KeyError):
            art.call_read_tool(self.getter, "ledger_delete_everything", {})


if __name__ == "__main__":
    unittest.main()
