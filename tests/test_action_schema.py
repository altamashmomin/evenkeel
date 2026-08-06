"""ACTION-SCHEMA-DESIGN inc 1: PARAM_SPECS is the single source for an agent
write verb's parameter schema. This pins param_schema(verb) BYTE-EQUAL to today's
hand-written agent write-tool input_schemas — so inc 2 (agent_write_tools) and
inc 3 (ledger_mcp) can generate their schemas from PARAM_SPECS with zero change —
and binds the enum vocabularies to the verbs' membership frozensets so a new
kind/status/income-type can't leave a tool's enum stale (the whole point)."""
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
    "add_item": "ledger_add_item",
    "set_item_status": "ledger_set_item_status",
}


class ActionSchemaTests(unittest.TestCase):
    def test_param_schema_matches_the_hand_written_tool_schema(self):
        by_name = {t["name"]: t for t in agent_write_tools.WRITE_TOOLS}
        for verb, tool in VERB_TOOL.items():
            self.assertEqual(
                by_name[tool]["input_schema"], actions.param_schema(verb),
                f"{verb}: generated schema drifted from the hand-written one")

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

    def test_offered_income_types_still_bound_to_agent_write_tools(self):
        # Until inc 2 has agent_write_tools consume REAL_INCOME_TYPE_ORDER, bind
        # the two ordered lists so they cannot drift in the meantime.
        self.assertEqual(list(actions.REAL_INCOME_TYPE_ORDER),
                         agent_write_tools.REAL_INCOME_TYPES)


if __name__ == "__main__":
    unittest.main()
