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


class AskWriteBase(unittest.TestCase):
    """The shared fixture: a seeded+income db, the app module loaded against
    it, and an in-process getter/caller for user 1. Subclasses hold the tests
    (kept in separate classes so a class's tests don't re-run under another's
    name)."""

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


class AskWriteTests(AskWriteBase):
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

    # -- B4: add a bill / a goal through the tools ---------------------------
    def test_add_bill_tool_creates_the_definition_as_the_person(self):
        mock = MockAnthropic([
            resp([tool_block("ledger_add_bill",
                             {"name": "Electric", "amount": 85.5, "due_day": 12})], "tool_use"),
            resp([text_block("Added the Electric bill ✓")], "end_turn"),
        ])
        out = self.ask(mock, msg="add our $85.50 electric bill due the 12th",
                       caller=self.caller)
        self.assertEqual(["ledger_add_bill"], out["tools_used"])
        # A1: adding a bill is reviewable in the Bills tab.
        self.assertEqual([{"tab": "bills", "label": "Open Bills"}], out["actions"])
        conn = self.db()
        try:
            # the row just created (highest id) — the seed also has an 'Electric'
            bill = conn.execute(
                "SELECT * FROM bills WHERE name = 'Electric' ORDER BY id DESC LIMIT 1").fetchone()
            audit = conn.execute(
                "SELECT actor FROM audit_log WHERE action = 'create_bill' AND target = ?",
                (f"bill:{bill['id']}",)).fetchone()
        finally:
            conn.close()
        self.assertEqual(8550, bill["amount_cents"])   # dollars → cents
        self.assertEqual(12, bill["due_day"])
        self.assertEqual("ui:avery", audit["actor"])

    def test_add_goal_tool_creates_the_target_as_the_person(self):
        mock = MockAnthropic([
            resp([tool_block("ledger_add_goal", {"name": "Vacation", "target": 2000})], "tool_use"),
            resp([text_block("Started the Vacation fund ✓")], "end_turn"),
        ])
        out = self.ask(mock, msg="start a $2,000 vacation fund", caller=self.caller)
        self.assertEqual(["ledger_add_goal"], out["tools_used"])
        # A1: adding a goal is reviewable in the Goals tab.
        self.assertEqual([{"tab": "goals", "label": "Open Goals"}], out["actions"])
        conn = self.db()
        try:
            goal = conn.execute(
                "SELECT * FROM goals WHERE name = 'Vacation' ORDER BY id DESC LIMIT 1").fetchone()
            audit = conn.execute(
                "SELECT actor FROM audit_log WHERE action = 'create_goal' AND target = ?",
                (f"goal:{goal['id']}",)).fetchone()
        finally:
            conn.close()
        self.assertEqual(200000, goal["target_cents"])
        self.assertEqual("ui:avery", audit["actor"])

    # -- B3: set a category budget through the tool --------------------------
    def budget_for(self, category):
        conn = self.db()
        try:
            return conn.execute(
                "SELECT * FROM budgets WHERE category = ?", (category,)).fetchone()
        finally:
            conn.close()

    def test_set_budget_tool_upserts_the_limit_and_logs_as_the_person(self):
        mock = MockAnthropic([
            resp([tool_block("ledger_set_budget",
                             {"category": "Groceries", "amount": 400})], "tool_use"),
            resp([text_block("Set your Groceries budget to $400/mo ✓")], "end_turn"),
        ])
        out = self.ask(mock, msg="budget $400 a month for groceries",
                       caller=self.caller)
        self.assertEqual(["ledger_set_budget"], out["tools_used"])
        # A1: a budget is reviewable in the Analytics tab.
        self.assertEqual([{"tab": "analytics", "label": "Open Analytics"}],
                         out["actions"])
        row = self.budget_for("Groceries")
        self.assertEqual(40000, row["amount_cents"])
        self.assertEqual(1, row["active"])
        conn = self.db()
        try:
            audit = conn.execute(
                "SELECT actor FROM audit_log WHERE action = 'set_budget' "
                "AND target = ?", (f"budget:{row['id']}",)).fetchone()
        finally:
            conn.close()
        self.assertEqual("ui:avery", audit["actor"])

    def test_set_budget_changes_an_existing_category_in_place(self):
        first = MockAnthropic([
            resp([tool_block("ledger_set_budget",
                             {"category": "Dining", "amount": 200})], "tool_use"),
            resp([text_block("Set ✓")], "end_turn")])
        self.ask(first, caller=self.caller)
        row1 = self.budget_for("Dining")
        second = MockAnthropic([
            resp([tool_block("ledger_set_budget",
                             {"category": "Dining", "amount": 250})], "tool_use"),
            resp([text_block("Bumped ✓")], "end_turn")])
        self.ask(second, caller=self.caller)
        row2 = self.budget_for("Dining")
        # Same row (upsert on category), new amount — not a second budget.
        self.assertEqual(row1["id"], row2["id"])
        self.assertEqual(25000, row2["amount_cents"])
        conn = self.db()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM budgets WHERE category = 'Dining'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(1, count)

    def test_set_budget_bad_amount_is_caught(self):
        mock = MockAnthropic([
            resp([tool_block("ledger_set_budget",
                             {"category": "Gas", "amount": -5})], "tool_use"),
            resp([text_block("A budget has to be a positive amount — how much?")],
                 "end_turn")])
        out = self.ask(mock, msg="budget minus five for gas", caller=self.caller)
        tr = mock.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(tr["is_error"])
        self.assertIsNone(self.budget_for("Gas"))
        self.assertEqual([], out["actions"])

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
        self.assertEqual(36, len(tools))  # 20 read + 16 write
        names = [t["name"] for t in tools]
        self.assertIn("ledger_classify_inflow", names)
        self.assertIn("ledger_recategorize_transaction", names)
        self.assertIn("ledger_propose_rule", names)
        self.assertIn("ledger_confirm_action", names)
        self.assertIn("ledger_set_budget", names)
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


