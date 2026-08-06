# Backup retention for deploy.sh — design (for review)

**Status:** proposed, not implemented. Awaiting Alta's sign-off before any
edit to `deploy/deploy.sh`. This is ops/deploy tooling — no schema, verb,
derivation, or money path — so the balance gate does not apply; but it edits
the script that runs against the live Pi, so it ships as one small increment
with a dry-run before it's trusted unattended (per the per-increment loop).

## Problem

`deploy/deploy.sh` writes a fresh `finance.db.bak-<timestamp>` on **every
run** (step 3, line 72), via `VACUUM INTO`, *before* the dry-run gate (step 4).
Nothing ever prunes them. So the count grows without bound and — crucially —
grows on runs that never actually deploy:

- a run that **aborts at the gate** still left a backup behind (step 3 ran);
- a same-day **no-op re-run** (the propagation-race pattern seen twice: the
  Aug-3 pantry MVP and the Aug-5 broken-match deploy) writes another;
- every real deploy writes one too.

On 2026-08-06 this reached **28 files** in `~/pifinance` and tripped the Ops
guardian's amber `> 12 files` alert two days running (issues #4, #6 on
`altamashmomin/evenkeel`). It was pruned by hand to the newest 10 after
confirming the newest backup's `integrity_check` passed. Without a real fix it
recurs on the next busy deploy day.

## Four decisions (options → recommendation)

### 1. Retention policy: keep-newest-N vs. time-based thinning

- **Keep-newest-N** — keep the N most-recent `.bak-*`, delete the rest. Simple;
  the safety argument is trivial (sort, keep the top N, the just-written backup
  is always in the top N); directly answers the guardian's own *count*-based
  alert.
- **Time-based thinning** — e.g. keep all from the last 48h, then one per day,
  then none past a horizon. Degrades more gracefully if deploy cadence changes,
  but the shell is much fiddlier and its failure mode is worse (a bug deletes
  *more* than intended).

**Recommend: keep-newest-N.** For a Pi doing a handful of deploys, N-based is
plenty, its correctness is obvious at a glance, and it maps 1:1 onto the alert
it's fixing (a count threshold). The deep-history rollback case is already
covered by the **off-Pi golden backup**, not by hoarding local `.bak-*`.
Because the backup filename embeds its creation time
(`finance.db.bak-YYYY-MM-DD-HHMMSS`), a **lexical sort of the names is exactly a
chronological sort** — so "oldest" is unambiguous and doesn't depend on mtime
(which a rollback `cp` could disturb). This agrees with the guardian's
mtime-based "newest" selection in practice.

### 2. Where the prune lives: deploy.sh vs. the Ops guardian

- **Inside deploy.sh, right after it writes + verifies the new backup.** The
  runs that cause the bloat (gate-failed, no-op re-runs) then clean up after
  themselves, because step 3 always runs regardless of what happens later. The
  prune is the same deterministic operation every time, co-located with the code
  that creates the files.
- **In the Ops guardian (`ops-health-check.sh`).** ⚠️ **This would be a real
  change of philosophy, not a bug fix.** The guardian is *deliberately
  recommend-only* — its own header says it "never restarts a service, prunes a
  backup, or deploys." Folding a `rm` into it makes the one component whose job
  is to *observe and alert* start *mutating* the thing it observes. Flagging
  this explicitly rather than deciding it silently, as asked.

**Recommend: inside deploy.sh.** It keeps the guardian read-only (its integrity
depends on that), and it puts the cleanup where the files are born. The guardian
keeps its `> MAX_BACKUPS` amber as an independent backstop — if deploy.sh's
prune ever silently stops working, the guardian still notices.

### 3. Configuration: hardcoded vs. flag vs. .env var

- **Hardcoded constant** — zero config, but re-tuning means editing the script.
- **deploy.sh flag** — bad fit here: deploy.sh is sometimes run by the
  `ledger-release` agent's exact printed commands and in near-unattended
  re-runs; a flag you must remember every time is a footgun and would drift.
- **`.env` var** — matches the existing precedent exactly (deploy.sh greps
  `PORT` from `.env`; the guardian reads `MAX_BACKUPS`, `OPS_ALERT_GH_REPO`,
  etc. from it). Set once, applies to every run including agent-driven ones.

**Recommend: `.env` var with a sane default.** `DEPLOY_KEEP_BACKUPS`, read like
`PORT` (`grep … .env`), defaulting to **10** so it works with zero config. A
distinct name from the guardian's `MAX_BACKUPS` on purpose — they mean different
things (keep-count vs. warn-threshold).

### 4. Default N, and its relationship to the guardian

**Recommend N = 10.** The guardian warns at `> MAX_BACKUPS` (default **12**), so
pruning to 10 after every run keeps the daily count comfortably under the alert
with 2 files of headroom. Invariant worth stating: **`DEPLOY_KEEP_BACKUPS` <
`MAX_BACKUPS`**, else deploy.sh would prune to a number the guardian still
alarms on. Ten backups also means the last ten deploy states are locally
recoverable — far more than a two-person household needs before falling back to
the off-Pi golden copy.

## Recommended design

