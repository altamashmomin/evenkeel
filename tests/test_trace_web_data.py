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
    """The body of a top-level `const NAME = <open>...<close>;` literal, with
    `//` line comments stripped — so quoted words inside a comment (e.g. the
    `'settles'` link type mentioned in a WRITES comment) never leak into the
    token extraction. Safe because no data value here contains `//`."""
    m = re.search(r"const " + name + r" = " + re.escape(open_c) + r"(.*?)\n"
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


if __name__ == "__main__":
    unittest.main()