class AskRuleTests(AskWriteBase):
    """B2: the rule pair — propose parks a preview (nothing written, no chip),
    confirm in a LATER ask-turn creates the rule + sweeps the previewed rows,
    all attributed to the person. The two-phase safety is SERVER-enforced
    (frozen payload, single-use token), so these tests exercise the real
    pending_actions machinery through the loop, not a prompt convention."""

    def an_inflow_phrase(self):
        """A seeded unclassified inflow's description + its matching rows."""
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT description FROM transactions WHERE direction = 'in' "
                "AND income_type = 'unclassified' ORDER BY id LIMIT 1").fetchone()
            self.assertIsNotNone(row)
            phrase = row["description"]
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM transactions WHERE direction = 'in' AND "
                "income_type = 'unclassified' AND instr(lower(description), "
                "lower(?)) > 0", (phrase,))]
        finally:
            conn.close()
        return phrase, ids

    def propose(self, set_type, match_desc, **extra):
        """Run one ask-turn that proposes a rule; returns (out, preview dict)."""
        mock = MockAnthropic([
            resp([tool_block("ledger_propose_rule",
                             {"set_type": set_type, "match_desc": match_desc,
                              **extra})], "tool_use"),
            resp([text_block("Here's what that rule would do — OK?")], "end_turn"),
        ])
        out = self.ask(mock, msg="always tag those", caller=self.caller)
        tr = mock.calls[1]["messages"][-1]["content"][0]
        self.assertFalse(tr["is_error"], tr["content"])
        return out, json.loads(tr["content"].split("\n", 1)[1])

    def confirm(self, token):
        """Run a SECOND ask-turn that confirms with `token`; returns (out, tr)."""
        mock = MockAnthropic([
            resp([tool_block("ledger_confirm_action",
                             {"confirmation_token": token})], "tool_use"),
            resp([text_block("Done — the rule is on.")], "end_turn"),
        ])
        out = self.ask(mock, msg="yes, do it", caller=self.caller)
        return out, mock.calls[1]["messages"][-1]["content"][0]

    def rule_count(self):
        conn = self.db()
        try:
            return conn.execute("SELECT COUNT(*) FROM income_rules").fetchone()[0]
        finally:
            conn.close()

    def test_propose_writes_nothing_and_carries_no_chip(self):
        phrase, ids = self.an_inflow_phrase()
        rules_before = self.rule_count()
        out, proposal = self.propose("paycheck", phrase)
        # The preview is honest: token + the would-match count.
        self.assertIn("confirmation_token", proposal)
        self.assertEqual(len(ids), proposal["preview"]["would_match_now"])
        # Nothing was created, the rows are untouched, and there is NO chip —
        # a parked preview is not a landed write.
        self.assertEqual(rules_before, self.rule_count())
        self.assertEqual([], out["actions"])
        self.assertEqual("paycheck", proposal["preview"]["set_type"])
        for txn_id in ids:
            self.assertEqual("unclassified", self.income_type(txn_id))

    def test_confirm_in_a_later_turn_creates_the_rule_and_sweeps(self):
        phrase, ids = self.an_inflow_phrase()
        _, proposal = self.propose("paycheck", phrase)
        out, tr = self.confirm(proposal["confirmation_token"])
        self.assertFalse(tr["is_error"], tr["content"])
        # The chip lands on the confirm, pointing at Activity.
        self.assertEqual([{"tab": "activity", "label": "Review in Activity"}],
                         out["actions"])
        # The rule exists, the previewed rows got tagged, all as the person.
        conn = self.db()
        try:
            rule = conn.execute(
                "SELECT * FROM income_rules ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(phrase, rule["match_desc"])
            self.assertEqual("paycheck", rule["set_type"])
            self.assertEqual(0, rule["set_transfer"])
            audit = conn.execute(
                "SELECT actor FROM audit_log WHERE action = 'create_income_rule' "
                "AND target = ?", (f"rule:{rule['id']}",)).fetchone()
            self.assertEqual("ui:avery", audit["actor"])
        finally:
            conn.close()
        for txn_id in ids:
            self.assertEqual("paycheck", self.income_type(txn_id))

    def test_confirm_token_is_single_use(self):
        phrase, _ = self.an_inflow_phrase()
        _, proposal = self.propose("gift", phrase)
        self.confirm(proposal["confirmation_token"])
        rules_after_first = self.rule_count()
        out, tr = self.confirm(proposal["confirmation_token"])
        self.assertTrue(tr["is_error"])          # refused, recoverable
        self.assertEqual(rules_after_first, self.rule_count())  # no double-create
        self.assertEqual([], out["actions"])     # a refused confirm earns no chip

    def test_transfer_rule_sets_the_flag_end_to_end(self):
        phrase, ids = self.an_inflow_phrase()
        _, proposal = self.propose("transfer", phrase)
        _, tr = self.confirm(proposal["confirmation_token"])
        self.assertFalse(tr["is_error"], tr["content"])
        conn = self.db()
        try:
            rule = conn.execute(
                "SELECT * FROM income_rules ORDER BY id DESC LIMIT 1").fetchone()
            # set_type='transfer' from Ask means a REAL transfer rule —
            # is_transfer is the source of truth (T3), not just the label.
            self.assertEqual(1, rule["set_transfer"])
            self.assertEqual("transfer", rule["set_type"])
            flags = [conn.execute(
                "SELECT is_transfer FROM transactions WHERE id = ?",
                (t,)).fetchone()[0] for t in ids]
        finally:
            conn.close()
        self.assertEqual([1] * len(ids), flags)

    def test_same_turn_confirm_is_refused_by_the_loop(self):
        # MIRAGE F1: propose AND confirm inside ONE run_ask (as a prompt
        # injection or a compliant-but-wrong model would) must NOT create the
        # rule — the human gate is structural, not just prompt-enforced. The
        # model reads the token from the propose result, then tries to confirm
        # it in the next round of the SAME turn.
        phrase, ids = self.an_inflow_phrase()
        rules_before = self.rule_count()

        class ConfirmSameTurn:
            """A model that grabs the token from round 1's tool_result and
            confirms it in round 2 — all within one run_ask."""
            def __init__(self):
                self.calls = []
                self.messages = NS(create=self._create)
                self._round = 0

            def _create(self, **kw):
                self.calls.append({**kw, "messages": list(kw["messages"])})
                self._round += 1
                if self._round == 1:
                    return resp([tool_block("ledger_propose_rule",
                                {"set_type": "paycheck", "match_desc": phrase})],
                                "tool_use")
                if self._round == 2:
                    # dig the token out of the previous tool_result
                    tr = kw["messages"][-1]["content"][0]["content"]
                    token = json.loads(tr.split("\n", 1)[1])["confirmation_token"]
                    return resp([tool_block("ledger_confirm_action",
                                {"confirmation_token": token}, id="tu_2")],
                                "tool_use")
                return resp([text_block("ok")], "end_turn")

        mock = ConfirmSameTurn()
        out = ask_loop.run_ask(mock, self.getter, "always tag those and do it",
                               model="claude-haiku-4-5", system="be helpful",
                               caller=self.caller)
        # The confirm was refused as a tool error; NOTHING was created or swept.
        tr = mock.calls[2]["messages"][-1]["content"][0]
        self.assertTrue(tr["is_error"])
        self.assertIn("same turn", tr["content"])
        self.assertEqual(rules_before, self.rule_count())
        self.assertEqual([], out["actions"])
        for txn_id in ids:
            self.assertEqual("unclassified", self.income_type(txn_id))

    def test_bad_proposal_is_refused_by_the_validator(self):
        # No matcher at all (the schema requires match_desc, but the verb is
        # the real wall — e.g. a hallucinated empty string).
        mock = MockAnthropic([
            resp([tool_block("ledger_propose_rule",
                             {"set_type": "paycheck", "match_desc": "  "})],
                 "tool_use"),
            resp([text_block("I need a phrase to match on — what should it be?")],
                 "end_turn"),
        ])
        out = self.ask(mock, msg="make a rule", caller=self.caller)
        tr = mock.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(tr["is_error"])
        self.assertEqual([], out["actions"])

    def test_invented_token_is_refused(self):
        rules_before = self.rule_count()
        out, tr = self.confirm("not-a-real-token")
        self.assertTrue(tr["is_error"])
        self.assertEqual([], out["actions"])
        self.assertEqual(rules_before, self.rule_count())


if __name__ == "__main__":
    unittest.main()
