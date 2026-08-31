"""deploy/notify_approvals.py — the Pi-side approval-alert job (NOTIFICATIONS-
DESIGN inc 3). Its formatter is pure (pending list -> title/body), tested here
without network: terse (kind + who + expiry, never the match phrase or an
amount), grammar, and the announced-tokens state (round-trip + prune)."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "notify_approvals", REPO / "deploy" / "notify_approvals.py")
na = importlib.util.module_from_spec(spec)
spec.loader.exec_module(na)


def pend(token, action_type="create_rule", proposed_by="cc",
         expires_at="2026-08-31T20:26:23+00:00",
         summary="tag SECRET PAYEE as paycheck"):
    return {"token": token, "action_type": action_type,
            "proposed_by": proposed_by, "expires_at": expires_at,
            "summary": summary, "detail": "…"}


class RenderTests(unittest.TestCase):
    def test_single_is_terse_and_hides_the_match_phrase(self):
        title, body = na.render_issue([pend("t1")])
        self.assertIn("1 proposal awaiting your approval", title)
        self.assertIn("a new auto-tagging rule", body)
        self.assertIn("by cc", body)
        # the summary / match phrase (a possible payee) must NEVER reach the issue
        self.assertNotIn("SECRET PAYEE", body)
        self.assertNotIn("$", body)

    def test_apply_rules_kind_and_in_app_proposer(self):
        _, body = na.render_issue(
            [pend("t1", action_type="apply_rules", proposed_by="in the app")])
        self.assertIn("a backlog sweep", body)
        self.assertIn("proposed in the app", body)

    def test_plural_grammar(self):
        title, body = na.render_issue([pend("t1"), pend("t2")])
        self.assertIn("2 proposals awaiting your approval", title)
        self.assertIn("these", body)


class StateTests(unittest.TestCase):
    def test_announced_round_trip_and_prune(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".notify-approvals.state"
            na.write_announced(str(p), {"a", "b"})
            self.assertEqual({"a", "b"}, na.read_announced(str(p)))
            na.write_announced(str(p), {"b"})            # a later run prunes
            self.assertEqual({"b"}, na.read_announced(str(p)))

    def test_missing_state_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(set(), na.read_announced(str(Path(d) / "nope.state")))


if __name__ == "__main__":
    unittest.main()
