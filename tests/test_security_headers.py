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
        # Trigger the type-confusion 500 (finding 25 — a non-string name raises
        # AttributeError past the route's try/except). It must now come back as
        # JSON, not Flask's HTML 500 page. PROPAGATE_EXCEPTIONS=False so the
        # registered errorhandler runs the way it does in production.
        self.app_module.app.config["PROPAGATE_EXCEPTIONS"] = False
        c = self.app_module.app.test_client()
        self.login(c)
        r = c.post("/api/inventory", json={"name": 12345})
        self.assertEqual(500, r.status_code)
        self.assertEqual("application/json", r.mimetype)
        self.assertIn("error", r.get_json())

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


if __name__ == "__main__":
    unittest.main()
