# Deploy: the MCP write tier (AGENT-DESIGN step 4)

Turns the agent from read-only into read-plus-write over Tailscale: two direct
writes (`ledger_classify_inflow`, `ledger_set_rule_enabled`) and the two-phase
tier (`ledger_propose_income_rule` / `ledger_apply_rules` → `ledger_confirm_action`).
The tools already ship in `ledger_mcp.py`; going live is (1) a gated `#007`
migration, then (2) swapping the MCP token's scope to `read,write`.

Prereq: the read tier is already deployed and running (see `mcp-read-tier.md`).

## 1. Deploy the code + gated migration `#007`

On the Pi, from the repo root, after advancing `main` to the increment:

```bash
deploy/deploy.sh origin/main
```

`deploy.sh` backs up `finance.db`, dry-run-gates the migration on the backup
copy, and only then applies `#007 --live` (adds the empty `pending_actions`
table; `schema_version` 6→7) and restarts `pifinance`. The gate must report
the enumerated `#007` diff and nothing else (see `notes/007-gate-expectation.seed.json`).
No money moves — `pending_actions` is a staging table, not a financial one.

Restart the MCP sibling too, so it serves the new write tools:

```bash
sudo systemctl restart ledger-mcp
```

## 2. Mint a `read,write` token (in the app, as yourself)

The write tools need write scope; a `read` token is refused (403) on every
write. Mint one the same way as the read token, adding `scopes`:

```bash
curl -sX POST http://raspberrypi:8080/api/tokens \
  -H 'Content-Type: application/json' \
  --cookie "$YOUR_SESSION" \
  -d '{"label":"claude-code-write","scopes":"read,write"}'
```

The plaintext is returned **once** — copy it. Token minting is session-only
(a bearer token can't mint tokens), and tokens are per-person, so this write
token is attributed to you in the audit log (`mcp:claude-code-write`).

## 3. Point the MCP service at the write token AND opt into writes

Two things are required — a `read,write` token is deliberately **not** enough on
its own, so a reachable port or a leaked token can't change data. In
`/home/altamash/pifinance/.env` (never commit it):

```
LEDGER_MCP_TOKEN=<the new read,write token>
LEDGER_MCP_ENABLE_WRITES=1
```

`LEDGER_MCP_ENABLE_WRITES` is the server-side opt-in: without it the write tools
are refused at the API client (they return "the write tier is disabled …"), even
with a `read,write` token — the safe default. Then restart:

```bash
sudo systemctl restart ledger-mcp
```

At startup the service logs its mode ("write tier ENABLED" / "disabled — read-only")
and, if bound beyond loopback, a reminder that the tailnet ACL is the only inbound
boundary. Repoint your Claude Code / Desktop client only if the endpoint changed;
the token lives on the Pi, not in the client.

> Security posture: the write tier now has **two** independent gates — the token's
> `write` scope (enforced by Flask) and `LEDGER_MCP_ENABLE_WRITES` (this server) —
> on top of the tailnet ACL that restricts *which devices* reach the port at all
> (`deploy/mcp-tailnet-acl.md`). Verify that ACL is live: from a non-owner device,
> `curl --max-time 6 http://<pi-tailnet-ip>:8765/mcp` should fail to connect.

## 4. Verify end to end (over the tailnet)

- A write tool now succeeds: ask the agent to tag an unclassified inflow, or
  call `ledger_set_rule_enabled` — it returns the updated row, and the change
  shows in the app.
- The two-phase discipline holds: `ledger_propose_income_rule` returns a
  preview + token and writes nothing; `ledger_confirm_action` with that token
  executes exactly the previewed change; a reused token is refused.
- The scope gate holds: a `read` token (the read-tier token) still gets 403 on
  any write tool — reads keep working, writes don't.

Rollback is the `finance.db.bak-<timestamp>` `deploy.sh` wrote in step 1.
