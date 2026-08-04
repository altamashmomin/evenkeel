# Security & dependency audit — 2026-08-04

A read-only defensive review of the Ledger codebase, performed the way the
`ledger-security` + `ledger-maintenance` agents would. No code was changed.

- **Scope:** `app.py`, `actions.py`, `ledger_mcp.py`, `agent_write_tools.py`,
  `ask_loop.py`, `simplefin_sync.py`, `schema_runtime.py`; `requirements.txt`
  and the installed venv.
- **Method:** full read of the security-critical surface; auth/scope tracing;
  SQL-injection sweep; secret-handling review; `pip-audit` (installed + against
  `requirements.txt`); full test suite.
- **Baseline:** `rework` @ `1a048c3`; suite green (exit 0, ~350 Python tests +
  48 render checks); schema version 9.

## Verdict

**Strong. No exploitable vulnerabilities found.** Parameterized SQL throughout
(dynamic SQL only ever interpolates code-controlled column names, never user
data), a single audited write path, correct bearer-token handling, clean secret
hygiene. Everything below is **hardening / operations**, not a hole — ranked by
real-world impact for a tailnet-only, two-person app.

## Findings

### 1 — SECRET_KEY with 2 gunicorn workers · MED · verify on the Pi
`app.py:32` generates a random session key per process when `SECRET_KEY` is
unset, and only *logs a warning*. `deploy/pifinance.service` runs `--workers 2`
and gunicorn does not preload the app, so **each worker would generate a
different key** → a session cookie signed by one worker is rejected by the other
→ logins fail intermittently (~50%) and never survive a restart. The symptom is
silent on a headless Pi (a log line, no error surface).

- **Not a forgery risk** — the key is still random, just inconsistent; this is
  availability/UX, not session forgery.
- **Verify:** `grep -c '^SECRET_KEY=' /home/altamash/pifinance/.env` → expect
  `1`. If set (most likely), there is no issue.
- **Fix / detect:** ensure `SECRET_KEY` is set in the Pi `.env`; add a check to
  the Ops guardian (§6 of `deploy/ops-health-check.sh`) so an unset key is
  flagged rather than discovered via flaky logins.

### 2 — No rate limit on `/api/ask` · LOW-MED
`app.py:1032` is the only endpoint that spends real money per call (up to 6
Haiku rounds). It is `session_required` (the two members only), but a stolen
session cookie — or simply heavy use — has no throttle and burns Anthropic
credits.

- **Fix:** a simple per-session daily cap / basic throttle on this one endpoint.

### 3 — Session cookie not `Secure`; served over HTTP · LOW
`app.py:40` omits `SESSION_COOKIE_SECURE`; the app is served `http://` on the
tailnet. Acceptable **today** — Tailscale (WireGuard) encrypts transport and
there is no Funnel/public exposure.

- **Invariant to record:** the app must never be exposed beyond the tailnet as-is.
- **Fix (only if exposure ever changes):** set `SESSION_COOKIE_SECURE=True` and
  terminate TLS.

### 4 — Login user-enumeration timing side-channel · LOW
`app.py:256` returns before running `check_password_hash` when the username is
unknown, so a valid username's response is measurably slower. An attacker could
enumerate valid usernames by timing. Very low impact (two users, usernames are
not secret, no public exposure).

- **Fix:** compare against a dummy hash on the missing-row path so both branches
  do equal work.

### 5 — Unbounded `pending_actions`; write-on-every-read for tokens · INFO
Confirmed/expired `pending_actions` rows are never pruned, and
`actions.py:1289` (`find_active_api_token`) writes `last_used_at` and commits on
every authenticated bearer request. Both are fine at household scale.

- **Fix (optional):** a periodic prune of terminal `pending_actions`; accept the
  token write as intentional observability.

### 6 — `/api/ask` history is client-controlled · INFO · accepted risk
The model's "prior turns" come straight from the client (`app.py:1046` →
`ask_loop`), so prompt-injecting the assistant is trivial. This is **safe by
design** only because the tool surface is tightly bounded (read + tag-own-inflow
+ pantry; no money movement, settle, or delete): the worst outcome is a
reversible, logged mis-tag. The security is ACL-by-omission, not trust in the
model — worth stating as an explicit boundary rather than an implicit one.

## Dependencies

`pip-audit -r requirements.txt`: **clean.** The app's real runtime dependencies
have **no known CVEs**:

| Package | Installed | Note |
|---|---|---|
| Flask | 3.1.3 | clean |
| Werkzeug | 3.1.8 | clean |
| Jinja2 | 3.1.6 | clean |
| requests | 2.34.2 | clean |
| gunicorn | 26.0.0 | clean |
| mcp | 2.0.0 | clean |
| httpx | 0.28.1 | clean |
| anthropic | 0.120.2 | clean |

`pip-audit` on the *installed venv* reported 15 advisories, **all in `pip` 22.3
and `setuptools` 65.5.0** — build tooling in the local venv, not app
dependencies and not reachable by the running Flask app. Hygiene only:
`pip install -U pip setuptools` on the Pi venv.

- **Reproducibility note:** `requirements.txt` uses `>=` floors, so the Pi
  installs whatever is newest at deploy time. Consider confirming the Pi's
  installed versions or adding a lockfile so a deploy can't silently pull a
  newer, unreviewed release.

## What's solid (confirmed)

- **SQL injection:** none. Parameterized throughout; dynamic SQL only over
  code-controlled identifiers (the `test_architecture` known-safe pattern).
- **Tokens:** SHA-256 of a 256-bit secret (correct — not a password),
  hash-only storage, plaintext returned once, scope enforced by HTTP method,
  and `session_required` for both token minting and `/api/ask` (a read token can
  neither mint tokens nor trigger paid calls).
- **Two-phase confirm:** `confirm_action` claims the pending row
  (`pending`→`confirmed`) in its own committed transaction *before* dispatch, so
  a replay/double-confirm cannot execute twice.
- **Secrets:** the SimpleFIN access URL is written `chmod 600` and never
  printed; `claim` validates `https://` before POSTing (no SSRF); nothing logs a
  token or key.

## Recommended next steps

1. Verify `SECRET_KEY` on the Pi (finding 1) — highest silent-failure risk.
2. Add a `SECRET_KEY`-present check to the Ops guardian (§6) to detect it going
   forward.
3. Consider a rate limit on `/api/ask` (finding 2).
4. `pip install -U pip setuptools` on the Pi venv (hygiene).

None are urgent; the app is in good shape.
