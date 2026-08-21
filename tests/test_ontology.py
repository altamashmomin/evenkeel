"""Coherence tests for the ontology manifest (ONTOLOGY-MANIFEST-DESIGN).

The manifest is generated from source, so these guard that the generation
stays faithful — and, because objects/actions/functions are discovered by
introspection, a new table / verb / derivation is manifest-covered
automatically (the tripwire property). Each check is cross-derived a
DIFFERENT way than the manifest derives it (textual regex vs. runtime
introspection), so the test and the code can't share a bug.

Teeth were verified by hand (house rule): an unregistered verb makes
test_all_actions_registered fail; dropping a table from GOVERNED_TABLES
makes test_objects_match_governed_tables fail; a table no migration
creates makes test_every_object_has_migration fail.
"""
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import actions
import ontology

MANIFEST = ontology.manifest()


class ManifestObjectTests(unittest.TestCase):
    def test_objects_match_governed_tables(self):
        names = {o["name"] for o in MANIFEST["objects"]}
        self.assertEqual(set(actions.GOVERNED_TABLES), names)

    def test_every_object_has_migration_provenance(self):
        missing = [o["name"] for o in MANIFEST["objects"]
                   if not o["created_by_migration"]]
        self.assertEqual([], missing,
                         f"object(s) no migration creates: {missing}")

    def test_written_by_are_real_registered_writes(self):
        verb_names = {a["name"] for a in MANIFEST["actions"]}
        for o in MANIFEST["objects"]:
            for v in o["written_by"]:
                self.assertIn(v, verb_names,
                              f"{o['name']}.written_by names unknown verb {v}")

    def test_action_writes_are_governed_tables(self):
        for a in MANIFEST["actions"]:
            for t in a["writes"]:
                self.assertIn(t, actions.GOVERNED_TABLES,
                              f"{a['name']} writes ungoverned table {t}")

    def test_members_has_no_verb_writer(self):
        # members is the one governed table written outside a verb (the
        # setup() route — test_architecture's KNOWN_EXCEPTIONS). If a
        # member/auth verb is ever extracted, this assertion flips and both
        # this and that exception get removed together.
        members = next(o for o in MANIFEST["objects"] if o["name"] == "members")
        self.assertEqual([], members["written_by"])
        others = [o for o in MANIFEST["objects"] if o["name"] != "members"]
        self.assertTrue(all(o["written_by"] for o in others),
                        "every governed table except members has a verb writer")


class ManifestActionTests(unittest.TestCase):
    def _defined_verbs_textually(self):
        # Independent of the manifest's introspection: the textual
        # def NAME(db, actor discovery test_architecture also uses.
        src = (REPO / "actions.py").read_text()
        return set(re.findall(r"^def ([a-z][a-z_]*)\(db, actor", src, re.M))

    def test_every_defined_verb_is_in_manifest(self):
        names = {a["name"] for a in MANIFEST["actions"]}
        self.assertEqual(self._defined_verbs_textually(), names)

    def test_all_actions_registered(self):
        # The manifest reports registration; asserting all-True here makes
        # CORE-DESIGN's prose registry table load-bearing in the reverse
        # direction too (ACTION-SCHEMA-DESIGN step 5's coherence check).
        unreg = sorted(a["name"] for a in MANIFEST["actions"]
                       if not a["registered"])
        self.assertEqual([], unreg,
                         f"verb(s) not in CORE-DESIGN's registry table: {unreg}")

    def test_params_match_param_schema(self):
        for a in MANIFEST["actions"]:
            if a["name"] in actions.PARAM_SPECS:
                self.assertEqual(actions.param_schema(a["name"]), a["params"])
            else:
                self.assertIsNone(a["params"])

    def test_two_phase_flags_match_confirm_dispatch(self):
        by_name = {a["name"]: a for a in MANIFEST["actions"]}
        # confirm_action dispatches create_income_rule and apply_rules.
        self.assertTrue(by_name["create_income_rule"]["two_phase"])
        self.assertTrue(by_name["apply_rules"]["two_phase"])
        # a direct verb is not two-phase; the dispatcher itself isn't a target.
        self.assertFalse(by_name["classify_inflow"]["two_phase"])
        self.assertFalse(by_name["confirm_action"]["two_phase"])


class ManifestFunctionTests(unittest.TestCase):
    def _public_db_derivations_textually(self):
        src = (REPO / "derivations.py").read_text()
        return set(re.findall(r"^def ([a-z][a-z_]*)\(db[,)]", src, re.M))

    def test_every_public_derivation_is_in_manifest(self):
        names = {f["name"] for f in MANIFEST["functions"]}
        self.assertEqual(self._public_db_derivations_textually(), names)

    def test_reads_inflows_matches_tripwire_exempt(self):
        # The manifest's reads_inflows flag must equal the tripwire's EXEMPT
        # set exactly — same fact, two files, drift-tested.
        exempt = ontology._exempt_derivations()
        self.assertTrue(exempt, "parsed no EXEMPT names — parser broke")
        flagged = {f["name"] for f in MANIFEST["functions"] if f["reads_inflows"]}
        self.assertEqual(exempt, flagged)


