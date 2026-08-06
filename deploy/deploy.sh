#!/usr/bin/env bash
# deploy.sh — advance the live Pi deployment to a new git ref, safely.
#
# The one command for both the initial v1.0 -> rework go-live walk and every
# future increment. It NEVER mutates the live database until a copy-based gate
# has proven, on the real data, that the migration moves no money (balance and
# every monthly total unchanged to the cent). Enforces CLAUDE.md hard rules 6
# (backup before touching finance.db) and 8 (small, verified steps).
#
# Usage (run as the service user, from the repo root on the Pi):
#     deploy/deploy.sh <new_ref> [<old_ref>]
#
#   <new_ref>  git ref to deploy (e.g. rework, main, a tag, a commit)
#   <old_ref>  the currently-deployed ref; defaults to the current HEAD.
#              For the first go-live this is the v1.0 tag:
#                   deploy/deploy.sh rework v1.0
#
# What it does, in order:
#   1. sanity-check the repo + a clean working tree
#   2. git fetch (tags included)
#   3. back up finance.db -> finance.db.bak-<timestamp>, then prune old backups
#      down to the newest N (DEPLOY_KEEP_BACKUPS, default 10) so re-runs don't
#      bloat the SD card — best-effort, never aborts the deploy
#   4. DRY-RUN gate on the backup copy (verify_live_migration.py) — abort if it
#      moves money or errors; the live DB is still untouched here
#   5. stop the service, checkout <new_ref>, pip install, migrate --live
#   6. start the service and smoke-check it
# On any failure after step 5 begins, it prints the exact rollback commands.
set -euo pipefail

die() { echo "deploy: error: $*" >&2; exit 1; }

