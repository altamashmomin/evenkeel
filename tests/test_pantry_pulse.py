"""deploy/pantry_pulse.py — the Pi-side weekly digest job. Its formatter is a
pure function (pulse JSON + today → title/body/quiet), so it is tested here
without any network: horizon and grace applied, snoozed rows skipped, quiet
weeks detected, money displayed verbatim from the API's `display` strings."""
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pantry_pulse", REPO / "deploy" / "pantry_pulse.py")
pulse_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pulse_mod)


def fixture():
    return {
        "list_count": 2, "list_total": {"cents": 1200, "display": "$12.00"},
        "priced_count": 1, "unpriced_count": 1,
        "list": [
            {"name": "Coffee", "kind": "staple", "status": "out", "store": "Costco",
             "typical": {"cents": 1200, "display": "$12.00"}, "snoozed_until": None},
            {"name": "Candles", "kind": "oneoff", "status": "out", "store": None,
             "typical": None, "snoozed_until": "2026-09-30"},          # snoozed
        ],
        "due_soon": [
            {"name": "Milk", "store": None, "predicted_date": "2026-08-24",
             "interval_source": "status", "typical": {"cents": 500, "display": "$5.00"}},
            {"name": "Rice", "store": None, "predicted_date": "2026-10-01",
             "interval_source": "cadence", "typical": None},            # beyond horizon
        ],
        "stale_staples": [
            {"name": "Moon dust", "last_activity": "2026-01-10"},       # 7+ months
            {"name": "Star salt", "last_activity": "2026-08-01"},       # recent
        ],
        "stale_shopping_items": [],
        "on_the_way": [{"name": "Dog food", "updated_at": "2026-08-10T12:00:00+00:00"}],
        "new_staple_suggestion": {"merchant": "Pet Barn", "purchases_seen": 4,
                                  "last_purchase": "2026-08-15",
                                  "total_spent": {"cents": 9000, "display": "$90.00"}},
        "unmatched_count": 1,
    }


class PantryPulseFormatterTests(unittest.TestCase):
    def test_applies_horizon_grace_and_snooze(self):
        title, body, quiet = pulse_mod.render_markdown(fixture(), "2026-08-21")
        self.assertFalse(quiet)
        self.assertIn("On the list — 1", body)            # snoozed Candles skipped
        self.assertIn("**Coffee** — out · @ Costco · ~$12.00", body)
        self.assertIn("Coming due within 7 days — 1", body)
        self.assertIn("**Milk** — in 3 days", body)
        self.assertNotIn("Rice", body)                      # beyond the horizon
        self.assertIn("Still tracking these? — 1", body)
        self.assertIn("Moon dust", body)
        self.assertNotIn("Star salt", body)                 # inside the grace
        self.assertIn("## On the way — 1", body)
        self.assertIn("**Dog food** — ordered 2026-08-10 (11 days ago) — still waiting?", body)
        self.assertIn("**Pet Barn** — bought 4×", body)
        self.assertIn("$90.00", body)
        self.assertIn("1 staple(s) have never matched", body)
        self.assertEqual("Pantry pulse — 2026-08-21: 1 on the list, 1 coming due", title)

    def test_quiet_week_posts_nothing(self):
        p = fixture()
        p.update(list=[], due_soon=[], stale_staples=[], new_staple_suggestion=None,
                 unmatched_count=0, list_count=0, on_the_way=[])
        _, _, quiet = pulse_mod.render_markdown(p, "2026-08-21")
        self.assertTrue(quiet)

    def test_main_refuses_without_a_token(self):
        import os
        os.environ.pop("PANTRY_PULSE_TOKEN", None)
        self.assertEqual(2, pulse_mod.main(["--dry-run"]))


if __name__ == "__main__":
    unittest.main()