Insert a **best-effort** prune block into deploy.sh immediately after the
new backup is written and proven non-empty (right after line 74,
`[ -s "$BACKUP" ] || die …`), *before* the gate. Sketch — **for review, not yet
applied:**

```bash
# --- 3b. prune old backups (keep newest N; best-effort, never blocks) --------
# Runs right after the new backup is written + verified, so gate-failed and
# no-op re-runs — the very runs that bloat the dir — self-limit. Non-fatal by
# design: housekeeping must never abort a deploy.
KEEP="$(grep -E '^DEPLOY_KEEP_BACKUPS=' .env 2>/dev/null | cut -d= -f2)"; KEEP="${KEEP:-10}"
prune_backups() {
    shopt -s nullglob
    local baks=() f
    for f in finance.db.bak-*; do            # lexical == chronological (ts in name)
        case "$f" in *-wal|*-shm|*-journal) continue;; esac   # never a sidecar
        baks+=("$f")
    done
    shopt -u nullglob
    local n=${#baks[@]}
    (( n > KEEP )) || { echo "   $n backup(s) ≤ keep limit ($KEEP) — nothing to prune"; return 0; }
    # Don't delete old rollback points until the just-taken backup is PROVEN
    # restorable (mirror the guardian's integrity_check-before-trust). immutable=1
    # so reading it never spawns -wal/-shm next to the file.
    local res
    res="$("$PY" -c 'import sqlite3,sys; print(sqlite3.connect("file:%s?immutable=1"%sys.argv[1],uri=True).execute("PRAGMA integrity_check").fetchone()[0])' "$BACKUP" 2>/dev/null | head -1)"
    if [ "$res" != "ok" ]; then
        echo "   WARNING — new backup did not pass integrity_check ('${res:-unreadable}'); keeping ALL backups this run"
        return 0
    fi
    local del=$(( n - KEEP )) i                # baks[] ascending → head is oldest
    for (( i=0; i<del; i++ )); do              # $BACKUP is newest → in kept tail, never here
        rm -f -- "${baks[$i]}" && echo "   pruned: ${baks[$i]}" \
            || echo "   WARNING — could not remove ${baks[$i]}"
    done
}
prune_backups || echo "   WARNING — prune step failed (non-fatal; deploy continues)"
```

## Safety contract (maps to the stated requirements)

- **Never delete the run's own rollback point.** `$BACKUP` is the newest file,
  so keep-newest-N always retains it; it can never be selected for deletion.
- **Never touch the off-Pi golden backup.** The glob is scoped to the Pi's
  working dir (`finance.db.bak-*` in `$REPO_DIR`); the golden copy lives on
  another machine and is structurally out of reach — no special-casing needed.
- **Prune only after the count exceeds the keep limit, post-write.** The
  `(( n > KEEP ))` guard is evaluated *after* the new backup exists, so a fresh
  or already-small dir is a no-op.
- **Integrity-before-trust.** The old backups are deleted only once the *new*
  backup passes `PRAGMA integrity_check` (opened `immutable=1`, exactly as the
  guardian does). If the new rollback point isn't provably good, keep every old
  one and warn.
- **Housekeeping never blocks a deploy.** The whole block is non-fatal under
  `set -euo pipefail`: guarded arithmetic, per-file `rm … || echo`, and a
  top-level `prune_backups || echo`. A failed unlink or a corrupt-new-backup
  warning does not abort the run.
- **Guardian stays read-only.** The delete lives in deploy.sh; the guardian
  keeps only its advisory `> MAX_BACKUPS` count as an independent backstop.

## The increment & how it gets verified

One small commit on `rework`: the prune block above + a one-line note in the
deploy.sh header comment (step 3 → "3. back up … and prune to the newest N")
+ a `DEPLOY_KEEP_BACKUPS` line in `.env.example` if one exists (otherwise
documented in `deploy/mcp-read-tier.md`/the deploy notes).

Because this doesn't touch app code, the check is a **dry-run of the prune
logic itself**, not the balance gate:

1. In a scratch dir, `touch` ~20 dummy `finance.db.bak-*` files with spread-out
   timestamps + a couple of `-wal`/`-shm` sidecars; run the extracted
   `prune_backups` against them and confirm it keeps exactly the newest N,
   never a sidecar, and never the designated `$BACKUP`.
2. Point it at a *real* small `VACUUM INTO` copy as `$BACKUP` and confirm the
   integrity_check path returns `ok` and pruning proceeds; corrupt a byte and
   confirm it refuses to prune and warns.
3. On the Pi, a normal `deploy.sh` run (or the next real increment) exercises it
   live: it should report `pruned: …` lines and leave exactly N `.bak-*`, and
   the next 07:00 guardian run should read green on the backup-count line.

## Open questions for sign-off

1. **N = 10** and **`.env` var `DEPLOY_KEEP_BACKUPS`** — good, or prefer a
   different number / a hardcoded constant?
2. Confirm **keep-newest-N** over time-based thinning (recommended).
3. Confirm the prune stays **in deploy.sh** and the guardian stays read-only
   (recommended) — i.e. we are *not* folding a delete into the guardian.
