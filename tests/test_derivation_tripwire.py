"""Automated tripwire for the bug class the sync-flip session found by
hand: no function in derivations.py may change its answer when an inflow
is added, unless it's explicitly allowed to (a genuine income aggregate
would say so here, with a reason).

Functions are discovered by introspection -- any public function in
derivations.py whose first parameter is named 'db' -- so a NEW aggregate
added later is picked up and checked automatically. Nothing to remember
to register; the only manual step is adding a name to EXEMPT, and only
for a function whose entire job is to summarize income.

The fixture is deliberately RICH so the probe can actually contaminate the
functions it covers (CODE-REVIEW-2026-08-07 #8): setUp seeds a few `items`
(a low staple, an out staple, a stocked staple, all named to match a
merchant) and the probe adds THREE inflows in a shopping category, on an
even in-window cadence, whose description matches those staples. Without
that data the pantry derivations returned [] before and after and the
tripwire was checking [] == [] -- vacuous. With it, removing the repo's
`direction = 'out'` filters is caught on 8 of the 15 discovered functions
(last_shopping_trip, recurring_charges, restock_forecast,
restock_suggestions, stale_shopping_items, staple_spend, top_merchants,
unmatched_staples) -- verified by the mutation.

Honest boundary: this catches a MANDATORY filter going missing. The 7
functions it does NOT move are provably not contaminatable by an inflow
probe, for structural reasons, not gaps:
  - compute_balance / member_breakdown -- protected by a splits INNER JOIN;
    an inflow writes no splits, so the probe can't reach them. This is a
    DEFENSE-IN-DEPTH filter, and tests/test_income_isolation.py's
    deliberately-mis-split fixtures (an inflow WITH split rows) cover it
    instead. The two suites are complementary, not redundant.
  - low_stock / shopping_list / goal_pace -- read items/goals only, never
    transactions, so no inflow can move them.
  - bill_variance -- with period=None matches no payment by construction.
  - new_staple_suggestions -- excludes already-tracked and fixed-amount
    (subscription-shaped) merchants, which the matching probe is; its own
    hand-written test covers its inflow-invariance.
Both the mutation coverage and the no-false-positive property (the REAL
code shows before == after) were verified before landing this."""
import inspect
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
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
    "savings_rate_trend": "savings rate over a window (raw + rolling) — built "
                          "on income_trend/income_summary, counting inflows "
                          "is its whole job (same as income_trend).",
    "cash_flow_forecast": "projected month-end NET CASH FLOW (in − out) — "
                          "built on income_summary, so it counts income by "
                          "construction; the bills half is separately guarded "
                          "by bill_variance's own tripwire check.",
    "anomaly_flags": "category-overage flags vs a trailing baseline, read "
                     "through spending_summary — so category spend is refund-"
                     "NETTED (same bounded exemption as category_trend); a "
                     "refund can legitimately clear an anomaly.",
    "budget_status": "category budgets vs actual net spend, read through "
                     "spending_summary — so 'actual' is refund-NETTED (same "
                     "bounded exemption as category_trend); a refund can "
                     "legitimately bring a category back under budget. Takes a "
                     "`period` arg, so it isn't a bare db-aggregate either.",
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
        self._seed_probe_items()

    # A low staple, an out staple, and a stocked one — all named so the probe's
    # matching purchases (below) reach the pantry derivations. updated_at is
    # in-window so restock_suggestions' "purchase since it ran low" can match.
    PROBE_ITEMS = [("Tripwire Beans", "low"), ("Tripwire Soap", "out"),
                   ("Tripwire Rice", "stocked")]
    # Three inflows the tripwire adds: a shopping category (so last_shopping_trip
    # is reachable), an EVEN in-window cadence (so recurring_charges is
    # reachable), a large amount (so top_merchants is reachable), a description
    # matching the staples (so the restock/staple derivations are reachable), and
    # income_type != 'refund' (so the refund-netting EXEMPT set stays inert).
    PROBE_DATES = ("2026-07-22", "2026-07-26", "2026-07-30")

    def _seed_probe_items(self):
        for name, status in self.PROBE_ITEMS:
            self.db.execute(
                "INSERT INTO items (name, kind, status, active, created_at, "
                "updated_at) VALUES (?, 'staple', ?, 1, '2026-07-01', '2026-07-01')",
                (name, status))
        self.db.commit()

    def _add_probe_inflows(self):
        for d in self.PROBE_DATES:
            self.db.execute(
                """INSERT INTO transactions
                   (txn_date, amount_cents, description, category, paid_by,
                    is_shared, source, direction, income_type)
                   VALUES (?, 999999999, 'TRIPWIRE BEANS CO', 'Groceries', 1, 0,
                           'simplefin', 'in', 'unclassified')""", (d,))
        self.db.commit()

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

    def test_no_derivation_changes_when_inflows_are_added(self):
        found = db_functions()
        self.assertTrue(found, "no db-taking functions discovered in derivations.py")
        before = _snapshot(self.db, found)

        self._add_probe_inflows()

        after = _snapshot(self.db, found)
        # Collect ALL contaminated functions (not just the first), so a missing
        # filter shows its full blast radius in one run.
        changed = [name for name, _ in found if before[name] != after[name]]
        self.assertEqual(
            [], changed,
            f"derivations {changed} changed after inflows were added — they are "
            "counting income as if it were spending or balance. If a function is "
            "SUPPOSED to include inflows, add its name to EXEMPT at the top of "
            "this file with a one-line reason.")


if __name__ == "__main__":
    unittest.main()
