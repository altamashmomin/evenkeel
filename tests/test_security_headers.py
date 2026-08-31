"""Security response headers + the JSON error backstop
(CODE-REVIEW-2026-08-07 #13 and #14).

#13 — every response carries a Content-Security-Policy (script-src 'self',
which would have neutralised the P0 XSS), plus nosniff and a Referrer-Policy.
#14 — /api/* answers JSON on both a 404 and a truly-unhandled 500, never Flask's
HTML page (the HTML page made app.js's api() fail to parse and blank the tab).
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

import _seedbase

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"


class SecurityHeaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-sechdr-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=63, months=1)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "sechdr-test-secret")
        spec = importlib.util.spec_from_file_location("app_sechdr_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)

    def tearDown(self):
        self.tmp.cleanup()

    def login(self, c):
        with c.session_transaction() as s:
            s["user_id"] = 1

    def test_csp_and_hardening_headers_on_every_response(self):
        c = self.app_module.app.test_client()
        r = c.get("/api/status")   # unauthenticated is fine for headers
        csp = r.headers.get("Content-Security-Policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertIn("default-src 'self'", csp)
        self.assertEqual("nosniff", r.headers.get("X-Content-Type-Options"))
        self.assertEqual("same-origin", r.headers.get("Referrer-Policy"))

    def test_api_404_is_json_not_html(self):
        c = self.app_module.app.test_client()
        r = c.get("/api/does-not-exist")
        self.assertEqual(404, r.status_code)
        self.assertEqual("application/json", r.mimetype)
        self.assertIn("error", r.get_json())

    def test_api_unhandled_500_is_json_not_html(self):
        # A truly-unhandled exception on /api/* must come back as JSON, not
        # Flask's HTML 500 page (finding #14 — HTML made app.js's api() fail to
        # parse and blank the tab). Decoupled from any verb's own validation by a
        # throwaway route that raises. PROPAGATE_EXCEPTIONS=False so the
        # registered errorhandler runs the way it does in production.
        self.app_module.app.config["PROPAGATE_EXCEPTIONS"] = False

        @self.app_module.app.route("/api/_boom_for_this_test")
        def _boom():
            raise RuntimeError("boom")

        c = self.app_module.app.test_client()
        r = c.get("/api/_boom_for_this_test")
        self.assertEqual(500, r.status_code)
        self.assertEqual("application/json", r.mimetype)
        self.assertIn("error", r.get_json())

    def test_add_item_rejects_a_non_string_name_with_a_clean_400(self):
        # H-4: a non-string name used to hit .strip() and 500; now a clean 400.
        c = self.app_module.app.test_client()
        self.login(c)
        r = c.post("/api/inventory", json={"name": 12345})
        self.assertEqual(400, r.status_code)
        self.assertIn("text", r.get_json()["error"])

    # ---- gzip compression (audit R1) ----
    def test_static_js_is_gzipped_only_when_the_client_accepts_it(self):
        c = self.app_module.app.test_client()
        plain = c.get("/render.js")                                    # no Accept-Encoding
        gz = c.get("/render.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(200, gz.status_code)
        self.assertEqual("gzip", gz.headers.get("Content-Encoding"))
        self.assertIn("Accept-Encoding", gz.headers.get("Vary", ""))
        self.assertLess(len(gz.data), len(plain.data))                # actually smaller
        self.assertNotIn("Content-Encoding", plain.headers)           # untouched w/o accept

    def test_index_shell_is_gzipped(self):
        c = self.app_module.app.test_client()
        r = c.get("/", headers={"Accept-Encoding": "gzip"})
        self.assertEqual("gzip", r.headers.get("Content-Encoding"))

    def test_api_json_is_gzipped_when_sizable(self):
        c = self.app_module.app.test_client()
        self.login(c)
        r = c.get("/api/activity?filter=all", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(200, r.status_code)
        # activity over a seeded month is well past COMPRESS_MIN_SIZE (500B)
        self.assertEqual("gzip", r.headers.get("Content-Encoding"))

    # ---- R2: immutable caching for ?v=-stamped assets ----
    def test_stamped_asset_is_cached_immutably_only_with_the_v_stamp(self):
        c = self.app_module.app.test_client()
        stamped = c.get("/render.js?v=123").headers.get("Cache-Control", "")
        self.assertIn("immutable", stamped)
        self.assertIn("max-age=31536000", stamped)
        # a bare hit (no ?v=) must NOT be cached forever — it can still revalidate
        bare = c.get("/render.js").headers.get("Cache-Control", "")
        self.assertNotIn("immutable", bare)

    def test_shell_and_api_are_never_immutable(self):
        c = self.app_module.app.test_client()
        # even with a ?v= on the shell URL, / must stay no-cache (it re-stamps)
        self.assertIn("no-cache", c.get("/?v=123").headers.get("Cache-Control", ""))
        self.assertNotIn("immutable", c.get("/?v=123").headers.get("Cache-Control", ""))
        self.login(c)
        self.assertNotIn("immutable",
                         c.get("/api/activity?v=1").headers.get("Cache-Control", ""))

    # ---- R3: the shell defers both scripts ----
    def test_shell_defers_both_scripts(self):
        c = self.app_module.app.test_client()
        html = c.get("/").get_data(as_text=True)
        self.assertEqual(2, html.count("defer></script>"))   # render.js + app.js
        self.assertIn('src="render.js?v=', html)             # ?v= stamp preserved
        self.assertIn('src="app.js?v=', html)


if __name__ == "__main__":
    unittest.main()
