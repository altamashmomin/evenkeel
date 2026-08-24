"""ACTION-SCHEMA-DESIGN: PARAM_SPECS is the single source for an agent write
verb's parameter schema. Inc 2 landed — agent_write_tools now GENERATES each
tool's input_schema from actions.param_schema(verb) (the hand-written blocks are
gone); this asserts that wiring, and binds the enum vocabularies to the verbs'
membership frozensets so a new kind/status/income-type can't leave a tool's enum
stale (the whole point). Inc 3 (ledger_mcp) extends the same source."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import actions             # noqa: E402
import agent_write_tools   # noqa: E402

# Each agent-exposed write verb -> the tool that surfaces it.
VERB_TOOL = {
    "classify_inflow": "ledger_classify_inflow",
    "recategorize_transaction": "ledger_recategorize_transaction",
    "create_bill": "ledger_add_bill",
    "create_goal": "ledger_add_goal",
    # B2: the rule pair. create_income_rule's spec is the NARROW Ask facade
    # (match_desc + set_type + also_apply); the verb accepts more via the
    # app/MCP — the facade schema simply can't reach it, like B1's
    # category-only recategorize.
    "create_income_rule": "ledger_propose_rule",
    "confirm_action": "ledger_confirm_action",
    "set_budget": "ledger_set_budget",
    "add_item": "ledger_add_item",
    "set_item_status": "ledger_set_item_status",
    "restock_items": "ledger_restock_items",
    "archive_item": "ledger_archive_item",
    "set_item_match": "ledger_set_item_match",
    "set_item_interval": "ledger_set_item_interval",
    "set_item_store": "ledger_set_item_store",
    "set_item_need_by": "ledger_set_item_need_by",
    "set_item_snooze": "ledger_set_item_snooze",
}


class ActionSchemaTests(unittest.TestCase):
    def test_write_tools_generate_their_schema_from_param_specs(self):
        # agent_write_tools now sources each tool's input_schema from
        # actions.param_schema(verb) — there is no second, hand-written copy.
        by_name = {t["name"]: t for t in agent_write_tools.WRITE_TOOLS}
        for verb, tool in VERB_TOOL.items():
            self.assertEqual(
                by_name[tool]["input_schema"], actions.param_schema(verb),
                f"{verb}: tool schema is not the PARAM_SPECS-generated one")

    def test_every_param_spec_verb_has_a_real_verb(self):
        for verb in actions.PARAM_SPECS:
            self.assertTrue(callable(getattr(actions, verb, None)),
                            f"PARAM_SPECS names {verb!r}, which is not a verb")

    def test_enum_order_covers_exactly_the_membership_frozensets(self):
        # The ordered enum tuples the tools use must cover exactly the frozensets
        # the verbs validate against — so adding a kind/status/type to one can't
        # silently leave the tool enum stale (it fails here until both update).
        self.assertEqual(set(actions.ITEM_KIND_ORDER), set(actions.ITEM_KINDS))
        self.assertEqual(set(actions.ITEM_STATUS_ORDER), set(actions.ITEM_STATUSES))
        self.assertEqual(set(actions.REAL_INCOME_TYPE_ORDER),
                         set(actions.INCOME_TYPES) - {"unclassified"})

    def test_income_type_enum_excludes_unclassified(self):
        # The offered income types are the real ones — 'unclassified' is the
        # absence of a tag, never an assignable choice.
        enum = actions.param_schema("classify_inflow")["properties"]["income_type"]["enum"]
        self.assertNotIn("unclassified", enum)
        self.assertEqual(list(actions.REAL_INCOME_TYPE_ORDER), enum)


if __name__ == "__main__":
    unittest.main()
