"""The Pi guardian's JSON verdict (deploy/ops-health-check.sh → ops-status.json).

The app's ops_health check-in reads this file, so it has to be valid JSON even
when a problem line holds quotes or backslashes, and it has to appear when
Python is missing or broken (the guardian is plain shell on purpose). These
run the real script off-Pi: no systemd here, so it reports red, which is
exactly the case worth checking.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "ops-health-check.sh"


@unittest.skipUnless(shutil.which("bash"), "bash not available")
class OpsHealthJsonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-ops-json-")
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, env_extra, path=None):
        env = {"HOME": str(self.dir), "PATH": path or os.environ.get("PATH", ""),
               "OPS_STATUS_FILE": str(self.dir / "ops-status.txt")}
        env.update(env_extra)
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True,
                              text=True, timeout=60)

    def verdict(self):
        return json.loads((self.dir / "ops-status.json").read_text(encoding="utf-8"))

    def test_writes_valid_json_beside_the_text_heartbeat(self):
        r = self.run_script({"APP_DIR": str(self.dir), "SECRET_KEY": "s"})
        self.assertIn(r.returncode, (0, 1, 2))
        self.assertTrue((self.dir / "ops-status.txt").exists())
        v = self.verdict()
        self.assertEqual({"green": 0, "amber": 1, "red": 2}[v["level"]], r.returncode)
        self.assertEqual(v["worst"], r.returncode)
        self.assertIsInstance(v["checked_epoch"], int)
        self.assertRegex(v["checked_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        for p in v["problems"]:
            self.assertIn(p["severity"], ("warn", "crit"))
            self.assertTrue(p["text"])
        # No leftover temp file from the atomic write.
        self.assertFalse((self.dir / "ops-status.json.tmp").exists())

    def test_quotes_backslashes_and_tabs_survive_escaping(self):
        # A missing APP_DIR makes the disk check echo the path into a warning.
        odd = str(self.dir / 'no"such\\dir\tx')
        self.run_script({"APP_DIR": odd})
        texts = [p["text"] for p in self.verdict()["problems"]]
        self.assertTrue(any(odd in t for t in texts), texts)

    def test_json_still_written_without_python(self):
        # A PATH with the shell tools the guardian needs but no python3.
        bindir = self.dir / "bin"
        bindir.mkdir()
        for tool in ("bash", "date", "df", "awk", "sed", "tr", "mv", "rm", "sort",
                     "tail", "head", "basename", "dirname", "stat", "printf", "cat"):
            found = shutil.which(tool)
            if found:
                (bindir / tool).symlink_to(found)
        self.assertIsNone(shutil.which("python3", path=str(bindir)))
        r = self.run_script({"APP_DIR": str(self.dir)}, path=str(bindir))
        self.assertIn(r.returncode, (1, 2), r.stderr)
        v = self.verdict()
        self.assertIn(v["level"], ("amber", "red"))

    def test_status_json_path_override(self):
        target = self.dir / "elsewhere.json"
        self.run_script({"APP_DIR": str(self.dir), "OPS_STATUS_JSON": str(target)})
        self.assertTrue(target.exists())
        self.assertFalse((self.dir / "ops-status.json").exists())
        json.loads(target.read_text(encoding="utf-8"))

    def test_never_posts_an_issue_unless_configured(self):
        r = self.run_script({"APP_DIR": str(self.dir)})
        self.assertNotIn("GitHub issue", r.stdout)


class OpsStatusPathContractTests(unittest.TestCase):
    """The tracked unit and the app must agree on where the verdict lives, or a
    healthy guardian shows up in the app as "not reporting" forever."""

    def test_unit_pins_the_folder_the_app_reads(self):
        import re
        unit = (REPO / "deploy" / "pifinance-ops.service").read_text(encoding="utf-8")
        env = re.findall(r"^Environment=OPS_STATUS_FILE=(\S+)$", unit, re.M)
        self.assertEqual(len(env), 1, "unit should pin OPS_STATUS_FILE once")
        unit_path = env[0]
        self.assertTrue(unit_path.startswith("/home/pi/"), unit_path)
        app_src = (REPO / "app.py").read_text(encoding="utf-8")
        m = re.search(r'"OPS_STATUS_FILE", os\.path\.expanduser\("~/([^"]+)"\)', app_src)
        self.assertIsNotNone(m, "app.py's OPS_STATUS_FILE default moved; update this test")
        self.assertEqual(unit_path, "/home/pi/" + m.group(1))
        # The folder has to exist before the script writes into it.
        folder = unit_path.rsplit("/", 1)[0]
        self.assertIn(f"ExecStartPre=/bin/mkdir -p {folder}\n", unit)


if __name__ == "__main__":
    unittest.main()
