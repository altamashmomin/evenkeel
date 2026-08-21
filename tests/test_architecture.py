"""Architectural invariants, enforced automatically so structural drift
fails the suite instead of waiting for a human to notice.

This is the committed, always-run version of the routine integrity sweep:
the by-hand greps were only as reliable as remembering to run them (and
running them right). Encoding them as tests makes the architecture
self-checking — a new raw write in a route, or a verb added without a
registry row, turns the build red the moment it lands.

Schema-version coherence (REQUIRED_SCHEMA_VERSION == latest migration) is
already covered by test_migrations.test_runtime_version_matches_latest_
migration, so it isn't repeated here."""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# GOVERNED_TABLES and WRITE_RE now live with the code they describe, so this
# test, the manifest, and the verbs share one definition instead of three
# copies drifting apart: the governed-table set lives in actions.py (with
# the registry it governs) and the raw-write regex in ontology.py (with the
# manifest's writes-scan). 'members' is in the set but carries a documented
# KNOWN_EXCEPTIONS entry below (account/auth management isn't a verb yet —
# CORE-DESIGN open question), so this test still catches auth writes leaking
# into new files.
from actions import GOVERNED_TABLES
from ontology import WRITE_RE

# Files/dirs where raw SQL against governed tables legitimately lives:
# the verbs themselves, schema migrations, and synthetic fixtures (whose
# whole job is to write rows directly — data, not verb calls).
ALLOWED_PATHS = {"actions.py", "seed_db.py", "seed_income.py"}
# Non-source trees to skip. `.claude` matters beyond config: a harness/agent
# git worktree under `.claude/worktrees/<name>/` is a FULL repo copy, so its
# nested `tests/`/`migrations/` would otherwise be scanned — their top-level
# path part is `.claude`, not `tests`, so the deeper exclusions never fire.
# `venv` mirrors `.venv` (this checkout has both virtualenv dir names).
ALLOWED_DIRS = {"migrations", "tests", ".venv", "venv", "__pycache__",
                ".git", ".claude"}

# Known, reasoned exceptions keyed by (filename, table). Each is a write
# that predates or sits outside the verb registry on purpose. Removing a
# verb's need for its exception (e.g. extracting an auth verb) means
# deleting the entry here — the test then guards the newly-governed table.
KNOWN_EXCEPTIONS = {
    ("app.py", "members"):
        "one-time account creation in the setup() route — the bootstrap has "
        "no member to act as yet. Password CHANGE is a verb since Aug 21, "
        "2026 (change_password); extracting setup itself is the remaining "
        "CORE-DESIGN open question",
}


def production_py_files():
    """Every .py under the repo except allow-listed dirs — a new file is
    scanned by default, which is the point (nothing opts in silently)."""
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if rel.parts[0] in ALLOWED_DIRS:
            continue
        if rel.name in ALLOWED_PATHS:
            continue
        yield path


class Invariant1NoRawWritesTests(unittest.TestCase):
    def test_no_raw_writes_to_governed_tables_outside_verbs(self):
        violations = []
        for path in production_py_files():
            rel = path.relative_to(REPO)
            text = path.read_text()
            # Full-text scan (not line-by-line) so SQL whose table name is
            # wrapped onto the next line can't slip through (#11). Line number
            # recovered from the match offset for a useful message.
            for m in WRITE_RE.finditer(text):
                table = m.group(1).lower()
                if table not in GOVERNED_TABLES:
                    continue
                if (rel.name, table) in KNOWN_EXCEPTIONS:
                    continue
                line_no = text.count("\n", 0, m.start()) + 1
                snippet = text[m.start():m.start() + 60].splitlines()[0]
                violations.append(f"{rel}:{line_no}: raw write to '{table}' — {snippet}")
        self.assertEqual(
            [], violations,
            "\nRaw SQL write(s) to a registry-governed table outside "
            "actions.py (CORE-DESIGN invariant 1: every write is a named "
            "verb). Move the write into a verb in actions.py and make the "
            "caller thin, or — if it is a genuinely new out-of-registry "
            "path — add a reasoned entry to KNOWN_EXCEPTIONS.\n  "
            + "\n  ".join(violations))

    def test_known_exceptions_are_still_real(self):
        # A KNOWN_EXCEPTIONS entry that no longer corresponds to an actual
        # write is stale — it would silently mask a future real write to
        # that (file, table). Require each exception to still fire.
        live = set()
        for path in production_py_files():
            rel = path.relative_to(REPO)
            text = path.read_text()
            for table in WRITE_RE.findall(text):
                live.add((rel.name, table.lower()))
        stale = [key for key in KNOWN_EXCEPTIONS if key not in live]
        self.assertEqual(
            [], stale,
            f"\nStale KNOWN_EXCEPTIONS (no matching write remains — remove "
            f"them so they can't mask a future real write): {stale}")


class RegistryCoherenceTests(unittest.TestCase):
    def _defined_verbs(self):
        # Public verbs: def NAME(db, actor, ...). Private helpers (_-prefixed,
        # e.g. _record_goal_flow, _write_audit) are not registry entries.
        src = (REPO / "actions.py").read_text()
        return set(re.findall(r"^def ([a-z][a-z_]*)\(db, actor", src, re.M))

    def _registry_verbs(self):
        # Backticked identifiers on any markdown table row in CORE-DESIGN.md.
        # The verb registry is the only such table with verb-shaped entries.
        doc = (REPO / "docs" / "CORE-DESIGN.md").read_text()
        registry = set()
        for line in doc.splitlines():
            if line.lstrip().startswith("|"):
                registry |= set(re.findall(r"`([a-z][a-z_]*)`", line))
        return registry

    def test_every_defined_verb_is_registered(self):
        defined = self._defined_verbs()
        self.assertTrue(defined, "no verbs discovered in actions.py — parser broke")
        unregistered = sorted(defined - self._registry_verbs())
        self.assertEqual(
            [], unregistered,
            "\nVerb(s) defined in actions.py but absent from CORE-DESIGN's "
            "registry table (a write path that isn't in the registry doesn't "
            "exist — add the row before/with the code, per the growth rule):\n  "
            + "\n  ".join(unregistered))


if __name__ == "__main__":
    unittest.main()
