"""Drift guard for the architecture Trace Web (static/trace-web.js).

The map claims to be "reconciled against actions.py · derivations.py" — but the
edge data is hand-maintained, so nothing stopped it going stale the moment a verb
was added (which is exactly how set_item_interval slipped off it). This test makes
the map self-verifying, the same way ontology.manifest() / test_architecture keep
the rest of the codebase honest: the three columns whose membership IS the source
of truth — write verbs, governed objects, read derivations — must match what the
real source exposes, or the build goes red.

It parses the JS literals with a regex (like ontology._registry_verbs parses the
markdown registry). The extraction is guarded by a sane-count assertion, so a
formatting change that breaks the parse fails loudly here rather than silently
passing an empty set.
"""
import inspect
import re
import unittest
from pathlib import Path

import actions
import derivations
import ontology

REPO = Path(__file__).resolve().parents[1]
JS = (REPO / "static" / "trace-web.js").read_text()


def _block(name, open_c, close_c):
    """The body of a top-level `const NAME = <open>...<close>;` literal, bounded
    at the first `<close>;` (so it works whether the literal is one line — like
    DERIVS's `[...];` — or many, and doesn't run into the next const). Nested
    inner arrays don't end with `<close>;`, so they don't fool the bound. `//`
    line comments are then stripped so quoted words inside a comment (e.g. the
    `'settles'` link type mentioned in a WRITES comment) never leak into the
    token extraction. Safe because no data value here contains `<close>;`."""
    m = re.search(r"const " + name + r" = " + re.escape(open_c) + r"(.*?)"
                  + re.escape(close_c) + ";", JS, re.S)
    assert m, f"could not find `const {name} = {open_c}...{close_c};` in trace-web.js"
    return re.sub(r"//.*", "", m.group(1))


def _writes_verbs():
    # keys of the WRITES object: `  verb_name: [...]`
    return set(re.findall(r"^\s*([a-z_]+):", _block("WRITES", "{", "}"), re.M))


def _writes_tables():
    # every table named on the right-hand side of WRITES (the quoted lowercase ids)
    return set(re.findall(r"'([a-z_]+)'", _block("WRITES", "{", "}")))


def _obj_tables():
    # the quoted lowercase ids inside OBJ (group labels have spaces/caps, so the
    # [a-z_] class skips them — only table names match)
    return set(re.findall(r"'([a-z_]+)'", _block("OBJ", "[", "]")))


def _derivs():
    return set(re.findall(r"'([a-z_]+)'", _block("DERIVS", "[", "]")))


def _source_derivations():
    return {n for n, f in inspect.getmembers(derivations, inspect.isfunction)
            if not n.startswith("_")
            and list(inspect.signature(f).parameters)[:1] == ["db"]}


# ── edge oracles: what each verb DIRECTLY writes / each derivation DIRECTLY
#    reads, straight from the source, so the map's wiring — not just its node
#    lists — is checked against the code. ────────────────────────────────────
GOV = set(actions.GOVERNED_TABLES)
_WRITE_RE = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)"
    r"\s+[\"'\[]?([a-z_]+)", re.I)
_READ_RE = re.compile(r"\b(?:FROM|JOIN)\s+[\"'\[]?([a-z_]+)", re.I)


def _module_fns(mod):
    return {n: f for n, f in inspect.getmembers(mod, inspect.isfunction)
            if f.__module__ == mod.__name__}


def _verbs():
    return {n: f for n, f in _module_fns(actions).items()
            if not n.startswith("_")
            and list(inspect.signature(f).parameters)[:2] == ["db", "actor"]}


def _closure_tables(mod, regex, follow_verbs):
    """Per-function governed-table refs matched by `regex`, expanded through the
    module's call graph. follow_verbs=False stops at other public verbs (those
    are the trace's *cascade* edges, drawn separately) — used for writes, so a
    verb is charged only with what it and its PRIVATE helpers mutate.
    follow_verbs=True follows every intra-module call — used for reads, so a
    derivation that reads via another derivation transitively reads its tables.
    Cycle-safe."""
    fns = _module_fns(mod)
    verbs = set(_verbs()) if mod is actions else set()
    src = {n: inspect.getsource(f) for n, f in fns.items()}
    callre = re.compile(r"\b(" + "|".join(map(re.escape, fns)) + r")\s*\(")
    direct = {n: {t.lower() for t in regex.findall(s) if t.lower() in GOV}
              for n, s in src.items()}
    calls = {n: {c for c in callre.findall(s)
                 if c != n and (follow_verbs or c not in verbs)}
             for n, s in src.items()}
    memo = {}

    def resolve(name, seen):
        if name in memo:
            return memo[name]
        if name in seen:
            return direct[name]
        seen = seen | {name}
        tables = set(direct[name])
        for callee in calls[name]:
            tables |= resolve(callee, seen)
        memo[name] = tables
        return tables

    return {n: resolve(n, frozenset()) for n in fns}


def _trace_writes_by_verb():
    block = _block("WRITES", "{", "}")
    return {m.group(1): set(re.findall(r"'([a-z_]+)'", m.group(2)))
            for m in re.finditer(r"^\s*([a-z_]+):\s*\[(.*?)\]", block, re.M)}


