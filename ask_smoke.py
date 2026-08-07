"""Live smoke test for the Ask endpoint (increment 2).

Makes ONE real, billed Anthropic call through the actual POST /api/ask route —
session auth, the env key, the real client, the tool-use loop — against a
FRESH SYNTHETIC seed, so no real data is touched. Proves the whole path works
end to end before we deploy to the Pi.

    export ANTHROPIC_API_KEY=sk-ant-...        # your rotated key
    ./venv/bin/python ask_smoke.py "is the rent paid this month?"

Omit the question for a default. Uses ASK_MODEL if set (default Haiku 4.5).
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY first: export it in this shell.")
    question = sys.argv[1] if len(sys.argv) > 1 else "How are we doing this month?"

    tmp = tempfile.mkdtemp(prefix="ask-smoke-")
    db = Path(tmp) / "smoke.db"
    for cmd in (
        [sys.executable, str(REPO / "seed_db.py"), str(db),
         "--seed", "72", "--months", "6", "--as-of", "2026-07-19"],
        [sys.executable, str(REPO / "migrate.py"), "apply", str(db)],
        [sys.executable, str(REPO / "seed_income.py"), str(db)],
    ):
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    os.environ["DATABASE_PATH"] = str(db)
    os.environ.setdefault("SECRET_KEY", "ask-smoke")
    spec = importlib.util.spec_from_file_location("app_ask_smoke", REPO / "app.py")
    app_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_module)
    app_module.app.config["TESTING"] = True

    client = app_module.app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1

    print(f"model: {os.environ.get('ASK_MODEL', 'claude-haiku-4-5')}  "
          f"(synthetic data)\n\nQ: {question}\n")
    resp = client.post("/api/ask", json={"message": question})
    if resp.status_code != 200:
        sys.exit(f"HTTP {resp.status_code}: {resp.get_json()}")
    out = resp.get_json()
    print(f"A: {out['answer']}\n")
    print(f"[tools: {', '.join(out['tools_used']) or 'none'}  ·  "
          f"rounds: {out['rounds']}  ·  stopped: {out['stopped']}]")


if __name__ == "__main__":
    main()
