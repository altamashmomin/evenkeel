"""deploy/change_digest.py — the Pi-side daily digest job (NOTIFICATIONS-DESIGN
inc 2). Its formatter is a pure function (digest JSON -> title/body/quiet), so it
is tested here without any network: the sync/human split, the terse rule (no
amounts ever), quiet detection, and the high-water-mark state round-trip."""
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "change_digest", REPO / "deploy" / "change_digest.py")
cd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cd)


def digest(**over):
    d = {
        "since": "2026-08-30T08:00:00+00:00", "until": "2026-08-31T08:00:00+00:00",
        "total": 8, "assistant_and_human_writes": 3, "sync_writes": 5,
        "by_actor": [{"actor": "mcp:cc", "count": 2},
                     {"actor": "ui:avery", "count": 1},
                     {"actor": "sync", "count": 5}],
        "by_action": [{"action": "record_transaction", "count": 5},
                      {"action": "set_budget", "count": 1},
                      {"action": "classify_inflow", "count": 1},
                      {"action": "set_rule_enabled", "count": 1}],
        "pending_approvals": 1,
    }
    d.update(over)
    return d


class ChangeDigestRenderTests(unittest.TestCase):
    def test_full_digest_splits_sync_and_stays_terse(self):
        title, body, quiet = cd.render_markdown(digest())
        self.assertFalse(quiet)
        self.assertIn("3 changes", title)
        self.assertIn("awaiting approval", title)
        # the two human/assistant actors are listed; sync is NOT a bullet
        self.assertIn("**mcp:cc**", body)
        self.assertIn("**ui:avery**", body)
        self.assertNotIn("- **sync**", body)
        # kinds are the friendly labels, and the sync verb is not itemized
        self.assertIn("set a budget", body)
        self.assertIn("tagged a deposit", body)
        self.assertNotIn("record_transaction", body)
        # sync is a one-line footnote
        self.assertIn("5 routine bank-feed updates", body)
        # TERSE: never an amount
        self.assertNotIn("$", body)

    def test_quiet_when_only_the_bank_feed_moved(self):
        _, _, quiet = cd.render_markdown(
            digest(assistant_and_human_writes=0, pending_approvals=0,
                   by_actor=[{"actor": "sync", "count": 3}],
                   by_action=[{"action": "record_transaction", "count": 3}],
                   sync_writes=3))
        self.assertTrue(quiet)

    def test_pending_only_is_not_quiet(self):
        title, body, quiet = cd.render_markdown(
            digest(assistant_and_human_writes=0, sync_writes=0,
                   by_actor=[], by_action=[], pending_approvals=2))
        self.assertFalse(quiet)
        self.assertIn("2 proposals awaiting your approval", body)
        self.assertIn("Pending approvals", body)
        self.assertNotIn("by you & the assistants", body)  # no changes section

    def test_singular_grammar(self):
        title, body, _ = cd.render_markdown(
            digest(assistant_and_human_writes=1, pending_approvals=1,
                   by_actor=[{"actor": "ui:avery", "count": 1}],
                   by_action=[{"action": "set_budget", "count": 1}], sync_writes=0))
        self.assertIn("1 change", title)
        self.assertNotIn("1 changes", title)
        self.assertIn("1 proposal awaiting your approval", body)


class ChangeDigestStateTests(unittest.TestCase):
    def test_high_water_mark_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".change-digest.state"
            cd.write_since(str(path), "2026-08-31T08:00:00+00:00")
            self.assertEqual("2026-08-31T08:00:00+00:00",
                             cd.read_since(str(path), 24))

    def test_first_run_with_no_state_falls_back_to_lookback(self):
        with tempfile.TemporaryDirectory() as d:
            got = cd.read_since(str(Path(d) / "nope.state"), 24)
            # a valid ISO time, and clearly earlier than "now"
            self.assertLess(datetime.fromisoformat(got),
                            datetime.fromisoformat(cd._now_iso()))


if __name__ == "__main__":
    unittest.main()
