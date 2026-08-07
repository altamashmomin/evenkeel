#!/usr/bin/env bash
# nightly-backup.sh — deploy-independent nightly snapshot of finance.db.
#
# Closes the gap NIGHTLY-BACKUP-DESIGN.md names: deploy.sh only backs up when
# a deploy happens, but real data changes daily (06:30 sync, manual entries,
# Ask tagging, pantry) — so the freshest local rollback point must not be
# bounded by deploy cadence. This is deploy.sh's steps 3+3b factored out and
# re-scoped to a SEPARATE pool (finance.db.nightly-<ts>), so a burst of
# same-day deploys can never crowd nightly snapshots out of retention.
#
# Run by pifinance-nightly-backup.timer at 03:00 (before the 06:30 sync and
# the 07:00 guardian, so the guardian always sees that night's file). It only
# ever READS finance.db (VACUUM INTO — WAL-safe, includes committed WAL data);
# it never stops the service, never touches the deploy pool
# (finance.db.bak-*) or the off-Pi golden copy.
#
# Config (.env, optional): NIGHTLY_KEEP_BACKUPS (default 14) — keep this
# BELOW the guardian's MAX_NIGHTLY_BACKUPS (default 16), same relationship as
# DEPLOY_KEEP_BACKUPS < MAX_BACKUPS.
#
# Exit: 0 wrote+verified a snapshot (prune is best-effort); 1 the snapshot
# itself failed or failed integrity_check — journald keeps the reason and the
# guardian's morning run flags the stale/corrupt pool.
set -euo pipefail

die() { echo "nightly-backup: error: $*" >&2; exit 1; }

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
[ -f finance.db ] || die "no finance.db in $APP_DIR — nothing to back up"

# venv python on the Pi; plain python3 for off-Pi dry-runs of this script.
PY="$APP_DIR/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)" || die "no python available"

# Optional key: when .env lacks it (the default), the grep fails and, under
# set -euo pipefail, an unguarded assignment would abort the run — same
# regression the deploy-prune dry-run caught. Fall through to the default.
KEEP="$(grep -E '^NIGHTLY_KEEP_BACKUPS=' .env 2>/dev/null | cut -d= -f2)" || true
KEEP="${KEEP:-14}"

TS="$(date +%Y-%m-%d-%H%M%S)"
TARGET="finance.db.nightly-$TS"
# Second-resolution timestamps: a same-second re-run would make VACUUM INTO
# fail on the existing file with a confusing error — name the real cause.
[ -e "$TARGET" ] && die "$TARGET already exists (re-run within the same second?) — try again"

echo "nightly-backup: writing $TARGET"
"$PY" -c "import sqlite3,sys; sqlite3.connect('finance.db').execute('VACUUM INTO ?', [sys.argv[1]])" "$TARGET" \
    || die "VACUUM INTO failed — no snapshot written"
[ -s "$TARGET" ] || die "snapshot is empty"

# Integrity gate: nothing old is pruned unless tonight's snapshot is provably
# restorable (immutable=1 so reading never spawns -wal/-shm sidecars). A
# corrupt snapshot is LEFT IN PLACE deliberately — it becomes the newest file
# the guardian integrity-checks, which turns tomorrow's report red.
res="$("$PY" -c 'import sqlite3,sys; print(sqlite3.connect("file:%s?immutable=1"%sys.argv[1],uri=True).execute("PRAGMA integrity_check").fetchone()[0])' "$TARGET" 2>/dev/null | head -1)"
if [ "$res" != "ok" ]; then
    echo "nightly-backup: NEW SNAPSHOT FAILED integrity_check ('${res:-unreadable}') — keeping all old snapshots" >&2
    exit 1
fi
echo "nightly-backup: integrity ok"

prune_nightlies() {
    shopt -s nullglob
    local snaps=() f
    for f in finance.db.nightly-*; do        # lexical order == chronological
        case "$f" in *-wal|*-shm|*-journal) continue;; esac
        snaps+=("$f")
    done
    shopt -u nullglob
    local n=${#snaps[@]}
    (( n > KEEP )) || { echo "nightly-backup: $n snapshot(s) <= keep limit ($KEEP)"; return 0; }
    local del=$(( n - KEEP )) i
    echo "nightly-backup: pruning $del old snapshot(s), keeping newest $KEEP of $n"
    for (( i=0; i<del; i++ )); do            # $TARGET is newest → never here
        rm -f -- "${snaps[$i]}" && echo "nightly-backup: pruned ${snaps[$i]}" \
            || echo "nightly-backup: WARNING — could not remove ${snaps[$i]}"
    done
}
prune_nightlies || echo "nightly-backup: WARNING — prune failed (non-fatal; snapshot is safe)"

echo "nightly-backup: done ($TARGET)"
