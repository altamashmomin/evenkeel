"""Recurring-charge / subscription detection (analytics Tier B #13).

Two axes: the merchant normalizer (noisy bank descriptions → a stable key,
biased to UNDER-merge so it never invents a phantom subscription), and the
recurring_charges derivation (same merchant + identical amount + ≥3 charges on a
regular cadence → a suggestion). Outflows only, settlements excluded; inflow
isolation is also guarded generically by the derivation tripwire."""
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
sys.path.insert(0, str(REPO))

import derivations


class RecurringDerivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-recurring-")
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

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _charge(self, desc, when, cents, direction="out", source="simplefin"):
        self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, direction, income_type) "
            "VALUES (?, ?, ?, 'Other', 1, 0, ?, ?, NULL)",
            (when, cents, desc, source, direction))
        self.db.commit()

    def _by_merchant(self, merchant):
        for c in derivations.recurring_charges(self.db):
            if c["merchant"] == merchant:
                return c
        return None

    # ---- the normalizer ------------------------------------------------------
    def test_normalizer_collapses_bank_noise_without_false_merges(self):
        n = derivations._normalize_merchant
        self.assertEqual("NETFLIX", n("NETFLIX.COM"))
        self.assertEqual("WAWA", n("WAWA# 8323"))
        self.assertEqual("WAWA", n("WAWA 8323"))          # two forms → one key
        self.assertEqual("NJM INSURANCE",
                         n("NJM Insurance    DES:Web Pay    ID:XXX   INDN:CHARLEE A"))
        self.assertEqual("", n(""))
        # distinct merchants must NOT collapse — a false merge is a phantom sub
        self.assertNotEqual(n("NETFLIX.COM"), n("SPOTIFY USA"))

    # ---- detection -----------------------------------------------------------
    def test_detects_a_monthly_subscription_with_next_date(self):
        for d in ("2026-01-05", "2026-02-04", "2026-03-06"):   # ~30-day gaps
            self._charge("QUARKSTREAM SUBSCRIPTION", d, 1549)
        got = self._by_merchant("Quarkstream Subscription")
        self.assertIsNotNone(got)
        self.assertEqual(3, got["occurrences"])
        self.assertEqual(1549, got["amount_cents"])
        self.assertEqual(30, got["interval_days"])
        self.assertEqual("monthly", got["cadence"])
        self.assertEqual("2026-03-06", got["last_charge"])
        self.assertEqual("2026-04-05", got["predicted_next"])   # last + 30 days

    def test_labels_weekly_and_yearly_cadences(self):
        for d in ("2026-01-01", "2026-01-08", "2026-01-15", "2026-01-22"):  # 7d
            self._charge("ZORPFIT GYM", d, 2000)
        self.assertEqual("weekly", self._by_merchant("Zorpfit Gym")["cadence"])
        for d in ("2024-02-01", "2025-02-01", "2026-02-01"):    # ~365d
            self._charge("YEARLYCO RENEWAL", d, 9900)
        self.assertEqual("yearly", self._by_merchant("Yearlyco Renewal")["cadence"])

    # ---- conservatism (suggest, don't assert) --------------------------------
    def test_irregular_gaps_are_not_flagged(self):
        for d in ("2026-01-01", "2026-01-05", "2026-03-01"):    # gaps 4, 55
            self._charge("RANDOMSHOP PLACE", d, 1000)
        self.assertIsNone(self._by_merchant("Randomshop Place"))

    def test_two_occurrences_is_not_enough(self):
        for d in ("2026-01-01", "2026-01-31"):
            self._charge("DUOSHOP INC", d, 500)
        self.assertIsNone(self._by_merchant("Duoshop Inc"))

    def test_same_merchant_varying_amounts_not_flagged(self):
        # regular cadence but 3 different amounts → no same-amount cluster of 3
        for d, cents in (("2026-01-01", 1000), ("2026-01-31", 1200), ("2026-03-02", 1100)):
            self._charge("VARYMART STORE", d, cents)
        self.assertIsNone(self._by_merchant("Varymart Store"))

    def test_same_day_duplicates_count_once(self):
        self._charge("DEDUPE SUB", "2026-01-01", 800)
        self._charge("DEDUPE SUB", "2026-01-01", 800)   # same day → one event
        self._charge("DEDUPE SUB", "2026-01-31", 800)
        self.assertIsNone(self._by_merchant("Dedupe Sub"))   # only 2 distinct days

    # ---- isolation -----------------------------------------------------------
    def test_inflows_are_ignored(self):
        # a regular, same-amount INFLOW must never look like a recurring charge
        for d in ("2026-01-01", "2026-01-31", "2026-03-02"):
            self._charge("PAYCHECKCO DEPOSIT", d, 500000, direction="in")
        self.assertIsNone(self._by_merchant("Paycheckco Deposit"))

    def test_settlements_are_excluded(self):
        for d in ("2026-01-01", "2026-01-31", "2026-03-02"):
            self._charge("SETTLE THING", d, 2500, source="settlement")
        self.assertIsNone(self._by_merchant("Settle Thing"))


class RecurringRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-recurring-route-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "61", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        # plant one clean monthly subscription
        db = sqlite3.connect(self.db_path)
        for d in ("2026-01-05", "2026-02-04", "2026-03-06"):
            db.execute(
                "INSERT INTO transactions (txn_date, amount_cents, description, "
                "category, paid_by, is_shared, source, direction, income_type) "
                "VALUES (?, 1549, 'QUARKSTREAM SUBSCRIPTION', 'Other', 1, 0, "
                "'simplefin', 'out', NULL)", (d,))
        db.commit()
        db.close()
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "recurring-route-secret")
        spec = importlib.util.spec_from_file_location("app_recurring_route_test", REPO / "app.py")
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
        r = self.client().get("/api/analytics/recurring")
        self.assertEqual(200, r.status_code)
        body = r.get_json()
        self.assertIn("recurring", body)
        got = next(x for x in body["recurring"] if x["merchant"] == "Quarkstream Subscription")
        self.assertEqual({"cents", "display"}, set(got["amount"]))
        self.assertEqual(1549, got["amount"]["cents"])
        self.assertEqual("$15.49", got["amount"]["display"])
        self.assertEqual("monthly", got["cadence"])
        self.assertEqual("2026-04-05", got["predicted_next"])

    def test_requires_auth(self):
        c = self.app_module.app.test_client()   # no session
        self.assertEqual(401, c.get("/api/analytics/recurring").status_code)


if __name__ == "__main__":
    unittest.main()
