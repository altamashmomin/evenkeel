"""The /trace route serves the architecture Trace Web, and /api/ontology serves
the manifest the map draws from (data-driven: the page fetches its facts at
load, so they can never drift from the source). Proves: /trace answers 200
no-cache, version-stamps its same-origin script, and — the load-bearing guard —
stays CSP-clean (no inline <script>, no external hosts), so it renders under the
strict `script-src 'self'` CSP; both the page AND the manifest endpoint are
gated (the code's shape is reconnaissance surface, CODE-REVIEW-2026-08-08);
/api/ontology returns exactly ontology.manifest()."""
import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

import _seedbase

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"


class TraceRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-trace-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=61, months=1)
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

    def test_stamped_script_is_served_and_fetches_the_manifest(self):
        html = self.c.get("/trace").get_data(as_text=True)
        stamped = re.search(r'(trace-web\.js\?v=\d+)', html).group(1)
        r = self.c.get("/" + stamped)
        self.assertEqual(200, r.status_code)
        body = r.get_data(as_text=True)
        # data-driven: the script fetches the manifest and builds from it; the
        # facts themselves live server-side, not in the file
        self.assertIn("fetch('/api/ontology')", body)
        self.assertIn("buildModel", body)
        self.assertIn("buildEdges", body)

    def test_ontology_endpoint_serves_the_manifest(self):
        """GET /api/ontology returns exactly ontology.manifest() — the map's
        single source. Gated: an anonymous GET is a 401 (JSON, no model leak),
        matching every other /api/ read."""
        import ontology
        r = self.c.get("/api/ontology")
        self.assertEqual(200, r.status_code)
        self.assertIn("no-cache", r.headers.get("Cache-Control", ""))
        payload = r.get_json()
        self.assertEqual(ontology.manifest(), payload)
        # the fields the map draws from are all present
        for key in ("schema_version", "objects", "actions", "functions",
                    "callers", "doors"):
            self.assertIn(key, payload)

    def test_ontology_endpoint_is_gated(self):
        anon = self.app_module.app.test_client()
        r = anon.get("/api/ontology")
        self.assertEqual(401, r.status_code)
        body = r.get_data(as_text=True)
        self.assertNotIn("confirm_action", body)   # no model leak
        self.assertIn("authentication required", body)

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
