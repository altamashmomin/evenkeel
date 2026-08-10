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


if __name__ == "__main__":
    unittest.main()