[ $# -ge 1 ] || die "usage: deploy/deploy.sh <new_ref> [<old_ref>]"
NEW_REF="$1"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# --- preconditions -----------------------------------------------------------
[ -f app.py ] && [ -f migrate.py ] || die "not in the Ledger repo root ($REPO_DIR)"
[ -f finance.db ] || die "no finance.db here — this script is for the live Pi deploy only"
PY="$REPO_DIR/venv/bin/python"
PIP="$REPO_DIR/venv/bin/pip"
[ -x "$PY" ] || die "no venv at $PY — create it with: python3 -m venv venv"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not a git checkout"
if [ -n "$(git status --porcelain)" ]; then
    die "working tree has local changes — commit/stash them so checkout is safe"
fi

OLD_REF="${2:-$(git rev-parse HEAD)}"
PORT="$(grep -E '^PORT=' .env 2>/dev/null | cut -d= -f2)"; PORT="${PORT:-8080}"
TS="$(date +%Y-%m-%d-%H%M%S)"
BACKUP="finance.db.bak-$TS"

echo "== Ledger deploy =="
echo "   old ref : $OLD_REF"
echo "   new ref : $NEW_REF"
echo "   backup  : $BACKUP"
echo

# --- 2. fetch ----------------------------------------------------------------
echo "-> git fetch"
git fetch --tags --quiet origin
git rev-parse --verify --quiet "$NEW_REF^{commit}" >/dev/null || die "unknown ref: $NEW_REF"

# --- 3. backup (hard rule 6) -------------------------------------------------
echo "-> backing up finance.db -> $BACKUP"
# Consistent hot backup: VACUUM INTO writes a single-file snapshot that INCLUDES
# committed WAL data (and defragments). A raw `cp` of the WAL-mode live DB — the
# service is still running here — silently omits whatever sits in finance.db-wal,
# so a rollback could lose the most recent transactions (demonstrated: cp dropped
# a just-committed row that VACUUM INTO kept). Reads finance.db, never modifies
# it; uses the venv python, so no sqlite3 CLI is needed on the Pi.
"$PY" -c "import sqlite3,sys; sqlite3.connect('finance.db').execute('VACUUM INTO ?', [sys.argv[1]])" "$BACKUP" \
    || die "backup (VACUUM INTO) failed — aborting before any change"
[ -s "$BACKUP" ] || die "backup is empty — aborting before any change"

# --- 3b. prune old backups (keep newest N; best-effort, never blocks) ---------
# deploy.sh writes a backup on EVERY run — including gate-failed and same-day
# no-op re-runs — so without pruning, .bak files pile up and eventually trip the
# Pi Ops guardian's backup-count alert. Prune here, right after the new backup
# is written and verified, so the very runs that cause the bloat self-limit.
# Non-fatal by design: housekeeping must never abort a deploy, so the whole
# block is guarded and the top-level call swallows any error.
#   - keeps the newest N (N = DEPLOY_KEEP_BACKUPS from .env, default 10)
#   - the just-written $BACKUP is newest, so it is ALWAYS kept (this run's own
#     rollback point can never be deleted)
#   - only *.bak-* here; the off-Pi "golden" backup lives elsewhere, unreachable
#   - deletes nothing until the new backup PROVES restorable via integrity_check
#     (mirrors the guardian's integrity-before-trust discipline)
# `|| true` because this key is OPTIONAL: when .env lacks it (the default), the
# grep fails and, under `set -euo pipefail`, an unguarded assignment would abort
# the whole deploy. Fall through to the default instead.
KEEP="$(grep -E '^DEPLOY_KEEP_BACKUPS=' .env 2>/dev/null | cut -d= -f2)" || true
KEEP="${KEEP:-10}"
prune_backups() {
    shopt -s nullglob
    local baks=() f
    for f in finance.db.bak-*; do            # lexical order == chronological (ts in name)
        case "$f" in *-wal|*-shm|*-journal) continue;; esac   # never a sidecar
        baks+=("$f")
    done
    shopt -u nullglob
    local n=${#baks[@]}
    (( n > KEEP )) || { echo "   $n backup(s) <= keep limit ($KEEP) — nothing to prune"; return 0; }
    # immutable=1 so reading the backup never spawns -wal/-shm beside it.
    local res
    res="$("$PY" -c 'import sqlite3,sys; print(sqlite3.connect("file:%s?immutable=1"%sys.argv[1],uri=True).execute("PRAGMA integrity_check").fetchone()[0])' "$BACKUP" 2>/dev/null | head -1)"
    if [ "$res" != "ok" ]; then
        echo "   WARNING — new backup did not pass integrity_check ('${res:-unreadable}'); keeping ALL backups this run"
        return 0
    fi
    local del=$(( n - KEEP )) i               # baks[] ascending → head is oldest
    echo "   pruning $del old backup(s), keeping newest $KEEP of $n"
    for (( i=0; i<del; i++ )); do             # $BACKUP is newest → in kept tail, never here
        rm -f -- "${baks[$i]}" && echo "   pruned: ${baks[$i]}" \
            || echo "   WARNING — could not remove ${baks[$i]}"
    done
}
prune_backups || echo "   WARNING — backup prune step failed (non-fatal; deploy continues)"

# --- 4. dry-run gate on the copy (live DB still untouched) --------------------
echo "-> dry-run gate: proving no money moves ($OLD_REF -> $NEW_REF)"
if ! "$PY" deploy/verify_live_migration.py --db "$BACKUP" --old "$OLD_REF" --new "$NEW_REF"; then
    die "gate failed — the live database was NOT touched. Backup kept at $BACKUP"
fi
echo

# --- 5. apply for real -------------------------------------------------------
rollback() {
    echo >&2
    echo "deploy: FAILED mid-apply. To roll back:" >&2
    echo "    sudo systemctl stop pifinance" >&2
    echo "    cp '$BACKUP' finance.db" >&2
    echo "    git checkout $OLD_REF" >&2
    echo "    sudo systemctl start pifinance" >&2
    echo "    sudo systemctl restart ledger-mcp   # if installed" >&2
}
trap rollback ERR

echo "-> stopping service"
sudo systemctl stop pifinance
echo "-> checkout $NEW_REF"
git checkout --quiet "$NEW_REF"
echo "-> pip install (in case deps changed)"
"$PIP" install -q -r requirements.txt
echo "-> migrate.py apply --live finance.db"
"$PY" migrate.py apply --live finance.db
echo "-> starting service"
sudo systemctl start pifinance
trap - ERR

# ledger-mcp has Requires=pifinance, so stopping the app also stopped the MCP
# sibling; starting the app does NOT bring it back (that's the reverse
# direction). Restart it if it's installed — outside the rollback trap, since
# the MCP tier is non-critical to the app itself.
if systemctl cat ledger-mcp.service >/dev/null 2>&1; then
    echo "-> restarting ledger-mcp (stopped with the app via Requires=)"
    sudo systemctl restart ledger-mcp \
        || echo "   WARNING — ledger-mcp restart failed; check: journalctl -u ledger-mcp -e"
fi

# --- 6. smoke check ----------------------------------------------------------
sleep 2
echo
echo "-> service status"
systemctl --no-pager --lines=0 status pifinance | head -4 || true
echo "-> app reachable on :$PORT ?"
if curl -fs "http://localhost:$PORT/api/status" >/dev/null; then
    echo "   OK — /api/status responded"
else
    echo "   WARNING — /api/status did not respond; check: journalctl -u pifinance -e"
fi
echo
echo "== done. Now open the app and confirm the balance + each month's Spent"
echo "   match what v1.0 showed. Rollback backup, if ever needed: $BACKUP"
