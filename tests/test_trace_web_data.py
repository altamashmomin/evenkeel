"""Guard for the Trace Web's remaining hand-maintained content.

The map is DATA-DRIVEN since /api/ontology landed: every fact — verbs, objects,
derivations, callers, doors, write/read/cascade edges — is fetched from
ontology.manifest() at load, so the old fact-sync guards are obsolete by
construction (there are no facts in the file to drift). What still lives in
trace-web.js are PRESENTATION HINTS: visual grouping, preferred ordering,
display labels, phase/sink styling, and the single FK-cascade edge SQLite
enforces rather than code. This test pins two things:

1. The JS really carries no fact tables (a regression to hand-maintained
   WRITES/READS/CALLERS/etc. would reintroduce silent drift).
2. Every name a hint mentions is real (a renamed verb/table can't leave a
   stale hint behind), and the grouping/order hints stay complete where
   completeness is the point (every governed table has a visual home; the
   caller/door orderings cover exactly the manifest's ids).

New names are deliberately allowed to be ABSENT from the order hints — the map
appends them alphabetically, so a new verb/derivation renders without touching
the file. Only staleness and incompleteness-of-grouping fail here.
"""
import re
import unittest
from pathlib import Path

import actions
import ontology

REPO = Path(__file__).resolve().parents[1]
JS = (REPO / "static" / "trace-web.js").read_text()
MANIFEST = ontology.manifest()

VERBS = {a["name"] for a in MANIFEST["actions"]}
TABLES = set(actions.GOVERNED_TABLES)
DERIVS = {f["name"] for f in MANIFEST["functions"]}


def _const_block(name, open_c, close_c):
    m = re.search(r"const " + name + r"\s*=\s*" + re.escape(open_c) + r"(.*?)"
                  + re.escape(close_c) + r";", JS, re.S)
    assert m, f"could not find `const {name}` in trace-web.js"
    return re.sub(r"//.*", "", m.group(1))   # strip comments before extraction


def _quoted(text):
    return set(re.findall(r"'([a-z_]+)'", text))


class TraceWebHintsTests(unittest.TestCase):
    # ── 1. no facts in the file ──────────────────────────────────────────────
    def test_no_hand_maintained_fact_tables(self):
        for fact_const in ("const WRITES", "const READS", "const CASCADE",
                           "const ALL_VERBS", "const DERIVS ="):
            self.assertNotIn(
                fact_const, JS,
                f"{fact_const!r} back in trace-web.js — facts must come from "
                "/api/ontology (buildModel), never a hand-kept table.")
        self.assertIn("fetch('/api/ontology')", JS,
                      "the map must fetch its facts from /api/ontology")

    # ── 2. hints reference only real names ───────────────────────────────────
    def test_obj_groups_cover_exactly_the_governed_tables(self):
        # every governed table has a deliberate visual home (the 'Other'
        # fallback renders strays, but a permanent resident there is sloppy),
        # and no group names a table that no longer exists.
        grouped = _quoted(_const_block("OBJ_GROUPS", "[", "]"))
        self.assertEqual(TABLES, grouped,
                         "OBJ_GROUPS out of sync with GOVERNED_TABLES — place "
                         "the new table / drop the stale one "
                         f"(diff: {sorted(TABLES ^ grouped)})")

    def test_order_hints_name_only_real_things(self):
        stale_verbs = _quoted(_const_block("VERB_ORDER", "[", "]")) - VERBS
        self.assertEqual(set(), stale_verbs,
                         f"VERB_ORDER names unknown verbs: {sorted(stale_verbs)}")
        stale_derivs = _quoted(_const_block("DERIV_ORDER", "[", "]")) - DERIVS
        self.assertEqual(set(), stale_derivs,
                         f"DERIV_ORDER names unknown derivations: {sorted(stale_derivs)}")

    def test_caller_and_door_hints_cover_exactly_the_manifest_ids(self):
        caller_order = _quoted(_const_block("CALLER_ORDER", "[", "]"))
        self.assertEqual(set(MANIFEST["callers"]), caller_order,
                         "CALLER_ORDER out of sync with manifest.callers ids")
        door_order = _quoted(_const_block("DOOR_ORDER", "[", "]"))
        self.assertEqual(set(MANIFEST["doors"]), door_order,
                         "DOOR_ORDER out of sync with manifest.doors")

    def test_phase_sink_and_fk_hints_are_real(self):
        phase = _quoted(_const_block("PHASE_VERBS", "new Set([", "])"))
        self.assertTrue(phase <= VERBS, f"PHASE_VERBS unknown: {sorted(phase - VERBS)}")
        # SINKS entries are node ids ('o:<table>')
        sinks = set(re.findall(r"'o:([a-z_]+)'", _const_block("SINKS", "new Set([", "])")))
        self.assertTrue(sinks <= TABLES, f"SINKS unknown: {sorted(sinks - TABLES)}")
        fk = _const_block("FK_CASCADE", "{", "}")
        fk_verbs = set(re.findall(r"^\s*([a-z_]+):", fk, re.M))
        fk_tables = _quoted(fk)
        self.assertTrue(fk_verbs <= VERBS, f"FK_CASCADE unknown verbs: {sorted(fk_verbs - VERBS)}")
        self.assertTrue(fk_tables <= TABLES, f"FK_CASCADE unknown tables: {sorted(fk_tables - TABLES)}")

    # ── the one hand edge stays justified ────────────────────────────────────
    def test_fk_cascade_edge_is_still_real_in_the_schema(self):
        # delete_goal → goal_contributions rides goals' ON DELETE CASCADE; if
        # the FK ever changes, this hint must go too. The DDL lives in the
        # migrations (001 baseline carries the v1.0 schema).
        ddl = "".join(p.read_text() for p in sorted((REPO / "migrations").iterdir())
                      if p.suffix in {".sql", ".py"})
        self.assertRegex(
            ddl, r"goal_contributions[\s\S]{0,400}?ON\s+DELETE\s+CASCADE",
            "FK_CASCADE hints delete_goal→goal_contributions, but no ON DELETE "
            "CASCADE near goal_contributions exists in the migrations DDL")


if __name__ == "__main__":
    unittest.main()
