"""The /trace route serves the architecture Trace Web — a static interactive map
of the ontology, wired into the app like the SPA shell. Proves: it answers 200
no-cache, version-stamps its same-origin script, and — the load-bearing guard —
stays CSP-clean (no inline <script>, no external hosts), so it renders under the
strict `script-src 'self'` CSP the app applies to every response. The sibling
trace-web.js is served and carries the reconciled edge logic."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"


class TraceRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-trace-")
        self.db_path = Path(self.tmp.name) / "route.db"
        subprocess.run(
            [sys.executable, str(REPO / "seed_db.py"), str(self.db_path),
             "--seed", "61", "--months", "1", "--as-of", SEED_AS_OF],
            check=True, capture_output=True, text=True)
        subprocess.run(
            [sys.executable, str(REPO / "migrate.py"), "apply", str(self.db_path)],
            check=True, capture_output=True, text=True)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "trace-test-secret")
        spec = importlib.util.spec_from_file_location("app_trace_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True
        self.c = self.app_module.app.test_client()
        with self.c.session_transaction() as s:   # the map is session-gated
            s["user_id"] = 1

    def tearDown(self):
        self.tmp.cleanup()

    def test_serves_no_cache_html(self):
        r = self.c.get("/trace")
        self.assertEqual(200, r.status_code)
        self.assertIn("text/html", r.headers.get("Content-Type", ""))
        self.assertIn("no-cache", r.headers.get("Cache-Control", ""))
        self.assertIn("Every path, end to end", r.get_data(as_text=True))

    def test_script_is_version_stamped(self):
        html = self.c.get("/trace").get_data(as_text=True)
        self.assertRegex(html, r'trace-web\.js\?v=\d+',
                         "trace-web.js is not version-stamped")
        self.assertNotIn('src="trace-web.js"', html)  # no bare (unstamped) leak

    def test_schema_version_is_injected(self):
        # the one fact the static map can't self-source is filled by the route;
        # the placeholder must not leak, and the live version must land.
        from schema_runtime import REQUIRED_SCHEMA_VERSION
        html = self.c.get("/trace").get_data(as_text=True)
        self.assertNotIn("__SCHEMA_VERSION__", html)
        self.assertIn(f'data-schema="{REQUIRED_SCHEMA_VERSION}"', html)

    def test_stamped_script_is_served_and_carries_edge_logic(self):
        html = self.c.get("/trace").get_data(as_text=True)
        stamped = re.search(r'(trace-web\.js\?v=\d+)', html).group(1)
        r = self.c.get("/" + stamped)
        self.assertEqual(200, r.status_code)
        body = r.get_data(as_text=True)
        # the reconciled model lives here, not in the shell
        self.assertIn("buildEdges", body)
        self.assertIn("confirm_action", body)

    def test_unauthenticated_is_redirected_and_leaks_no_model(self):
        """The map AND its script render the internal data model (verbs, tables,
        derivations, the two-phase targets), so both are session-gated
        (CODE-REVIEW-2026-08-08). An unauthenticated browser is redirected to the
        SPA login and never receives the model — gating the HTML alone would leave
        the static-served script directly fetchable."""
        anon = self.app_module.app.test_client()
        for path in ("/trace", "/trace-web.js"):
            r = anon.get(path)
            self.assertEqual(302, r.status_code, f"{path} must redirect when logged out")
            self.assertTrue(r.headers.get("Location", "").endswith("/"),
                            f"{path} should redirect to the SPA login")
            body = r.get_data(as_text=True)
            self.assertNotIn("confirm_action", body)   # no verb/model leak
            self.assertNotIn("buildEdges", body)

    def test_is_csp_clean(self):
        """The one guard that matters: served under the app's strict CSP
        (script-src 'self', default-src 'self'), the page must carry no inline
        <script> and no external host, or it silently breaks in the browser.
        Tighten this and it bites."""
        html = self.c.get("/trace").get_data(as_text=True)
        # exactly one <script>, and it is the external same-origin one
        scripts = re.findall(r"<script\b[^>]*>", html)
        self.assertEqual(1, len(scripts), f"expected one <script>, got {scripts}")
        self.assertIn("src=", scripts[0], "the script must be external, not inline")
        for host in ("http://", "https://", "//fonts.", "googleapis", "gstatic"):
            self.assertNotIn(host, html, f"external reference {host!r} violates the CSP")


if __name__ == "__main__":
    unittest.main()