def _trace_reads_by_deriv():
    block = _block("READS", "{", "}")
    inv = {}
    for m in re.finditer(r"^\s*([a-z_]+):\s*\[(.*?)\]", block, re.M):
        obj = m.group(1)
        for d in re.findall(r"'([a-z_]+)'", m.group(2)):
            inv.setdefault(d, set()).add(obj)
    return inv


class TraceWebDataTests(unittest.TestCase):
    def test_extraction_found_a_sane_amount(self):
        # if the regex ever silently stops matching, the equality tests below
        # would compare against {} — catch that here first.
        self.assertGreaterEqual(len(_writes_verbs()), 25, "WRITES parse looks broken")
        self.assertGreaterEqual(len(_obj_tables()), 12, "OBJ parse looks broken")
        self.assertGreaterEqual(len(_derivs()), 20, "DERIVS parse looks broken")

    def test_verbs_match_the_action_registry(self):
        manifest_verbs = {a["name"] for a in ontology.manifest()["actions"]}
        self.assertEqual(
            manifest_verbs, _writes_verbs(),
            "trace-web.js WRITES is out of sync with actions.py's write verbs "
            "(ontology.manifest). Add/remove the verb in WRITES + CALLERS.")

    def test_objects_match_governed_tables(self):
        self.assertEqual(
            set(actions.GOVERNED_TABLES), _obj_tables(),
            "trace-web.js OBJ is out of sync with actions.GOVERNED_TABLES.")

    def test_derivations_match_derivations_module(self):
        self.assertEqual(
            _source_derivations(), _derivs(),
            "trace-web.js DERIVS is out of sync with the public db-derivations "
            "in derivations.py.")

    def test_every_written_table_is_a_known_object(self):
        # internal coherence: a verb can't write a table the map doesn't draw.
        unknown = _writes_tables() - _obj_tables()
        self.assertEqual(set(), unknown,
                         f"WRITES names table(s) absent from OBJ: {sorted(unknown)}")

    def test_write_edges_cover_every_direct_write(self):
        # Edge-level guard: every table a verb PROVABLY writes in the source (its
        # own body + private helpers, per WRITE_RE) must be on that verb's
        # WRITES edge — so a new mutation can't quietly go undrawn. Superset, not
        # equality: the map legitimately adds edges the static scan can't see —
        # FK cascades (delete_goal → goal_contributions) and dynamic-table verbs
        # (reset_money DELETEs f-string table names). audit_log is excluded: it's
        # every verb's write, drawn as its own 'audit' grammar, not a WRITES edge.
        scan = _closure_tables(actions, _WRITE_RE, follow_verbs=False)
        trace = _trace_writes_by_verb()
        missing = {v: sorted(scan[v] - {"audit_log"} - trace.get(v, set()))
                   for v in _verbs()
                   if scan[v] - {"audit_log"} - trace.get(v, set())}
        self.assertEqual(
            {}, missing,
            "trace-web.js WRITES omits a direct write proven in actions.py "
            f"(verb -> missing tables): {missing}. Add it to that verb's row.")

    def test_read_edges_cover_every_direct_read(self):
        # Edge-level guard for the READS map (the densest hand-maintained data):
        # every table a derivation PROVABLY reads (its own FROM/JOIN + those of
        # the derivations/helpers it calls) must be on that derivation's read
        # edges. Superset for the same reason — the map may carry edges the scan
        # can't follow (a callable passed into _monthly_series).
        reads = _closure_tables(derivations, _READ_RE, follow_verbs=True)
        trace = _trace_reads_by_deriv()
        missing = {d: sorted(reads[d] - trace.get(d, set()))
                   for d in _source_derivations()
                   if reads[d] - trace.get(d, set())}
        self.assertEqual(
            {}, missing,
            "trace-web.js READS omits a table a derivation reads in "
            f"derivations.py (derivation -> missing tables): {missing}.")

    def test_doors_match_the_ontology_doors(self):
        # #2: the three door nodes are tied to the ontology's door ids via `key`,
        # so a new read surface can't be drawn (or an old one drop off) without
        # the source. Labels are free to be prettier than the manifest keys.
        keys = set(re.findall(r"key:\s*'([a-z]+)'", _block("DOORS", "[", "]")))
        self.assertEqual(set(ontology.manifest()["doors"]), keys,
                         "trace-web.js DOORS keys are out of sync with "
                         "ontology.manifest()['doors'].")

    def test_every_door_serves_the_full_read_layer(self):
        # the map's claim is "one shared read layer, three doors" — every door
        # exposes every derivation (verified: the MCP + Ask doors share the
        # agent_read_tools registry, which collectively covers all 23). Pin it
        # structurally so no door can silently drop derivations.
        block = _block("DOORS", "[", "]")
        doors = re.findall(r"\{[^}]*\}", block)
        self.assertEqual(3, len(doors), "expected 3 doors")
        for d in doors:
            self.assertIn("serves: DERIVS.slice()", d,
                          f"a door does not serve the full derivation set: {d}")


if __name__ == "__main__":
    unittest.main()
