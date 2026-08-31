# Design: Change notifications & the activity digest

You should be *told* when the assistants change something — not have to open the
app and notice. Today the visibility is entirely passive: F-1's **Pending
approvals** card only appears when you happen to open Home, and the many
*single-phase* writes (tagging a deposit, adding a bill, setting a budget,
toggling a rule) leave no trace beyond the `audit_log`. This closes that gap:
a prompt when a change needs your approval, and a daily digest of everything the
assistants did.

Status: **proposed (not built).** This governs the build if you say go.

## The core claim

Nothing new needs to be *recorded* for this — every write already lands in
`audit_log` (actor, action, detail, timestamp), which is the ground truth of
"what changed and who did it." Notifications are a **read over that log plus an
outbound message.** So this feature:

- **stores nothing derived** — the digest is computed on read, like every other
  Ledger surface;
- **touches no money path** — it reads `audit_log`, never `transactions`, so the
  balance gate stays zero-diff by construction and the derivation tripwire can't
  be contaminated (same property as `calendar_events`);
- **adds no write verb** — the money write path is untouched; the notification
  is an ops-layer read + an outbound HTTP call, exactly like the pantry pulse.

And it **reuses the delivery channel you already run:** `deploy/pantry_pulse.py`
and the ops-guardian post **GitHub issues** to `OPS_ALERT_GH_REPO` (a private
alerts repo) via `OPS_ALERT_GH_TOKEN`, and GitHub does the phone push / email.
No new secrets, no new service, one consistent alerting surface.

## Privacy rule (load-bearing)

Notifications leave the tailnet — they go to GitHub, and past it to your phone.
So **notification bodies are terse: kinds and counts, never amounts,
descriptions, balances, or account details.** The specifics stay behind the
tailnet + login, in the app. A notification says *what kind of thing happened
and to go look* — it is a pointer, not a statement. This holds regardless of
channel, and the private alerts repo is the second layer, not the first.

Examples (the whole body):
- Approval: *"An assistant proposed a new auto-tagging rule. Approve or dismiss
  it in Ledger → Pending approvals (expires Tue 4pm)."*
- Digest: *"In the last day the assistants: tagged 2 deposits, added 1 bill, set
  1 budget. 1 rule proposal is awaiting your approval. Open Ledger → Activity to
  review."*

## Two surfaces

### 1. Approval alerts (near-real-time)

When an automation **proposes** a two-phase action (a rule, a backlog sweep),
tell the household promptly so a human can approve or ignore it *before it
expires*. This is the direct answer to "would I get notified?" — you would.

Two parts:
- **Extend the pending-approval TTL.** It's 10 minutes today (`PENDING_ACTION_TTL_SECONDS`),
  sized for the *old* flow where the proposer confirmed in the same conversation.
  F-1 made approval a separate human-in-app step, so 10 minutes is now too short
  to notice a notification and act. Bump it to **~24h** for proposals awaiting a
  human. It's a code constant — **no migration.**
- **A short-interval Pi job** — `deploy/notify_approvals.py` on a ~15-minute
  timer — reads the pending queue (`GET /api/actions/pending`, already built for
  F-1) and posts a terse issue for each proposal it hasn't announced yet.
  "Already announced" is tracked by a **high-water-mark in a Pi state file**
  (last-notified `created_at`), mirroring how the ops-guardian keeps its status
  file — so **no schema change.**

### 2. The activity digest (daily)

Once a day, a summary of *everything* the assistants changed — not just the
gated two-phase actions but the single-phase writes too (the residual F-1
doesn't cover). This is the "nothing changes without me seeing it" story.

- `deploy/change_digest.py` on a daily timer (e.g. 08:00) reads a new
  `GET /api/activity/digest?since=<iso>`, renders terse markdown, and posts one
  issue to `OPS_ALERT_GH_REPO`. Quiet day (no writes) → nothing posted, exactly
  like the pantry pulse's `quiet` path.

## Components

| Piece | Where | Notes |
|---|---|---|
| `activity_digest(db, since, until)` | `derivations.py` | Groups `audit_log` rows by actor + action-type into counts + a plain summary + the pending-approvals count. Pure read; tripwire-safe (no `transactions`). |
| `GET /api/activity/digest?since=` | `app.py` | Thin caller (session or read-bearer), shape like `/api/inventory/pulse`. |
| `GET /api/actions/pending` | *(exists — F-1)* | The approvals job reuses it. |
| `post_issue` / a `notify()` helper | `deploy/_notify.py` | Extract pantry-pulse's `post_issue` into a shared helper so all three jobs share one. |
| `deploy/change_digest.py` + `pifinance-change-digest.timer` | `deploy/` | Daily. Mirrors `pantry_pulse.py`. |
| `deploy/notify_approvals.py` + `pifinance-notify-approvals.timer` | `deploy/` | ~15 min. State-file high-water-mark. |
| TTL bump | `actions.py` constant | `PENDING_ACTION_TTL_SECONDS` 600 → ~86400. No migration. |

New `.env` knobs (Pi-side; reuse the GitHub channel):
```
# reuse the existing alert channel (OPS_ALERT_GH_REPO / OPS_ALERT_GH_TOKEN)
CHANGE_DIGEST_ENABLED=1
CHANGE_DIGEST_HOUR=8
APPROVAL_NOTIFY_ENABLED=1
PENDING_ACTION_TTL_SECONDS=86400   # optional override of the 24h default
```
The digest/approval jobs need a **read-scope** bearer token (like
`PANTRY_PULSE_TOKEN`) to call the API — never a write token.

## Build order (each a gated increment)

1. **`activity_digest` derivation + `/api/activity/digest` route + tests.** No
   schema, no money path → balance gate zero-diff by construction.
2. **Shared `deploy/_notify.py`** (extract `post_issue`) **+ `change_digest.py`
   + its timer + an install doc.** Ships the daily digest. Alta installs the
   timer on the Pi (like the pantry-pulse timer).
3. **`notify_approvals.py` + its timer + the TTL bump.** Ships the approval
   alerts. The TTL bump is a one-line constant change (gate zero-diff).
4. **(Optional, later)** a pluggable `NOTIFY_CHANNEL` (ntfy / Pushover / email)
   for faster, richer phone push than a GitHub issue; and an in-app
   **"recent changes — who changed what"** view over `audit_log` so you can
   review without leaving the app.

## Decisions for you

1. **Channel:** GitHub issue (zero new infra, the default here) — or a real-time
   push (ntfy is free + self-hostable; Pushover is a polished iOS app, ~$5). The
   push options are faster and phone-native but add a service + secret. My
   recommendation: **ship on GitHub first** (it's already wired), add a push
   channel later only if the GitHub latency annoys you.
2. **TTL:** extend pending approvals 10 min → 24h? **Recommended** — a companion
   to F-1, so a notification has time to be acted on.
3. **Digest scope:** cover **all** writes (including Charlee's in-app actions and
   single-phase MCP writes) for full visibility — or only the two-phase
   approvals? **Recommended: all** in the digest; approval alerts stay scoped to
   the pending queue.
4. **Cadence:** digest daily at 08:00, approvals every ~15 min — adjustable.
5. **Reassurance vs. noise:** a fully-quiet day posts nothing. Do you also want a
   weekly "all quiet, N reads, 0 writes" heartbeat, or silence-is-golden?

## What this is *not*

Not a threat detector. It doesn't judge whether a change "looks malicious" — F-1
already makes the *judgement* unnecessary by requiring your approval for the
risky path. This just gives you **visibility**: you're told what happened, and
for the gated actions, nothing happens until you say yes.
