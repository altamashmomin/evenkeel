# Nightly backup routine — design (BUILT Aug 6, 2026; not yet deployed)

**Status:** decisions ratified by Alta (Aug 6, 2026) — separate pool, N=14,
golden-backup refresh scoped out — and **built as designed** (`deploy/
nightly-backup.sh` + `pifinance-nightly-backup.{service,timer}` +
`.gitignore` `*.db.nightly-*` + the guardian's new nightly block). Off-Pi
tooling, no schema/verb/money path → balance gate N/A; dry-run-verified before
trust (below). **Not yet installed on the Pi** — the units need the usual
`pi`→`altamash` sed-on-copy + `systemctl enable --now`.

**Two things the build added beyond this doc (both flagged in review):**
1. **Retired the guardian's deploy-pool AGE check** (`MAX_BACKUP_AGE_H`). Once
   nightlies exist, "newest `finance.db.bak-*` is >7d old" just means "no
   deploy lately" — a non-signal that would false-alarm on a quiet week with
   perfect nightly coverage. Freshness now lives on the nightly pool
   (`MAX_NIGHTLY_AGE_H=30`); the deploy pool keeps only its restorability +
   `MAX_BACKUPS` count checks. `MAX_BACKUP_AGE_H` is gone.
2. **Same-second collision guard.** Second-resolution timestamps mean a
   same-second re-run would hit `VACUUM INTO`'s "output file already exists"
   with a confusing SQLite error; the script now names the real cause and
   exits cleanly (caught in the dry-run).

**Dry-run coverage (off-Pi, extracted script):** first run writes + integrity-
oks + no-op prune; 20 snapshots + sidecars → prunes to newest 14, sidecars
untouched, newest kept + integrity-ok; `.env` `NIGHTLY_KEEP_BACKUPS=5` honored;
same-second collision dies with the clear message; a bad source → exit 1,
nothing written. Guardian two-pool block runs clean (nightly + deploy).

## Problem

`finance.db` is currently backed up in exactly one place: `deploy/deploy.sh`
step 3, which runs only when a deploy runs. That means the freshest available
local rollback point is bounded by "how long since the last deploy" — not by
any actual data-freshness guarantee. Real data changes daily regardless of
deploy cadence (SimpleFIN sync at 06:30, manual entries, Ask-tab tagging,
pantry updates), and deploys are irregular — the changelog shows stretches of
several days with none, and other days with a dozen. The Ops guardian's own
`MAX_BACKUP_AGE_H` (default 168h = 7 days) tacitly tolerates up to a week of
staleness before even going amber, which is generous to the point of masking
the real gap.

The other existing safety net — the off-Pi "golden" backup — was taken once,
at initial go-live (July 26). It protects against total Pi loss (SD card
death, theft, etc.) but is now three weeks stale and not refreshed on any
schedule; restoring from it would lose everything since.

**Gap:** there is no backup mechanism decoupled from deploy activity. A quiet
week (no deploys, but daily bank syncs and manual entries) leaves the newest
rollback point up to 7+ days behind live data.

## What this increment covers (and what it doesn't)

**In scope:** a nightly, deploy-independent, on-Pi snapshot of `finance.db`,
so the newest local rollback point is never more than ~24h stale regardless
of deploy activity. Mirrors `deploy.sh`'s own backup step (WAL-safe
`VACUUM INTO`, integrity-checked, self-pruning) and the guardian's existing
daily-timer pattern (`pifinance-ops.timer`).

**Out of scope, deliberately:** refreshing the off-Pi "golden" copy on a
schedule. That's a *different* risk (whole-Pi loss, not just stale local
backups) and a *different* mechanism (needs a second reachable machine over
Tailscale, which isn't guaranteed available at any given hour) — it deserves
its own design pass rather than being smuggled into this one. Flagged as a
natural follow-up, not silently dropped.

## Three decisions

### 1. Shared backup pool (same `finance.db.bak-*` files deploy.sh uses) vs. a separate nightly pool

- **Shared pool.** The nightly script writes the exact same
  `finance.db.bak-<timestamp>` naming deploy.sh already uses, into the same
  `APP_DIR`. Free reuse of everything that already understands that glob: the
  guardian's freshness/restorability/count checks, `.gitignore`'s
  `*.db.bak-*` entry, and deploy.sh's own `prune_backups` (which already
  treats "oldest by lexical/timestamp order" generically — it doesn't care
  *why* a `.bak-*` file was written). One mental model: "the backup pool."
- **Separate pool** (e.g. `finance.db.nightly-<timestamp>`). Keeps the two
  purposes distinct — deploy backups are code-version rollback points,
  nightly backups are a pure data safety net — so a burst of same-day deploys
  (the Aug 3–6 pattern hit 14 in one day) can't crowd nightly snapshots out of
  a shared keep-newest-N window. Costs: a second `.gitignore` pattern, a
  second prune routine (small — the same ~20-line function, re-scoped to the
  new prefix), and the guardian needs to learn the second prefix for its
  freshness/count checks (or gets a parallel check block).

