# Locking down the MCP port with a Tailscale ACL (P0-2)

**Problem (from CODE-REVIEW-2026-08-07, finding 2).** `ledger_mcp.py` runs
`mcp.run(transport="streamable-http")` with **no inbound authentication**. On the
Pi it binds the Tailscale IP so Alta's Claude Code client and (incidentally)
every other tailnet device can reach `100.108.237.13:8765`. Verified: an
unauthenticated request returns `406`, not `401` — the protocol layer answers.
Because the Pi's `LEDGER_MCP_TOKEN` is minted `read,write`, any tailnet peer —
including Charlee's shared device — could drive all 24 tools, both halves of the
two-phase write tier included. **The tailnet is the entire auth boundary.**

**Fix chosen:** a Tailscale ACL so only Alta's own devices can reach `:8765`.
Zero code, no change to `ledger_mcp.py`, and it does **not** touch Alta's working
MCP client (the client keeps connecting exactly as today). Charlee keeps the web
app on `:8080`.

This is a Tailscale **admin-console** action, done at
<https://login.tailscale.com/admin/acls>. It is not in this repo.

---

## ⚠ Before you touch anything — the one real risk

Tailscale ACLs are **default-allow only until you define an `acls` block; the
moment one exists, everything not explicitly listed is DENIED.** A home tailnet
with no custom ACL is wide open, so adding the block below *also* removes any
flow you don't list. The ruleset here is written to keep every flow you actually
use, but:

1. **Use the admin console's "Preview" / access-tester before Save.** It shows,
   per device, exactly what each can reach. Confirm: Alta's Mac → Pi:8765 =
   allowed; Charlee's device → Pi:8765 = **denied**; Charlee's device → Pi:8080 =
   allowed; Alta's Mac → Pi:22 (ssh) = allowed.
2. The ACL editor keeps **version history** — if anything goes wrong, revert to
   the previous version in the console. That is your rollback.
3. This governs the **tailnet only**. It does nothing for the LAN exposure —
   that's P0-3 (bind `BIND_HOST` to the tailnet IP), a separate fix.

---

## The ruleset

Confirm two identifiers in the admin console first:

- **Your user login** — the Users page shows it (CLAUDE.md records the tailnet as
  `altamashmomin@`; use the exact string shown).
- **The Pi's address** — its tailnet IP is `100.108.237.13`. (You can instead tag
  the Pi and target `tag:pi`, which survives an IP change, but that needs
  `tagOwners` set up; the IP is fine to start.)

```jsonc
{
  "acls": [
    // 1) Alta's own devices reach everything on the tailnet — ssh, the MCP
    //    admin port :8765, the web app, all of it. This keeps the MCP client,
    //    ssh, and deploys working with no change.
    { "action": "accept", "src": ["altamashmomin@"], "dst": ["*:*"] },

    // 2) Everyone ELSE (i.e. Charlee's shared device) reaches ONLY the web app
    //    on the Pi. Not :8765, not ssh. This is what closes P0-2.
    { "action": "accept", "src": ["*"], "dst": ["100.108.237.13:8080"] }
  ]
}
```

Why this shape: ACL rules are additive-accept with no "deny" verb, so you cannot
"block one port" — you restrict by *not granting* it. Rule 1 grants Alta's
devices full reach (so nothing of yours breaks). Rule 2 grants everyone the web
app and nothing more, so `:8765` is reachable only via rule 1 — Alta only.

If you rely on other tailnet flows not covered above (another device, a printer,
etc.), add them as their own `accept` lines **before** saving — remember,
anything unlisted is now denied.

---

## Verify after Save

From a device that is **not** one of Alta's (or ask Charlee to, from her phone on
the tailnet) the MCP port should now be unreachable, while the web app still
loads:

```bash
# should now FAIL / time out (was 406 before):
curl --max-time 6 http://100.108.237.13:8765/mcp ; echo "exit=$?"
# should still succeed (web app):
curl -s -o /dev/null -w '%{http_code}\n' --max-time 6 http://100.108.237.13:8080/api/status
```

From Alta's Mac, the MCP client should reconnect and list all tools exactly as
before — no client change needed:

```bash
curl --max-time 6 http://100.108.237.13:8765/mcp ; echo "exit=$?"   # reachable
```

---

## Note

This is the immediate, no-breakage mitigation. If you later want
possession-of-tailnet to stop being possession-of-write-scope even for Alta's own
devices, the follow-up is real inbound auth on the FastMCP server (a
`token_verifier`) or fronting it with `tailscale serve` (TLS + identity) — both
require reconfiguring the MCP client, which is why the ACL is the right first
move. See CODE-REVIEW-2026-08-07 finding 2 for the options.
