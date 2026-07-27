"""income_trend derivation and the shared _monthly_series engine it rides.

Two things under test: (1) _monthly_series is a *generic* month-bucketing
mapper — it knows nothing about income, proven by driving it with a trivial
metric_fn; (2) income_trend = _monthly_series over income_summary, so each
month's figures mean exactly what the dashboard card's do, refund netting
included. Fixtures are hand-built so every expected number is exact."""
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

A, B, C = "2026-05", "2026-06", "2026-07"   # C is the anchor month


class MonthWindowTests(unittest.TestCase):
    def test_trailing_window_is_chronological_and_inclusive(self):
        self.assertEqual(
            ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07"],
            derivations._month_window("2026-07", 6))

    def test_window_crosses_the_year_boundary(self):
        self.assertEqual(
            ["2025-11", "2025-12", "2026-01"],
            derivations._month_window("2026-01", 3))

    def test_window_of_one_is_just_the_anchor(self):
        self.assertEqual(["2026-03"], derivations._month_window("2026-03", 1))


class MonthlySeriesEngineTests(unittest.TestCase):
    """The engine is generic: it must work with ANY per-month metric_fn,
    not just income_summary. Driving it with a trivial function is the whole
    point — increments 7-16 depend on this genericity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-series-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "63", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def outflow(self, month, amount_cents=1000):
        self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, ?, 'spend', 'Groceries', 1, 0, 'manual', 'out')""",
            (f"{month}-10", amount_cents))
        self.db.commit()

    def test_maps_an_arbitrary_metric_over_the_window_in_order(self):
        # A metric_fn that echoes its month proves the engine is content-blind.
        series = derivations._monthly_series(
            self.db, lambda db, m: {"echo": m}, months_back=3, anchor=C)
        self.assertEqual(
            [{"month": A, "echo": A}, {"month": B, "echo": B}, {"month": C, "echo": C}],
            series)

    def test_includes_empty_months_so_the_axis_is_continuous(self):
        calls = []
        derivations._monthly_series(
            self.db, lambda db, m: calls.append(m) or {}, months_back=3, anchor=C)
        self.assertEqual([A, B, C], calls)  # every month visited, data or not

    def test_anchor_defaults_to_latest_data_month(self):
        self.outflow(B)   # latest (and only) data month is B
        series = derivations._monthly_series(
            self.db, lambda db, m: {}, months_back=2)
        self.assertEqual([A, B], [e["month"] for e in series])

    def test_empty_database_yields_empty_series(self):
        self.assertEqual([], derivations._monthly_series(self.db, lambda db, m: {}))

    def test_latest_data_month_is_none_when_empty(self):
        self.assertIsNone(derivations._latest_data_month(self.db))
        self.outflow(A)
        self.outflow(C)
        self.assertEqual(C, derivations._latest_data_month(self.db))


class IncomeTrendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-income-trend-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "64", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("DELETE FROM splits")
        self.db.execute("DELETE FROM transactions")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def outflow(self, month, amount_cents, category="Groceries"):
        self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction)
               VALUES (?, ?, 'spend', ?, 1, 0, 'manual', 'out')""",
            (f"{month}-10", amount_cents, category))
        self.db.commit()

    def inflow(self, month, amount_cents, income_type):
        self.db.execute(
            """INSERT INTO transactions (txn_date, amount_cents, description,
                   category, paid_by, is_shared, source, direction, income_type)
               VALUES (?, ?, 'income', 'Other', 1, 0, 'simplefin', 'in', ?)""",
            (f"{month}-05", amount_cents, income_type))
        self.db.commit()

    def test_per_month_income_vs_net_spend_over_the_window(self):
        # C: spend 5000, paycheck 300000, refund 2000 -> net spend 3000.
        self.outflow(C, 5000)
        self.inflow(C, 300000, "paycheck")
        self.inflow(C, 2000, "refund")
        # B: spend 4000, paycheck 250000.
        self.outflow(B, 4000)
        self.inflow(B, 250000, "paycheck")
        # A: nothing at all -> a zero month.
        series = derivations.income_trend(self.db, months_back=3, anchor=C)
        self.assertEqual([A, B, C], [e["month"] for e in series])

        a, b, c = series
        self.assertEqual((0, 0, 0, None), (a["true_income_cents"],
            a["month_spend_cents"], a["net_cash_flow_cents"], a["savings_rate"]))
        self.assertEqual((250000, 4000, 246000), (b["true_income_cents"],
            b["month_spend_cents"], b["net_cash_flow_cents"]))
        # Refund netting flows straight through: month_spend is 3000, not 5000.
        self.assertEqual((300000, 3000, 297000), (c["true_income_cents"],
            c["month_spend_cents"], c["net_cash_flow_cents"]))
        self.assertEqual(302000, c["gross_inflows_cents"])  # refund is money in

    def test_default_anchor_is_the_latest_data_month(self):
        self.outflow(B, 4000)
        self.inflow(B, 250000, "paycheck")
        series = derivations.income_trend(self.db, months_back=2)
        self.assertEqual([A, B], [e["month"] for e in series])
        self.assertEqual(250000, series[-1]["true_income_cents"])

    def test_window_scopes_out_months_outside_it(self):
        self.inflow("2026-01", 999999, "paycheck")   # far outside a 2-month window
        self.outflow(C, 5000)
        series = derivations.income_trend(self.db, months_back=2, anchor=C)
        self.assertEqual([B, C], [e["month"] for e in series])
        self.assertTrue(all(e["true_income_cents"] == 0 for e in series))


if __name__ == "__main__":
    unittest.main()
