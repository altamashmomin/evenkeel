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
        # A1: a landed write carries a tap-through chip to where it lives.
        self.assertEqual([{"tab": "activity", "label": "Review in Activity"}],
                         out["actions"])
        # the row really changed, through the route
        self.assertEqual("paycheck", self.income_type(txn_id))
        # the model saw a NON-error tool_result carrying the updated row
        tr = mock.calls[1]["messages"][-1]["content"][0]
        self.assertFalse(tr["is_error"])
        # #7: the payload is labeled untrusted, then the JSON follows.
        self.assertTrue(tr["content"].startswith("[untrusted tool data"))
        self.assertEqual("paycheck",
                         json.loads(tr["content"].split("\n", 1)[1])["income_type"])
        # attributed to the person, not an mcp token
        conn = self.db()
        try:
            audit = conn.execute(
                "SELECT actor FROM audit_log WHERE action = 'classify_inflow' "
                "AND target = ?", (f"transaction:{txn_id}",)).fetchone()
        finally:
            conn.close()
        self.assertEqual("ui:avery", audit["actor"])

    # -- B1: recategorize a spending row through the tool --------------------
    def a_spending_row(self):
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT id, category, amount_cents FROM transactions "
                "WHERE direction = 'out' AND source != 'settlement' "
                "ORDER BY id LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "seed should have spending rows")
        return row

    def category_of(self, txn_id):
        conn = self.db()
        try:
            return conn.execute(
                "SELECT category, amount_cents FROM transactions WHERE id = ?",
                (txn_id,)).fetchone()
        finally:
            conn.close()

    def test_recategorize_tool_relabels_only_and_logs_as_the_person(self):
        row = self.a_spending_row()
        target = "Household" if row["category"] != "Household" else "Dining"
        mock = MockAnthropic([
            resp([tool_block("ledger_recategorize_transaction",
                             {"transaction_id": row["id"], "category": target})], "tool_use"),
            resp([text_block(f"Moved it to {target} ✓")], "end_turn"),
        ])
        out = self.ask(mock, msg=f"that charge was {target}, not {row['category']}",
                       caller=self.caller)
        self.assertEqual(["ledger_recategorize_transaction"], out["tools_used"])
        # A1: a recategorize is reviewable in Activity.
        self.assertEqual([{"tab": "activity", "label": "Review in Activity"}],
                         out["actions"])
        after = self.category_of(row["id"])
        self.assertEqual(target, after["category"])          # label moved
        self.assertEqual(row["amount_cents"], after["amount_cents"])  # amount did NOT
        # audited as the person, via the edit_transaction write path
        conn = self.db()
        try:
            audit = conn.execute(
                "SELECT actor, detail_json FROM audit_log "
                "WHERE action = 'edit_transaction' AND target = ?",
                (f"transaction:{row['id']}",)).fetchone()
        finally:
            conn.close()
        self.assertEqual("ui:avery", audit["actor"])
        self.assertEqual({"category": target}, json.loads(audit["detail_json"])["changed"])

    def test_recategorize_tool_schema_is_category_only(self):
        # The structural guarantee: the tool can't reach amount/splits/etc — its
        # schema (from PARAM_SPECS) admits only transaction_id + category.
        import agent_write_tools
        spec = {t["name"]: t for t in agent_write_tools.WRITE_TOOLS}[
            "ledger_recategorize_transaction"]
        props = spec["input_schema"]["properties"]
        self.assertEqual({"transaction_id", "category"}, set(props))
        self.assertFalse(spec["input_schema"]["additionalProperties"])

    # -- pantry writes: the effect through the same routes -------------------
    def a_staple(self, name="Coffee"):
        c = self.app_module.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = 1
        r = c.post("/api/inventory", json={"name": name, "kind": "staple"})
        self.assertEqual(201, r.status_code)
        return r.get_json()["id"]

    def item_row(self, item_id):
        conn = self.db()
        try:
            return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        finally:
            conn.close()

    def audited(self, action, item_id):
        conn = self.db()
        try:
            return conn.execute(
                "SELECT actor FROM audit_log WHERE action = ? AND target = ?",
                (action, f"item:{item_id}")).fetchone()
        finally:
            conn.close()

    def test_archive_tool_removes_the_item_and_logs_as_the_person(self):
        item_id = self.a_staple()
        mock = MockAnthropic([
            resp([tool_block("ledger_archive_item", {"item_id": item_id})], "tool_use"),
            resp([text_block("Removed Coffee from the pantry ✓")], "end_turn"),
        ])
        out = self.ask(mock, msg="stop tracking coffee", caller=self.caller)
        self.assertEqual(["ledger_archive_item"], out["tools_used"])
        self.assertEqual(0, self.item_row(item_id)["active"])          # soft-deleted
        self.assertEqual("ui:avery", self.audited("archive_item", item_id)["actor"])

    def test_restock_tool_marks_the_set_stocked_as_the_person(self):
        coffee = self.a_staple()
        milk = self.a_staple(name="Milk")
        mock = MockAnthropic([
            resp([tool_block("ledger_restock_items",
                             {"item_ids": [coffee, milk]})], "tool_use"),
            resp([text_block("Marked Coffee and Milk stocked ✓")], "end_turn"),
        ])
        out = self.ask(mock, msg="we got the coffee and milk", caller=self.caller)
        self.assertEqual(["ledger_restock_items"], out["tools_used"])
        # A1: a pantry write lands one "Open Pantry" chip (deduped by tab).
        self.assertEqual([{"tab": "inventory", "label": "Open Pantry"}],
                         out["actions"])
        self.assertEqual("stocked", self.item_row(coffee)["status"])
        self.assertEqual("stocked", self.item_row(milk)["status"])
        self.assertEqual("ui:avery", self.audited("restock_items", coffee)["actor"])
        self.assertEqual("ui:avery", self.audited("restock_items", milk)["actor"])

    def test_snooze_tool_sets_the_pause_as_the_person(self):
        item_id = self.a_staple()
        mock = MockAnthropic([
            resp([tool_block("ledger_set_item_snooze",
                             {"item_id": item_id, "until": "2026-08-30"})], "tool_use"),
            resp([text_block("Snoozed Coffee until Aug 30 ✓")], "end_turn"),
        ])
        out = self.ask(mock, msg="stop reminding us about coffee until the 30th",
                       caller=self.caller)
        self.assertEqual(["ledger_set_item_snooze"], out["tools_used"])
        self.assertEqual("2026-08-30", self.item_row(item_id)["snoozed_until"])
        self.assertEqual("ui:avery", self.audited("set_item_snooze", item_id)["actor"])

    def test_set_match_tool_sets_the_phrase_as_the_person(self):
        item_id = self.a_staple()
        mock = MockAnthropic([
            resp([tool_block("ledger_set_item_match",
                             {"item_id": item_id, "restock_match": "chewy"})], "tool_use"),
            resp([text_block("Matched Coffee to 'chewy' ✓")], "end_turn"),
        ])
        out = self.ask(mock, msg="match coffee to chewy", caller=self.caller)
        self.assertEqual(["ledger_set_item_match"], out["tools_used"])
        self.assertEqual("chewy", self.item_row(item_id)["restock_match"])
        self.assertEqual("ui:avery", self.audited("set_item_match", item_id)["actor"])

    def test_set_interval_tool_sets_the_cadence_as_the_person(self):
        item_id = self.a_staple()
        mock = MockAnthropic([
            resp([tool_block("ledger_set_item_interval",
                             {"item_id": item_id, "days": 14})], "tool_use"),
            resp([text_block("I'll remind you to restock Coffee every 14 days ✓")], "end_turn"),
        ])
        out = self.ask(mock, msg="remind me to restock coffee every two weeks",
                       caller=self.caller)
        self.assertEqual(["ledger_set_item_interval"], out["tools_used"])
        self.assertEqual(14, self.item_row(item_id)["restock_interval_days"])
        self.assertEqual("ui:avery", self.audited("set_item_interval", item_id)["actor"])

    def test_set_interval_bad_value_is_caught(self):
        item_id = self.a_staple()
        mock = MockAnthropic([
            resp([tool_block("ledger_set_item_interval",
                             {"item_id": item_id, "days": 9999})], "tool_use"),  # out of 1..365
            resp([text_block("That's too long — pick something up to a year.")], "end_turn"),
        ])
        out = self.ask(mock, msg="restock coffee every 9999 days", caller=self.caller)
        self.assertEqual(["ledger_set_item_interval"], out["tools_used"])   # attempted
        self.assertIsNone(self.item_row(item_id)["restock_interval_days"])  # verb rejected it

    # -- write tools are offered only with a caller --------------------------
    def test_write_tools_present_only_when_caller_given(self):
        read_only = MockAnthropic([resp([text_block("hi")], "end_turn")])
        self.ask(read_only)  # no caller
        self.assertEqual(20, len(read_only.calls[0]["tools"]))

        with_write = MockAnthropic([resp([text_block("hi")], "end_turn")])
        self.ask(with_write, caller=self.caller)
        tools = with_write.calls[0]["tools"]
        self.assertEqual(31, len(tools))  # 20 read + 11 write
        names = [t["name"] for t in tools]
        self.assertIn("ledger_classify_inflow", names)
        self.assertIn("ledger_recategorize_transaction", names)
        self.assertIn("ledger_add_item", names)
        self.assertIn("ledger_set_item_status", names)
        self.assertIn("ledger_set_item_interval", names)
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
        # A1: a write that FAILED changed nothing, so it earns no chip.
        self.assertEqual([], out["actions"])

    # -- a plain read answer carries no tap-through chips (A1) ----------------
    def test_read_only_answer_has_no_actions(self):
        mock = MockAnthropic([resp([text_block("You're doing great this month.")],
                                   "end_turn")])
        out = self.ask(mock, caller=self.caller)
        self.assertEqual([], out["tools_used"])
        self.assertEqual([], out["actions"])

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

    # -- pantry: add_item really creates a row, logged as the person ----------
    def test_add_item_tool_creates_a_row_and_logs_as_the_person(self):
        mock = MockAnthropic([
            resp([tool_block("ledger_add_item",
                             {"name": "Coffee", "kind": "staple", "status": "low"})],
                 "tool_use"),
            resp([text_block("Added coffee to your staples (marked low) ✓")], "end_turn"),
        ])
        out = self.ask(mock, msg="add coffee, we're low", caller=self.caller)
        self.assertEqual(["ledger_add_item"], out["tools_used"])
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT * FROM items WHERE name = 'Coffee'").fetchone()
            audit = conn.execute(
                "SELECT actor FROM audit_log WHERE action = 'add_item' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "the item was really created")
        self.assertEqual("staple", row["kind"])
        self.assertEqual("low", row["status"])
        self.assertEqual("ui:avery", audit["actor"])

    # -- pantry: set_item_status flips an existing item -----------------------
    def test_set_item_status_tool_flips_an_item(self):
        # seed one item through the verb (same path the app uses)
        import actions
        conn = self.db()
        try:
            item = actions.add_item(conn, "ui:avery", {"name": "Milk", "kind": "staple"})
            conn.commit()
            item_id = item["id"]
        finally:
            conn.close()
        mock = MockAnthropic([
            resp([tool_block("ledger_set_item_status",
                             {"item_id": item_id, "status": "out"})], "tool_use"),
            resp([text_block("Marked milk as out ✓")], "end_turn"),
        ])
        out = self.ask(mock, msg="we're out of milk", caller=self.caller)
        self.assertEqual(["ledger_set_item_status"], out["tools_used"])
        conn = self.db()
        try:
            status = conn.execute(
                "SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual("out", status)

    # -- pantry: a bad item id is recoverable, not a crash --------------------
    def test_set_item_status_bad_id_is_caught(self):
        mock = MockAnthropic([
            resp([tool_block("ledger_set_item_status",
                             {"item_id": 999999, "status": "out"})], "tool_use"),
            resp([text_block("I don't see that item — what's it called?")], "end_turn"),
        ])
        out = self.ask(mock, msg="mark it out", caller=self.caller)  # must not raise
        tr = mock.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(tr["is_error"])  # 404 from the verb → recoverable


if __name__ == "__main__":
    unittest.main()
