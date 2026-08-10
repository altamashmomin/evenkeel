"""The SPA shell version-stamps its assets (style.css / render.js / app.js) so
a frontend deploy is picked up without a per-device hard refresh. Proves: each
asset URL carries a ?v=<mtime> token, the HTML is sent no-cache, the stamp
tracks the file's mtime (so it changes exactly when the file changes), and the
asset itself is still served with the query string present."""
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


class IndexCacheBustingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-index-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=61, months=1)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "index-test-secret")
        spec = importlib.util.spec_from_file_location("app_index_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def test_assets_are_version_stamped_and_html_is_no_cache(self):
        c = self.app_module.app.test_client()
        r = c.get("/")
        self.assertEqual(200, r.status_code)
        self.assertIn("no-cache", r.headers.get("Cache-Control", ""))
        html = r.get_data(as_text=True)
        for name in ("style.css", "render.js", "app.js"):
            self.assertRegex(html, rf'{re.escape(name)}\?v=\d+',
                             f"{name} is not version-stamped")
        # no bare (unstamped) reference leaks through
        self.assertNotIn('src="app.js"', html)
        self.assertNotIn('src="render.js"', html)
        self.assertNotIn('href="style.css"', html)

    def test_version_tracks_file_mtime(self):
        c = self.app_module.app.test_client()
        first = re.search(r'app\.js\?v=(\d+)', c.get("/").get_data(as_text=True)).group(1)
        # bump app.js's mtime a second into the future — the stamp must follow
        path = os.path.join(self.app_module.app.static_folder, "app.js")
        st = os.stat(path)
        os.utime(path, (st.st_atime, st.st_mtime + 5))
        second = re.search(r'app\.js\?v=(\d+)', c.get("/").get_data(as_text=True)).group(1)
        self.assertNotEqual(first, second, "stamp should change when the file changes")
        self.assertEqual(str(int(st.st_mtime + 5)), second)

    def test_stamped_asset_is_still_served(self):
        c = self.app_module.app.test_client()
        html = c.get("/").get_data(as_text=True)
        stamped = re.search(r'(app\.js\?v=\d+)', html).group(1)
        r = c.get("/" + stamped)  # the query string must not break static serving
        self.assertEqual(200, r.status_code)
        self.assertIn("renderInventory", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
