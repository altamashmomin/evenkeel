#!/usr/bin/env bash
# Local soak harness for the Ledger MCP read tier (AGENT-DESIGN step 2).
#
# Runs the Flask app + ledger_mcp on this Mac against a *synthetic* seeded
# database, so you can connect Claude Code and live with the read tools for a
# week — de-risking the tool surface and docstrings before any live Pi deploy.
# Touches no real financial data. Re-runnable: the db + token persist under
# ~/.ledger-soak, so restarts reuse the same world.
#
#   ./soak_local.sh          # start both servers (Ctrl-C to stop)
#
# One-time, in another terminal, to point Claude Code at it:
#   claude mcp add --transport http ledger http://127.0.0.1:8765/mcp
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$REPO/venv/bin/python"
SOAK="$HOME/.ledger-soak"
DB="$SOAK/soak.db"
TOKEN_FILE="$SOAK/token"
FLASK_PORT=5055
MCP_PORT=8765
export SECRET_KEY="local-soak-fixed-key"   # bearer auth is used, not sessions

mkdir -p "$SOAK"

# 1. Synthetic data (once): spend + income with unclassified inflows to tag.
if [ ! -f "$DB" ]; then
  echo "→ seeding synthetic soak db at $DB"
  "$PY" "$REPO/seed_db.py" "$DB" --seed 72 --months 6 --as-of 2026-07-19 >/dev/null
  "$PY" "$REPO/migrate.py" apply "$DB" >/dev/null
  "$PY" "$REPO/seed_income.py" "$DB" >/dev/null
fi

# 2. A read token (once), minted through the verb, saved plaintext for reuse.
if [ ! -f "$TOKEN_FILE" ]; then
  echo "→ minting a read token"
  DATABASE_PATH="$DB" "$PY" - "$DB" >"$TOKEN_FILE" <<'PY'
import sqlite3, sys, actions
c = sqlite3.connect(sys.argv[1]); c.row_factory = sqlite3.Row
print(actions.create_api_token(
    c, "ui:avery", {"label": "claude-code-soak", "user_id": 1,
                    "scopes": "read"})["token"], end="")
PY
fi
TOKEN="$(cat "$TOKEN_FILE")"

# 3. Boot Flask, then the MCP server that reads from it.
cleanup() { kill "${FLASK_PID:-}" "${MCP_PID:-}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "→ starting Flask on 127.0.0.1:$FLASK_PORT"
DATABASE_PATH="$DB" "$PY" -c "from app import app; app.run(port=$FLASK_PORT, use_reloader=False)" \
  >"$SOAK/flask.log" 2>&1 &
FLASK_PID=$!

for _ in $(seq 1 30); do
  curl -sf "http://127.0.0.1:$FLASK_PORT/api/status" >/dev/null 2>&1 && break
  sleep 0.3
done

echo "→ starting ledger_mcp on 127.0.0.1:$MCP_PORT"
LEDGER_MCP_TOKEN="$TOKEN" \
LEDGER_API_BASE="http://127.0.0.1:$FLASK_PORT" \
LEDGER_MCP_HOST=127.0.0.1 LEDGER_MCP_PORT=$MCP_PORT \
  "$PY" "$REPO/ledger_mcp.py" >"$SOAK/mcp.log" 2>&1 &
MCP_PID=$!

sleep 1.5
cat <<EOF

  ✔ Soak is up.
    MCP endpoint : http://127.0.0.1:$MCP_PORT/mcp
    Flask API    : http://127.0.0.1:$FLASK_PORT   (logs: $SOAK/flask.log)
    Data         : synthetic ($DB)  — has unclassified inflows to try tagging-talk on
    Logs         : $SOAK/mcp.log

  Connect Claude Code (in another terminal, one time):
    claude mcp add --transport http ledger http://127.0.0.1:$MCP_PORT/mcp

  Then ask it things: "how are we doing this month?", "are we spending more on
  groceries?", "what deposits haven't been categorized?". Read-only — it can't
  write. Ctrl-C here to stop both servers.

EOF

wait
