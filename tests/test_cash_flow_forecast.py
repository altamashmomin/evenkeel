"""Cash-flow forecast (analytics Tier B #14) — the conservative month-end
NET CASH FLOW floor: income_summary's net-so-far minus the bills still unpaid
this period. Bills-only (no inferred paycheck), net flow (not a balance), goals
excluded. EXEMPT in the derivation tripwire (it counts income by construction),
so income-sensitivity is proven here instead."""
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"
PERIOD = "2026-03"
sys.path.insert(0, str(REPO))

import derivations


class CashFlowForecastTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-forecast-")
        self.db_path = Path(self.tmp.name) / "test.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "51", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        # wipe seed transactions/bills so the period is fully controlled
        self.db.execute("DELETE FROM transactions")
        self.db.execute("DELETE FROM bills")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _txn(self, when, cents, direction="out", income_type=None, source="simplefin"):
        cur = self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction, income_type) "
            "VALUES (?, ?, 'x', 'Other', 1, 0, ?, ?, ?)",
            (when, cents, source, direction, income_type))
        self.db.commit()
        return cur.lastrowid

    def _bill(self, name, cents, due_day):
        cur = self.db.execute(
            "INSERT INTO bills (name, amount_cents, due_day, category, active) "
            "VALUES (?, ?, ?, 'Bills', 1)", (name, cents, due_day))
        self.db.commit()
        return cur.lastrowid

    def _pay(self, bill_id, when, cents):
        txn = self._txn(when, cents, source="bill")
        self.db.execute(
            "INSERT INTO bill_payments (bill_id, period, paid_on, txn_id) "
            "VALUES (?, ?, ?, ?)", (bill_id, PERIOD, when, txn))
        self.db.commit()

    def test_floor_is_net_so_far_minus_unpaid_bills(self):
        # income + spend this month
        self._txn("2026-03-10", 300000, direction="in", income_type="paycheck")
        self._txn("2026-03-12", 20000)                       # groceries
        # three bills: Rent paid, Water + Internet still due
        rent = self._bill("Rent", 100000, 1)
        self._bill("Water", 3000, 3)
        self._bill("Internet", 5000, 15)
        self._pay(rent, "2026-03-01", 100000)                # counts as spend too

        f = derivations.cash_flow_forecast(self.db, PERIOD)
        # net so far: 300000 income − (100000 rent + 20000 groceries) spend
        self.assertEqual(300000, f["income_so_far_cents"])
        self.assertEqual(120000, f["spend_so_far_cents"])
        self.assertEqual(180000, f["net_so_far_cents"])
        # only the two UNPAID bills remain (Rent is paid → already in spend)
        self.assertEqual(8000, f["bills_remaining_cents"])
        self.assertEqual(172000, f["projected_net_cents"])   # 180000 − 8000
        # listed soonest-due first, Rent absent
        self.assertEqual(["Water", "Internet"],
                         [b["name"] for b in f["bills_remaining"]])
        self.assertEqual([3, 15], [b["due_day"] for b in f["bills_remaining"]])

    def test_no_unpaid_bills_means_projected_equals_net_so_far(self):
        self._txn("2026-03-10", 250000, direction="in", income_type="paycheck")
        self._txn("2026-03-12", 40000)
        paid = self._bill("Rent", 100000, 1)
        self._pay(paid, "2026-03-01", 100000)
        f = derivations.cash_flow_forecast(self.db, PERIOD)
        self.assertEqual([], f["bills_remaining"])
        self.assertEqual(0, f["bills_remaining_cents"])
        self.assertEqual(f["net_so_far_cents"], f["projected_net_cents"])

    def test_forecast_reflects_income_justifying_the_tripwire_exemption(self):
        self._bill("Internet", 5000, 15)                     # one unpaid bill
        before = derivations.cash_flow_forecast(self.db, PERIOD)["projected_net_cents"]
        self._txn("2026-03-10", 200000, direction="in", income_type="paycheck")
        after = derivations.cash_flow_forecast(self.db, PERIOD)["projected_net_cents"]
        # a paycheck moves the number UP — this is why it's EXEMPT, not a leak
        self.assertEqual(before + 200000, after)


class CashFlowForecastRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-forecast-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "61", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        db = sqlite3.connect(self.db_path)
        db.execute("DELETE FROM bills")          # isolate from seed bills
        db.execute("INSERT INTO bills (name, amount_cents, due_day, category, active) "
                   "VALUES ('Internet', 5000, 15, 'Bills', 1)")
        db.commit()
        db.close()
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "forecast-route-secret")
        spec = importlib.util.spec_from_file_location("app_forecast_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def client(self):
        c = self.app_module.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = 1
        return c

    def test_endpoint_shape_and_money(self):
        r = self.client().get("/api/analytics/cash-flow-forecast?period=2026-03")
        self.assertEqual(200, r.status_code)
        body = r.get_json()
        self.assertEqual(
            {"period", "income_so_far", "spend_so_far", "net_so_far",
             "bills_remaining", "bills_remaining_total", "projected_net"},
            set(body))
        self.assertEqual({"cents", "display"}, set(body["projected_net"]))
        internet = body["bills_remaining"][0]
        self.assertEqual("Internet", internet["name"])
        self.assertEqual({"cents", "display"}, set(internet["amount"]))
        self.assertEqual("$50.00", internet["amount"]["display"])

    def test_requires_auth(self):
        c = self.app_module.app.test_client()
        self.assertEqual(401, c.get("/api/analytics/cash-flow-forecast").status_code)


if __name__ == "__main__":
    unittest.main()
