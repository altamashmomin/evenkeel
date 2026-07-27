"""Automated tripwire for the bug class the sync-flip session found by
hand: no function in derivations.py may change its answer when an inflow
is added, unless it's explicitly allowed to (a genuine income aggregate
would say so here, with a reason).

Functions are discovered by introspection -- any public function in
derivations.py whose first parameter is named 'db' -- so a NEW aggregate
added later is picked up and checked automatically. Nothing to remember
to register; the only manual step is adding a name to EXEMPT, and only
for a function whose entire job is to summarize income.

Honest boundary: this only catches a MANDATORY filter going missing (no
other guard exists at all, e.g. spending_summary's original bug) --
verified by temporarily reverting that exact fix and confirming this
test fails. It will NOT catch a regression in a DEFENSE-IN-DEPTH filter
(compute_balance, settle_up's covered-rows query), because ordinary
seed_income.py fixtures never produce the invariant-1 violation (an
inflow with split rows) those filters guard against -- verified the same
way, and that gap is exactly what tests/test_income_isolation.py's
deliberately-mis-split fixtures exist to cover instead. The two suites
are complementary, not redundant."""
import inspect
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
sys.path.insert(0, str(REPO))

import derivations

# A name goes here only with a one-line reason for why THIS function is
# supposed to change when inflows exist.
EXEMPT = {
    "income_summary": "the income aggregate itself — the one derivation "
                      "whose entire job is to count inflows (INCOME-DESIGN)",
    "spending_summary": "nets income_type='refund' inflows against their "
                        "category (INCOME-DESIGN net-spend rule) — the one "
                        "SPEND derivation that reads inflows on purpose. The "
                        "exemption is bounded to refunds only; "
                        "test_income_isolation proves non-refund inflows "
                        "still never move spend.",
    "income_trend": "per-month income_summary over a trailing window — an "
                    "income aggregate by construction, counting inflows is "
                    "its whole job (same reason as income_summary).",
    "category_trend": "per-month NET category spend over a window — reads "
                      "refund inflows on purpose via spending_summary (same "
                      "bounded exemption), and takes a `category` arg so it "
                      "isn't a bare db-aggregate.",
}


def db_functions():
    """Every public function in derivations.py whose first parameter is
    named 'db' -- i.e. every aggregate expected to ignore inflows."""
    found = []
    for name, func in inspect.getmembers(derivations, inspect.isfunction):
        if name.startswith("_") or name in EXEMPT:
            continue
        params = list(inspect.signature(func).parameters)
        if params and params[0] == "db":
            found.append((name, func))
    return found


def _json_default(obj):
    """sqlite3.Row isn't natively JSON-serializable; converting it to a
    plain dict is what makes before/after comparison meaningful instead
    of comparing two different objects' memory addresses."""
    if isinstance(obj, sqlite3.Row):
        return dict(obj)
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


def _snapshot(db, found):
    return {name: json.dumps(func(db), default=_json_default, sort_keys=True)
            for name, func in found}


class DerivationIgnoresIncomeTripwireTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-tripwire-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "81", "--months", "3", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "seed_income.py"), str(self.db_path),
             "--seed", "81", "--months", "3", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_discovery_finds_the_known_aggregates_and_only_db_taking_functions(self):
        # A check on the discovery mechanism itself: if this silently
        # dropped to zero, the tripwire below would pass by checking
        # nothing at all.
        names = {name for name, _ in db_functions()}
        self.assertIn("compute_balance", names)
        # spending_summary is now EXEMPT (it nets refunds on purpose); the
        # bounded-exemption guard lives in test_income_isolation instead.
        self.assertNotIn("spending_summary", names)
        self.assertIn("spending_summary", EXEMPT)
        self.assertNotIn("round_ratio", names)  # takes (numerator, denominator), not db

    def test_no_derivation_changes_when_a_large_inflow_is_added(self):
        found = db_functions()
        self.assertTrue(found, "no db-taking functions discovered in derivations.py")
        before = _snapshot(self.db, found)

        self.db.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source, direction, income_type)
               VALUES (?, 999999999, 'tripwire paycheck', 'Other', 1, 0,
                       'simplefin', 'in', 'unclassified')""",
            (date.today().isoformat(),))
        self.db.commit()

        after = _snapshot(self.db, found)
        for name, _ in found:
            self.assertEqual(
                before[name], after[name],
                f"derivations.{name}(db) changed after an inflow was added — "
                "it's counting income as if it were spending or balance. If "
                "this function is SUPPOSED to include inflows, add its name "
                "to EXEMPT at the top of this file with a one-line reason.")


if __name__ == "__main__":
    unittest.main()
