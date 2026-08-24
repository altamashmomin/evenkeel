"""The .ics feed edge: /calendar/<token>.ics + /api/calendar/link.

The token is DERIVED — HMAC(SECRET_KEY, member id), never stored — so there is
no schema, nothing to revoke row-by-row, and rotating SECRET_KEY kills every
link at once. The feed route is the app's only token-in-URL surface (calendar
apps can't hold a session), so it must 404 on a wrong token, serve
text/calendar with no-store on a right one, and hand each member their own
URL only over a browser session (bearer tokens must not mint feed URLs).
Also covers the pure ICS renderer: RFC 5545 escaping, folding, CRLF."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

import _seedbase

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


class CalendarRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-calroute-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=74, months=1)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "calendar-route-test-secret")
        spec = importlib.util.spec_from_file_location(
            "app_calendar_route_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, logged_in=True):
        client = self.app_module.app.test_client()
        if logged_in:
            with client.session_transaction() as session:
                session["user_id"] = 1
        return client

    # ------------------------------------------------------------ the feed

    def test_wrong_token_is_a_plain_404(self):
        response = self.client(logged_in=False).get("/calendar/not-a-token.ics")
        self.assertEqual(404, response.status_code)

    def test_valid_token_serves_the_calendar_without_a_session(self):
        token = self.app_module._calendar_token(1)
        response = self.client(logged_in=False).get(f"/calendar/{token}.ics")
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.mimetype == "text/calendar")
        self.assertEqual("no-store", response.headers["Cache-Control"])
        body = response.get_data(as_text=True)
        self.assertTrue(body.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(body.endswith("END:VCALENDAR\r\n"))
        self.assertIn("X-WR-CALNAME:Ledger", body)
        # The seed carries active bills, so real events must be present.
        self.assertIn("BEGIN:VEVENT", body)
        self.assertIn("DTSTART;VALUE=DATE:", body)

    def test_each_active_member_token_works(self):
        for member_id in (1, 2):
            token = self.app_module._calendar_token(member_id)
            response = self.client(logged_in=False).get(f"/calendar/{token}.ics")
            self.assertEqual(200, response.status_code)

    # ------------------------------------------------------------ the link

    def test_link_requires_a_session(self):
        self.assertEqual(
            401, self.client(logged_in=False).get("/api/calendar/link").status_code)

    def test_link_hands_the_member_their_own_urls(self):
        body = self.client().get("/api/calendar/link").get_json()
        token = self.app_module._calendar_token(1)
        self.assertEqual(sorted(body), ["https", "webcal"])
        self.assertTrue(body["webcal"].startswith("webcal://"))
        self.assertIn(f"/calendar/{token}.ics", body["webcal"])
        self.assertIn(f"/calendar/{token}.ics", body["https"])

    def test_public_base_url_overrides_the_request_host(self):
        # Apple Calendar rewrites webcal:// to https:// — a plain-HTTP :8080
        # link can never subscribe. PUBLIC_BASE_URL points the links at the
        # HTTPS front door (Tailscale Serve) regardless of how the browser
        # reached the app.
        os.environ["PUBLIC_BASE_URL"] = "https://raspberrypi.tail1234.ts.net/"
        self.addCleanup(os.environ.pop, "PUBLIC_BASE_URL", None)
        body = self.client().get("/api/calendar/link").get_json()
        token = self.app_module._calendar_token(1)
        self.assertEqual(
            f"webcal://raspberrypi.tail1234.ts.net/calendar/{token}.ics",
            body["webcal"])
        self.assertEqual(
            f"https://raspberrypi.tail1234.ts.net/calendar/{token}.ics",
            body["https"])

    def test_tokens_differ_per_member_and_depend_on_the_secret(self):
        self.assertNotEqual(self.app_module._calendar_token(1),
                            self.app_module._calendar_token(2))

    # ------------------------------------------------------------ the renderer

    def test_ics_escape_and_fold(self):
        m = self.app_module
        self.assertEqual(r"a\\b\;c\,d\ne", m._ics_escape("a\\b;c,d\ne"))
        long = "SUMMARY:" + "x" * 200
        folded = m._ics_fold(long)
        for line in folded.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 74)
        self.assertEqual(long, folded.replace("\r\n ", ""))

    def test_fold_never_splits_inside_a_multibyte_char(self):
        folded = self.app_module._ics_fold("SUMMARY:" + "💸" * 40)
        for line in folded.split("\r\n"):
            line.encode("utf-8").decode("utf-8")  # would raise if split mid-char
        self.assertEqual("💸" * 40, folded.replace("\r\n ", "")[8:])

    def test_render_ics_is_deterministic_and_all_day(self):
        events = [{"kind": "bill", "uid": "ledger-bill-9-2026-07",
                   "date": "2026-07-31", "name": "Rent; special, edition",
                   "amount_cents": 123456, "paid": True}]
        body = self.app_module._render_ics(events, "2026-07-19")
        self.assertEqual(body, self.app_module._render_ics(events, "2026-07-19"))
        self.assertIn("UID:ledger-bill-9-2026-07@ledger", body)
        self.assertIn("DTSTAMP:20260719T000000Z", body)
        self.assertIn("DTSTART;VALUE=DATE:20260731", body)
        self.assertIn("DTEND;VALUE=DATE:20260801", body)   # exclusive end: next day
        self.assertIn(r"Rent\; special\, edition", body)
        self.assertIn("$1\\,234.56 ✓ paid", body)
        self.assertIn("TRANSP:TRANSPARENT", body)


if __name__ == "__main__":
    unittest.main()