class ManifestEdgeTests(unittest.TestCase):
    """The edge-level facts added for the data-driven Trace Web: direct writes,
    derivation reads, cascades, and the caller map — each derived from source
    (AST call detection, docstring-stripped; HTTP calls walked through the
    route table). These pins were chosen to bite on the exact bugs found while
    building the derivation: a docstring mention registering as a call, and a
    paren-less verb reference being missed."""

    def test_writes_direct_is_a_subset_of_writes(self):
        for a in MANIFEST["actions"]:
            self.assertTrue(set(a["writes_direct"]) <= set(a["writes"]),
                            f"{a['name']}: writes_direct exceeds the closure")

    def test_docstring_mentions_do_not_register(self):
        # create_income_rule's docstring says "…propose_action/confirm_action
        # (the MCP write tier)" — prose, not a call. The old regex scan charged
        # it with confirm_action's writes; the AST scan must not.
        by_name = {a["name"]: a for a in MANIFEST["actions"]}
        self.assertEqual(["audit_log", "income_rules"],
                         by_name["create_income_rule"]["writes"])
        self.assertEqual([], by_name["create_income_rule"]["cascades"])

    def test_confirm_action_is_the_only_cascader(self):
        cascaders = {a["name"]: a["cascades"] for a in MANIFEST["actions"]
                     if a["cascades"]}
        self.assertEqual({"confirm_action": ["apply_rules", "create_income_rule"]},
                         cascaders)

    def test_reset_money_carries_its_dynamic_tables(self):
        # reset_money DELETEs via an f-string loop over RESET_TABLES — the scan
        # can't see it in text, so ontology charges the constant itself.
        by_name = {a["name"]: a for a in MANIFEST["actions"]}
        self.assertTrue(set(actions.RESET_TABLES)
                        <= set(by_name["reset_money"]["writes_direct"]))

    def test_propose_action_writes_no_audit(self):
        # nothing executed at propose time — the one verb without an audit row.
        by_name = {a["name"]: a for a in MANIFEST["actions"]}
        self.assertNotIn("audit_log", by_name["propose_action"]["writes_direct"])
        others = [a for a in MANIFEST["actions"]
                  if a["name"] not in ("propose_action",)]
        for a in others:
            self.assertIn("audit_log", a["writes_direct"],
                          f"{a['name']} should write an audit row")

    def test_function_reads_are_governed_and_sane(self):
        for f in MANIFEST["functions"]:
            self.assertTrue(set(f["reads"]) <= set(actions.GOVERNED_TABLES),
                            f"{f['name']} reads a non-governed table")
        by_name = {f["name"]: f for f in MANIFEST["functions"]}
        self.assertEqual(["members", "splits", "transactions"],
                         by_name["compute_balance"]["reads"])

    def test_callers_shape_and_the_paren_less_reference(self):
        c = MANIFEST["callers"]
        self.assertEqual({"ui", "sync", "mcp", "ask", "cli"}, set(c))
        self.assertEqual(["record_transaction"], c["sync"])
        # reset_money is the ONLY door-less verb; in particular the goal verbs
        # are UI-reachable through a paren-less reference
        # (`actions.contribute_to_goal if cents > 0 else …`), which a
        # call-only scan missed.
        self.assertEqual(["reset_money"], c["cli"])
        self.assertIn("contribute_to_goal", c["ui"])
        self.assertIn("withdraw_from_goal", c["ui"])
        # the doors, walked through the route table
        self.assertEqual(["classify_inflow", "confirm_action", "propose_action",
                          "set_rule_enabled"], c["mcp"])
        self.assertEqual(["add_item", "archive_item", "classify_inflow",
                          "restock_items", "set_item_interval", "set_item_match",
                          "set_item_need_by", "set_item_snooze",
                          "set_item_status", "set_item_store"], c["ask"])

    def test_every_caller_verb_is_real(self):
        verb_names = {a["name"] for a in MANIFEST["actions"]}
        for surface, verbs in MANIFEST["callers"].items():
            self.assertTrue(set(verbs) <= verb_names,
                            f"callers[{surface}] names unknown verbs")


class ManifestShapeTests(unittest.TestCase):
    def test_vocabularies_reference_the_constants(self):
        v = MANIFEST["vocabularies"]
        self.assertEqual(list(actions.REAL_INCOME_TYPE_ORDER), v["income_types"])
        self.assertEqual(list(actions.ITEM_KIND_ORDER), v["item_kinds"])
        self.assertEqual(list(actions.ITEM_STATUS_ORDER), v["item_statuses"])

    def test_schema_version_is_current(self):
        from schema_runtime import REQUIRED_SCHEMA_VERSION
        self.assertEqual(REQUIRED_SCHEMA_VERSION, MANIFEST["schema_version"])

    def test_manifest_is_json_serializable(self):
        self.assertEqual(MANIFEST, json.loads(json.dumps(MANIFEST)))

    def test_manifest_takes_no_db(self):
        # The ontology is a property of the code, not any database's contents.
        import inspect
        self.assertEqual([], list(inspect.signature(ontology.manifest).parameters))


if __name__ == "__main__":
    unittest.main()
