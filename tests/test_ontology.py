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
