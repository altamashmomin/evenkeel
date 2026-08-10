"""Shared, process-cached test seeds.

Building a fixture db costs two subprocess spawns — `seed_db.py` + `migrate.py`.
Done in every test method's `setUp` across ~55 files, that was ~1,000 spawns and
most of the suite's wall time. Here each distinct (seed, months, as_of) template
is built ONCE per process and then file-copied per test. A copy is byte-identical
to a fresh build (the seeds are deterministic — fixtures are pure functions of
(seed, months, as_of), see CLAUDE.md conventions), so this is a pure speedup:
every test sees exactly the data it did before, on its own private copy it may
freely mutate.

Scope: seed_db + migrate only. The income fixture (`seed_income.py`) is left to
the handful of files that use it — they lean on seed_income's own argument
defaults, which don't map cleanly onto a shared (seed, months, as_of) key.

Usage in a test's setUp (replaces the seed_db + migrate subprocess blocks):

    import _seedbase
    ...
    self.db_path = Path(self.tmp.name) / "test.db"
    _seedbase.seed_into(self.db_path, seed=61, months=1)
"""
import atexit
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_AS_OF = "2026-07-19"   # the frozen fixture clock; tests import this or define their own

_TEMPLATES = {}   # (seed, months, as_of) -> built template path
_ROOT = None


def _root():
    global _ROOT
    if _ROOT is None:
        _ROOT = tempfile.mkdtemp(prefix="ledger-seed-templates-")
        atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)
    return _ROOT


def _run(*args):
    subprocess.run([sys.executable, *[str(a) for a in args]],
                   check=True, capture_output=True, text=True)


def _template(seed, months, as_of):
    key = (seed, months, as_of)
    path = _TEMPLATES.get(key)
    if path is not None and Path(path).exists():
        return path
    path = str(Path(_root()) / f"tmpl-{seed}-{months}-{as_of}.db")
    _run(REPO / "seed_db.py", path, "--seed", seed, "--months", months, "--as-of", as_of)
    _run(REPO / "migrate.py", "apply", path)
    _TEMPLATES[key] = path
    return path


def seed_into(dest, seed, months=1, as_of=SEED_AS_OF):
    """Populate `dest` with a migrated fixture db by copying a process-cached
    template built with these params. Byte-identical to running seed_db.py +
    migrate.py fresh; returns str(dest)."""
    shutil.copy(_template(seed, months, as_of), str(dest))
    return str(dest)