**Recommend: separate pool.** The whole reason this increment exists is that
deploy-triggered backups aren't a reliable freshness signal — folding nightly
snapshots into the same keep-newest-N window re-couples the two things this
design is trying to decouple. A quiet-but-eventful stretch (lots of real data,
zero deploys) is exactly the case worth protecting, and a busy deploy day
shouldn't be able to starve it.

### 2. Retention depth for nightly snapshots

Keep-newest-N, same reasoning as `DEPLOY_KEEP_BACKUPS` (lexical filename sort
== chronological; the just-written one is always newest and never prunable;
integrity-checked before anything old is deleted).

- **N = 7** (one week). Tightest, least SD-card cost.
- **N = 14** (two weeks). Recommended — matches the existing doc's framing
  ("far more than a two-person household needs before falling back to the
  off-Pi golden copy"), still a small footprint (finance.db is small; O(10s
  of MB) each), gives real slack if an issue goes unnoticed over a weekend.
- **N = 30** (a month). Most slack, more SD-card use for a Pi already tight
  enough to have an Ops disk check.

**Recommend: N = 14**, env var `NIGHTLY_KEEP_BACKUPS` (default 14) — same
`.env`-driven precedent as `DEPLOY_KEEP_BACKUPS`/`MAX_BACKUPS`.

### 3. Guardian coverage

The guardian (`ops-health-check.sh`) currently checks the `finance.db.bak-*`
pool for freshness/restorability/count. With a separate nightly pool it
should gain a parallel check block (new prefix, new thresholds) rather than
silently having a whole backup mechanism it knows nothing about. Proposed
thresholds, matching the sync-freshness check's shape
(`MAX_SYNC_AGE_H=26` — a day plus slack):
- `MAX_NIGHTLY_AGE_H` amber above, default **30** (a day plus slack for
  `RandomizedDelaySec`).
- Restorability: integrity-check the newest nightly file, same as the
  existing block.
- Count: amber above `MAX_NIGHTLY_BACKUPS`, default **16** (> the keep-14
  default, same relationship `DEPLOY_KEEP_BACKUPS < MAX_BACKUPS` already
  establishes).

This is the one piece that touches an existing tracked file
(`deploy/ops-health-check.sh`) rather than only adding new ones — small,
additive, same pattern as its existing blocks.

## Proposed shape (sketch, not final)

- **New script:** `deploy/nightly-backup.sh` — `VACUUM INTO`
  `finance.db.nightly-<timestamp>`, integrity-check it, prune to newest
  `NIGHTLY_KEEP_BACKUPS`. Essentially `deploy.sh`'s steps 3+3b factored out
  and re-scoped to the new prefix; no service stop/checkout/migrate — it only
  ever reads `finance.db`, never touches the running app.
- **New systemd units:** `pifinance-nightly-backup.service` +
  `.timer`, same family as `pifinance-sync.timer`/`pifinance-ops.timer`.
  Proposed time: **03:00** — after the day's activity, well clear of the
  06:30 sync and the 07:00 guardian run, so the guardian's freshness check
  always sees that night's file already in place.
- **Install location:** inside the repo (`deploy/`), tracked and deployed via
  `git pull` like `deploy.sh`/`ops-health-check.sh` themselves — the script
  writes only `.db.nightly-*` files, which (once added to `.gitignore`) never
  dirty the tree, so it doesn't need the guardian's out-of-repo workaround
  (that workaround exists specifically because the guardian writes an
  *un-ignored* `ops-status.txt`; this script writes nothing but gitignored
  backup files).
- **`.gitignore`:** add `*.db.nightly-*` alongside the existing
  `*.db.bak-*` line.
- **Guardian:** the new check block described above, in
  `deploy/ops-health-check.sh`.

## Safety contract (same bar as the deploy-backup retention design)

- Never touches the running app or `finance.db` beyond a read (`VACUUM INTO`
  is non-mutating on the source).
- Never deletes the run's own just-written snapshot.
- Prunes only after the new file passes `PRAGMA integrity_check`
  (`immutable=1`, same as every other integrity check in this codebase).
- Best-effort/non-fatal: a failed prune or a failed backup just gets logged
  (journald) and the guardian's amber/red catches it the next morning — no
  silent total failure, but also nothing that can wedge the Pi.
- Never touches the off-Pi golden copy or the deploy-backup pool.

## Open questions for sign-off

1. Separate nightly pool vs. sharing deploy.sh's `finance.db.bak-*` pool
   (recommended: separate).
2. Retention depth — N = 7 / 14 / 30 nightly snapshots (recommended: 14).
3. Should the off-Pi golden-backup refresh be scoped as an explicit follow-up
   after this ships, or is it worth pulling into scope now despite the added
   cross-machine dependency?
