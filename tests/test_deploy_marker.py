"""deploy/deploy.sh remembers the last COMPLETED deploy in `.deployed-sha`.

Why (Nov 21, 2026): the no-op guard compared the target with HEAD. If a deploy
stopped after its checkout (pip, migrate or restart failed), HEAD already sat
on the new commit, so a plain `deploy/deploy.sh origin/main` re-run refused
with "nothing to deploy" and the half-applied deploy couldn't be finished
without passing the old sha by hand. The marker is written only after the
smoke check passes, and is the default gate baseline.

These tests run the REAL script against a scratch Pi checkout: a bare
"origin", a clone holding a dummy finance.db and venv, and stubs for sudo /
systemctl / curl / sleep / pip / the money gate / migrate on PATH, so nothing
outside the temp dir is touched. Skips where bash or git is missing.
"""
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY_SH = os.path.join(REPO, "deploy", "deploy.sh")
GITIGNORE = os.path.join(REPO, ".gitignore")


def _exe(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@unittest.skipUnless(shutil.which("bash") and shutil.which("git"), "needs bash + git")
class DeployMarkerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.log = os.path.join(t, "calls.log")
        self.curl_ok = os.path.join(t, "curl-ok")
        open(self.curl_ok, "w").close()

        # stubs on PATH: record calls, never touch the machine
        self.bin = os.path.join(t, "bin")
        os.mkdir(self.bin)
        _exe(os.path.join(self.bin, "sudo"), 'exec "$@"')
        _exe(os.path.join(self.bin, "systemctl"),
             f'echo "systemctl $*" >> "{self.log}"\n'
             'case "$1" in cat) exit 1;; esac\nexit 0')
        _exe(os.path.join(self.bin, "curl"), f'[ -f "{self.curl_ok}" ]')
        _exe(os.path.join(self.bin, "sleep"), "exit 0")

        # the remote and the Pi's checkout
        self.origin = os.path.join(t, "origin.git")
        self.pi = os.path.join(t, "pi")
        self._git(t, "init", "-q", "--bare", "-b", "main", self.origin)
        seed = os.path.join(t, "seed")
        self._git(t, "clone", "-q", self.origin, seed)
        self._git(seed, "config", "user.email", "t@example.invalid")
        self._git(seed, "config", "user.name", "t")
        os.makedirs(os.path.join(seed, "deploy"))
        shutil.copy(DEPLOY_SH, os.path.join(seed, "deploy", "deploy.sh"))
        shutil.copy(GITIGNORE, os.path.join(seed, ".gitignore"))
        with open(os.path.join(seed, "app.py"), "w") as f:
            f.write("# stub\n")
        _exe(os.path.join(seed, "migrate.py"), f'echo "migrate $*" >> "{self.log}"')
        _exe(os.path.join(seed, "deploy", "verify_live_migration.py"),
             f'echo "gate $*" >> "{self.log}"')
        with open(os.path.join(seed, "requirements.txt"), "w") as f:
            f.write("")
        self.seed = seed
        self.sha_a = self._commit("A")
        self._git(seed, "push", "-q", "origin", "main")

        self._git(t, "clone", "-q", self.origin, self.pi)
        venv = os.path.join(self.pi, "venv", "bin")
        os.makedirs(venv)
        # venv python: the real interpreter for the VACUUM INTO backup, but the
        # gate / migrate scripts are the stubs above (run as plain executables)
        _exe(os.path.join(venv, "python"),
             'case "$1" in /*.py) exec "$@";; *.py) s="./$1"; shift; exec "$s" "$@";; esac\n'
             f'exec "{sys.executable}" "$@"')
        _exe(os.path.join(venv, "pip"), f'echo "pip $*" >> "{self.log}"')
        sqlite3.connect(os.path.join(self.pi, "finance.db")).close()
        with open(os.path.join(self.pi, ".env"), "w") as f:   # the Pi always has one
            f.write("PORT=8080\n")

    def tearDown(self):
        self.tmp.cleanup()

    # ---- helpers -------------------------------------------------------------
    def _git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()

    def _commit(self, msg):
        with open(os.path.join(self.seed, "change.txt"), "w") as f:
            f.write(msg)
        self._git(self.seed, "add", "-A")
        self._git(self.seed, "commit", "-q", "-m", msg)
        return self._git(self.seed, "rev-parse", "HEAD")

    def _push_new(self, msg):
        sha = self._commit(msg)
        self._git(self.seed, "push", "-q", "origin", "main")
        return sha

    def _deploy(self, *args):
        # Backups are named to the second; with sleep stubbed, two runs can
        # land in the same second, so start each run in a fresh one.
        now = time.time()
        if now - getattr(self, "_last_run", 0) < 1.0:
            time.sleep(1.0 - (now % 1.0) + 0.01)
        self._last_run = time.time()
        if os.path.exists(self.log):
            os.remove(self.log)
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"])
        return subprocess.run(["bash", "deploy/deploy.sh", *args], cwd=self.pi,
                              env=env, capture_output=True, text=True)

    def _calls(self):
        if not os.path.exists(self.log):
            return ""
        with open(self.log) as f:
            return f.read()

    def _marker(self):
        p = os.path.join(self.pi, ".deployed-sha")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return f.read().strip()

    def _head(self):
        return self._git(self.pi, "rev-parse", "HEAD")

    # ---- tests ---------------------------------------------------------------
    def test_a_completed_deploy_records_the_marker(self):
        sha_b = self._push_new("B")
        r = self._deploy("origin/main")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertEqual(sha_b, self._head())
        self.assertEqual(sha_b, self._marker())
        # no marker yet → HEAD (A) is the gate baseline, as before
        self.assertIn(f"gate --db", self._calls())
        self.assertIn(f"--old {self.sha_a} --new origin/main", self._calls())

    def test_the_marker_is_ignored_by_git(self):
        sha_b = self._push_new("B")
        self.assertEqual(0, self._deploy("origin/main").returncode)
        self.assertEqual(sha_b, self._marker())
        self.assertEqual("", self._git(self.pi, "status", "--porcelain"),
                         "the marker must not dirty the tree (deploy refuses a dirty tree)")

    def test_re_running_a_completed_deploy_says_already_deployed(self):
        self._push_new("B")
        self.assertEqual(0, self._deploy("origin/main").returncode)
        r = self._deploy("origin/main")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertIn("already deployed", r.stdout)
        self.assertNotIn("systemctl", self._calls(), "a no-op touches no service")
        self.assertEqual([], [f for f in os.listdir(self.pi)
                              if f.startswith("finance.db.bak-")][1:],
                         "a no-op writes no second backup")

    def test_a_deploy_that_stopped_after_checkout_can_be_re_run(self):
        # B deploys fully; C's deploy dies at the smoke check, after checkout.
        self._push_new("B")
        self.assertEqual(0, self._deploy("origin/main").returncode)
        sha_b = self._marker()
        sha_c = self._push_new("C")
        os.remove(self.curl_ok)
        failed = self._deploy("origin/main")
        self.assertNotEqual(0, failed.returncode)
        self.assertEqual(sha_c, self._head(), "the checkout moved before the failure")
        self.assertEqual(sha_b, self._marker(), "a failed deploy never advances the marker")

        # Fixed; the plain re-run resumes from B instead of refusing.
        open(self.curl_ok, "w").close()
        r = self._deploy("origin/main")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertNotIn("nothing to deploy", r.stdout + r.stderr)
        self.assertIn("stopped partway", r.stdout)
        self.assertIn(f"--old {sha_b} --new origin/main", self._calls())
        self.assertIn("migrate apply --live finance.db", self._calls())
        self.assertEqual(sha_c, self._marker())

    def test_an_explicit_old_ref_still_wins(self):
        self._push_new("B")
        self.assertEqual(0, self._deploy("origin/main").returncode)
        self._push_new("C")
        r = self._deploy("origin/main", self.sha_a)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertIn(f"--old {self.sha_a} --new origin/main", self._calls())
        self.assertNotIn("stopped partway", r.stdout)

    def test_an_unreadable_marker_falls_back_to_head(self):
        sha_b = self._push_new("B")
        with open(os.path.join(self.pi, ".deployed-sha"), "w") as f:
            f.write("not-a-commit\n")
        r = self._deploy("origin/main")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertIn("WARNING", r.stdout)
        self.assertIn(f"--old {self.sha_a} --new origin/main", self._calls())
        self.assertEqual(sha_b, self._marker())

    def test_no_marker_and_head_at_target_still_refuses(self):
        # Without a marker the script can't tell a finished deploy from an
        # interrupted one, so it keeps refusing and says how to finish it.
        self._push_new("B")
        self._git(self.pi, "fetch", "-q", "origin")
        self._git(self.pi, "checkout", "-q", "origin/main")
        r = self._deploy("origin/main")
        self.assertNotEqual(0, r.returncode)
        self.assertIn("nothing to deploy", r.stderr)
        self.assertIn("deploy/deploy.sh origin/main <previous-sha>", r.stderr)
        self.assertNotIn("systemctl", self._calls())


if __name__ == "__main__":
    unittest.main()
