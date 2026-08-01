# Ledger MCP read tier — deploy & connect

The `ledger_mcp` server (AGENT-DESIGN build-order step 2) is a sibling process
to the Flask app on the Pi. It wraps the read endpoints as MCP tools and
serves them over streamable HTTP on the tailnet. It holds no state and does no
math — it is an HTTP client of the Flask API under a **read-scope bearer
token**, so it cannot write even if a tool tried (the API returns 403 on any
mutating method for a `read` token).

**Tailnet-only. No Tailscale Funnel, no public exposure** (AGENT-DESIGN
decision #1).

## 1. Mint a read token (in the app, as yourself)

Log into Ledger in the browser and mint a `read` token:

```bash
curl -sX POST http://raspberrypi:8080/api/tokens \
  -H 'Content-Type: application/json' \
  --cookie "$YOUR_SESSION" \
  -d '{"label":"claude-code"}'
```

The plaintext token is returned **once** — copy it now. (Token management
routes are session-only; a bearer token cannot mint tokens.) Omitting
`scopes` defaults to `read` — correct for this read-only tier. The write
tier accepts `"scopes":"read,write"` for an agent that also proposes/confirms
writes (see the write-tier deploy notes); keep this MCP token `read`.

## 2. Configure and start the service (on the Pi)

Add to `/home/altamash/pifinance/.env` (never commit it):

```
LEDGER_MCP_TOKEN=<the token from step 1>
LEDGER_API_BASE=http://127.0.0.1:8080
LEDGER_MCP_HOST=100.108.237.13     # the Pi's Tailscale IP — tailnet-only
LEDGER_MCP_PORT=8765
```

Install deps into the app's venv, then the unit. The tracked unit carries the
`pi`/`/home/pi` placeholder like the others — rewrite on copy, never edit the
tracked file (a future `git pull` would conflict):

```bash
/home/altamash/pifinance/venv/bin/pip install -r requirements.txt
sed 's#/home/pi#/home/altamash#g; s/User=pi/User=altamash/' \
  deploy/ledger-mcp.service | sudo tee /etc/systemd/system/ledger-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now ledger-mcp.service
systemctl status ledger-mcp.service
```

The MCP endpoint is then `http://100.108.237.13:8765/mcp` (or
`http://raspberrypi:8765/mcp` over MagicDNS).

## 3. Connect Claude Code (over Tailscale, from a tailnet device)

```bash
claude mcp add --transport http ledger http://raspberrypi:8765/mcp
```

Then ask, e.g. "how are we doing this month?" — Claude starts with
`ledger_household_snapshot`. If a call returns a 401 error, the token was
revoked or expired; mint a new one (step 1) and update `.env`.

## Tools (13, all read-only)

`ledger_household_snapshot` (start here), `ledger_balance`,
`ledger_spending_composition`, `ledger_category_trend`,
`ledger_income_summary`, `ledger_income_trend`, `ledger_savings_rate_trend`,
`ledger_member_breakdown`, `ledger_bill_variance`, `ledger_list_income_rules`,
`ledger_unclassified_inflows`, `ledger_search_transactions`,
`ledger_list_goals_and_bills`.

Every money field is `{cents, display}` (or dollars where the wrapped v1
endpoint predates the dual shape); the docstrings tell the agent to quote the
display string and never do its own arithmetic.

## Why no balance gate for this increment

The MCP server touches no schema and no derivation — it is a pure HTTP client
of endpoints that already exist and are already gated. Like the frontend
increments, it ships without a gate run; its safety net is
`tests/test_ledger_mcp.py`, whose load-bearing assertions prove each tool's
JSON is byte-equal to the Flask endpoint it wraps (it can only reshape, never
recompute).
