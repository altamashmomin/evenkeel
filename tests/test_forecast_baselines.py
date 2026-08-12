"""Forecast-lab baselines (scenario planning): per-category average monthly
NET spend + average monthly paycheck income over a trailing window. The
derivation states measured facts only — every what-if (sliders, income
override, horizon) is client-side — so all that's tested here is the
measurement: window math, refund netting, the paycheck-only income rule,
the positive-total filter, and the clock-free anchor default. EXEMPT in the
derivation tripwire (it counts income by construction), so its income-
sensitivity contract is proven here instead."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _seedbase

import derivations

A, B, C = "2026-05", "2026-06", "2026-07"


class ForecastBaselinesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-fc-baselines-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=73, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        # wipe seed rows so every month in the window is fully controlled
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _txn(self, when, cents, category="Groceries", direction="out",
             income_type=None, source="simplefin"):
        self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction, income_type) "
            "VALUES (?, ?, 'x', ?, 1, 0, ?, ?, ?)",
            (when, cents, category, source, direction, income_type))
        self.db.commit()

    def test_category_average_is_window_total_over_full_window(self):
        self._txn(f"{A}-10", 30000)               # Groceries: 300 + 0 + 150
        self._txn(f"{C}-10", 15000)
        out = derivations.forecast_baselines(self.db, months_back=3, anchor=C)
        self.assertEqual([A, B, C], out["months"])
        cat = out["categories"][0]
        self.assertEqual("Groceries", cat["category"])
        self.assertEqual(45000, cat["total_cents"])
        # 45000 / 3 — the empty month B drags the average down (run rate)
        self.assertEqual(15000, cat["avg_cents"])

    def test_income_average_counts_paychecks_only(self):
        self._txn(f"{B}-05", 200000, direction="in", income_type="paycheck")
        self._txn(f"{B}-06", 999999, direction="in", income_type="gift")
        self._txn(f"{C}-05", 100000, direction="in", income_type="unclassified")
        out = derivations.forecast_baselines(self.db, months_back=2, anchor=C)
        # only the paycheck counts: 200000 over 2 months
        self.assertEqual(100000, out["income_avg_cents"])

    def test_refunds_net_against_their_category(self):
        self._txn(f"{C}-02", 40000)
        self._txn(f"{C}-20", 10000, direction="in", income_type="refund")
        out = derivations.forecast_baselines(self.db, months_back=1, anchor=C)
        self.assertEqual(30000, out["categories"][0]["total_cents"])

    def test_non_positive_categories_are_omitted(self):
        # a refund-dominated category has no run rate to put a lever on
        self._txn(f"{C}-02", 5000, category="Returns")
        self._txn(f"{C}-20", 9000, category="Returns",
                  direction="in", income_type="refund")
        self._txn(f"{C}-03", 1000, category="Coffee")
        out = derivations.forecast_baselines(self.db, months_back=1, anchor=C)
        self.assertEqual(["Coffee"], [c["category"] for c in out["categories"]])

    def test_categories_sorted_by_average_descending_then_name(self):
        self._txn(f"{C}-01", 5000, category="Zed")
        self._txn(f"{C}-02", 20000, category="Rent")
        self._txn(f"{C}-03", 5000, category="Apples")
        out = derivations.forecast_baselines(self.db, months_back=1, anchor=C)
        self.assertEqual(["Rent", "Apples", "Zed"],
                         [c["category"] for c in out["categories"]])

    def test_anchor_defaults_to_latest_data_month(self):
        self._txn(f"{B}-10", 12000)
        out = derivations.forecast_baselines(self.db, months_back=2)
        self.assertEqual(B, out["anchor"])
        self.assertEqual([A, B], out["months"])

    def test_out_of_window_months_are_excluded(self):
        self._txn(f"{A}-10", 77700)               # outside a 1-month window
        self._txn(f"{C}-10", 11100)
        out = derivations.forecast_baselines(self.db, months_back=1, anchor=C)
        self.assertEqual([C], out["months"])
        self.assertEqual(11100, out["categories"][0]["total_cents"])

    def test_settlements_do_not_count_as_spend(self):
        self._txn(f"{C}-05", 50000, category="Settle up", source="settlement")
        out = derivations.forecast_baselines(self.db, months_back=1, anchor=C)
        self.assertEqual([], out["categories"])

    def test_empty_database_yields_empty_shape(self):
        out = derivations.forecast_baselines(self.db)
        self.assertEqual({"anchor": None, "months": [], "income_avg_cents": 0,
                          "categories": []}, out)

    def test_averages_use_round_ratio_ties_to_even(self):
        # 100 + 25 = 125 cents over 2 months -> 62.5 -> 62 (ties to even)
        self._txn(f"{B}-01", 100)
        self._txn(f"{C}-01", 25)
        out = derivations.forecast_baselines(self.db, months_back=2, anchor=C)
        self.assertEqual(62, out["categories"][0]["avg_cents"])


if __name__ == "__main__":
    unittest.main()
