"""Folds the node-based pure-render-function tests (tests/test_render.js)
into the Python suite, so `python -m unittest` covers the frontend's pure
presentation layer too — the one corner that used to be verified only by
hand in a browser. Skips cleanly where node isn't installed (the tests
run in dev/CI, not on the Pi)."""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class FrontendRenderTests(unittest.TestCase):
    def test_pure_render_functions(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed — frontend render tests skipped")
        result = subprocess.run(
            [node, str(REPO / "tests" / "test_render.js")],
            capture_output=True, text=True)
        if result.returncode != 0:
            self.fail("node render tests failed:\n"
                      + result.stdout + "\n" + result.stderr)
        sys.stdout.write(result.stdout)


if __name__ == "__main__":
    unittest.main()
