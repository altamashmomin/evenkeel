#!/usr/bin/env bash
# ops-health-check.sh — Ledger Pi-side Ops guardian.
#
# Runs ON THE PI (daily, via pifinance-ops.timer) and answers the question the
# cloud health-sweep structurally CANNOT: "is production actually up, fresh, and
# safe right now?" It is DETERMINISTIC and dependency-light on purpose — plain
# shell + coreutils + systemctl + curl + sqlite3 — so it keeps working even when
# the Anthropic API key is dead, the network is flaky, or Python is wedged.
#
# It is READ-ONLY and NON-DESTRUCTIVE. It never restarts a service, prunes a
# backup, or deploys — those stay human (and are what a red alert asks for). It
# NEVER opens finance.db (CORE-DESIGN invariant 6): sync freshness is read from
# systemd, not the live DB; only *.bak-* files are ever opened, read-only.
#
# Output: a human-readable report to stdout (captured by journald), a heartbeat
# status file, and an exit code — 0 green, 1 amber (warnings), 2 red (critical).
# On amber/red it can file a GitHub issue so the cloud Chief of Staff sees it
# (best-effort; only if `gh` is installed+authed and OPS_ALERT_GH_REPO is set).
#
# Config (all optional; put overrides in the app's .env, which the unit loads):
#   APP_DIR             install dir            (default: script's parent dir)
#   PORT                app port for /api/status probe   (default: 8080)
#   WEB_SERVICE         web unit name          (default: pifinance)
#   MCP_SERVICE         MCP unit name          (default: ledger-mcp)
#   SYNC_SERVICE        sync unit name         (default: pifinance-sync.service)
#   DISK_WARN_PCT       amber above this %     (default: 80)
#   DISK_CRIT_PCT       red above this %       (default: 90)
#   MAX_SYNC_AGE_H      amber if last good sync older   (default: 26)
#   MAX_BACKUP_AGE_H    amber if newest backup older    (default: 168 = 7d)
#   MAX_BACKUPS         amber if more bak files than    (default: 12)
#   ASK_KEY_EXPIRES     YYYY-MM-DD; amber within 14d/past (default: unset)
#   OPS_ALERT_GH_REPO   e.g. altamashmomin/evenkeel; with the token below, files
#                       a GitHub issue on amber/red (the Chief-of-Staff bridge)
#   OPS_ALERT_GH_TOKEN  a fine-grained PAT (that repo, issues:write) for the POST
#   OPS_STATUS_FILE     heartbeat path         (default: $APP_DIR/ops-status.txt)
set -uo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PORT="${PORT:-8080}"
WEB_SERVICE="${WEB_SERVICE:-pifinance}"
MCP_SERVICE="${MCP_SERVICE:-ledger-mcp}"
SYNC_SERVICE="${SYNC_SERVICE:-pifinance-sync.service}"
DISK_WARN_PCT="${DISK_WARN_PCT:-80}"
DISK_CRIT_PCT="${DISK_CRIT_PCT:-90}"
MAX_SYNC_AGE_H="${MAX_SYNC_AGE_H:-26}"
MAX_BACKUP_AGE_H="${MAX_BACKUP_AGE_H:-168}"
MAX_BACKUPS="${MAX_BACKUPS:-12}"
OPS_STATUS_FILE="${OPS_STATUS_FILE:-$APP_DIR/ops-status.txt}"

# Worst severity seen so far: 0 green, 1 amber, 2 red.
WORST=0
LINES=()
bump() { (( $1 > WORST )) && WORST=$1; return 0; }  # never leak the (( )) status
ok()   { LINES+=("  ✓ $1"); }
warn() { LINES+=("  ⚠ $1"); bump 1; }
crit() { LINES+=("  ✗ $1"); bump 2; }
have() { command -v "$1" >/dev/null 2>&1; }

# ── 1. Web app: unit active + HTTP 200 on /api/status ───────────────────────
if have systemctl && systemctl is-active --quiet "$WEB_SERVICE"; then
  code=""
  if have curl; then
    code="$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 8 \
            "http://127.0.0.1:${PORT}/api/status" 2>/dev/null)"
  fi
  if [[ "$code" == "200" ]]; then
    ok "web app ($WEB_SERVICE) active and serving /api/status (200)"
  elif [[ -z "$code" ]]; then
    warn "web app ($WEB_SERVICE) active but /api/status probe unavailable (curl missing/timed out)"
  else
    crit "web app ($WEB_SERVICE) active but /api/status returned '$code' — app may be wedged"
  fi
else
  crit "web app ($WEB_SERVICE) is NOT active — Charlee/Alta cannot reach Ledger"
fi

# ── 2. MCP sibling (Alta's read/write tier; degraded, not fatal) ────────────
if have systemctl; then
  if systemctl is-active --quiet "$MCP_SERVICE"; then
    ok "MCP server ($MCP_SERVICE) active"
  else
    warn "MCP server ($MCP_SERVICE) not active — Alta's Claude tools are down (the app itself is unaffected)"
  fi
fi

# ── 3. Disk (SD-card protection) ────────────────────────────────────────────
pct="$(df -P "$APP_DIR" 2>/dev/null | awk 'NR==2{gsub(/%/,"",$5); print $5}')"
if [[ -n "$pct" ]]; then
  if   (( pct >= DISK_CRIT_PCT )); then crit "disk ${pct}% full (≥${DISK_CRIT_PCT}%) — imminent write failures; prune backups/logs"
  elif (( pct >= DISK_WARN_PCT )); then warn "disk ${pct}% full (≥${DISK_WARN_PCT}%) — trending toward full"
  else ok "disk ${pct}% used"; fi
else
  warn "could not read disk usage for $APP_DIR"
fi

# ── 4. Sync freshness (from systemd, never from finance.db) ─────────────────
if have systemctl; then
  # A Type=oneshot service never enters the 'active' state, so its
  # ActiveExitTimestamp is always empty — read ExecMainExitTimestamp (when
  # ExecStart's process last exited) for the real last-run time.
  st="$(systemctl show "$SYNC_SERVICE" \
        -p ExecMainStatus -p ExecMainExitTimestamp -p Result 2>/dev/null)"
  exit_status="$(sed -n 's/^ExecMainStatus=//p' <<<"$st")"
  last_finish="$(sed -n 's/^ExecMainExitTimestamp=//p' <<<"$st")"
  if [[ -n "$last_finish" && "$last_finish" != "n/a" ]] && have date; then
    last_epoch="$(date -d "$last_finish" +%s 2>/dev/null || echo 0)"
    now_epoch="$(date +%s)"
    age_h=$(( (now_epoch - last_epoch) / 3600 ))
    if [[ "${exit_status:-0}" != "0" ]]; then
      crit "last SimpleFIN sync FAILED (exit ${exit_status}) — bank data is silently stale"
    elif (( age_h > MAX_SYNC_AGE_H )); then
      warn "last successful sync was ${age_h}h ago (> ${MAX_SYNC_AGE_H}h) — data may be stale; check the timer"
    else
      ok "SimpleFIN sync ran ${age_h}h ago, exit 0"
    fi
  else
    warn "no recorded run for $SYNC_SERVICE yet — verify the sync timer is enabled"
  fi
fi

# ── 5. Backups: recent, restorable, not bloating the card ───────────────────
# Exclude SQLite sidecars (-wal/-shm/-journal): they are not backups, and
# counting them inflates the total and could pick a non-db file as "newest".
shopt -s nullglob
baks=()
for _f in "$APP_DIR"/finance.db.bak-*; do
  case "$_f" in *-wal|*-shm|*-journal) continue;; esac
  baks+=("$_f")
done
shopt -u nullglob
if (( ${#baks[@]} == 0 )); then
  crit "no finance.db.bak-* backup found in $APP_DIR — no local rollback point"
else
  newest="$(ls -1t "$APP_DIR"/finance.db.bak-* 2>/dev/null \
            | grep -vE -- '-(wal|shm|journal)$' | head -1)"
  if have date && have stat; then
    b_epoch="$(stat -c %Y "$newest" 2>/dev/null || echo 0)"
    b_age_h=$(( ($(date +%s) - b_epoch) / 3600 ))
    (( b_age_h > MAX_BACKUP_AGE_H )) \
      && warn "newest backup is ${b_age_h}h old (> ${MAX_BACKUP_AGE_H}h) — no fresh safety net" \
      || ok "newest backup ${b_age_h}h old ($(basename "$newest"))"
  fi
  # Restorability: integrity-check the newest backup (a .bak, never finance.db).
  # python3 first (always present — the app runs on it) opened immutable=1, so
  # SQLite reads the static file directly and NEVER creates -wal/-shm sidecars
  # next to the backup. sqlite3 CLI only as a fallback.
  res=""
  if have python3; then
    res="$(python3 -c '
import sqlite3, sys
try:
    c = sqlite3.connect("file:%s?immutable=1" % sys.argv[1], uri=True)
    print(c.execute("PRAGMA integrity_check").fetchone()[0])
except Exception as e:
    print("unreadable (%s)" % e.__class__.__name__)
' "$newest" 2>/dev/null | head -1)"
  elif have sqlite3; then
    res="$(sqlite3 "$newest" 'PRAGMA integrity_check;' 2>&1 | head -1)"
  fi
  if [[ -z "$res" ]]; then
    warn "cannot integrity-check backups (no sqlite3 or python3)"
  elif [[ "$res" == "ok" ]]; then
    ok "newest backup passes integrity_check (restorable)"
  else
    crit "newest backup FAILED integrity_check ('$res') — backup may be corrupt"
  fi
  # Bloat: too many bak files slowly fills the SD card.
  (( ${#baks[@]} > MAX_BACKUPS )) \
    && warn "${#baks[@]} backup files (> ${MAX_BACKUPS}) — consider pruning the oldest (human action)" \
    || ok "${#baks[@]} backup file(s) retained"
fi

# ── 6. Credentials before they die silently ─────────────────────────────────
# Session signing key: if unset, each gunicorn worker (the app runs --workers 2)
# invents its OWN key, so a cookie signed by one worker is rejected by the other
# — logins fail intermittently and never survive a restart, and app.py only logs
# a warning. See docs/SECURITY-AUDIT-2026-08-04.md finding #1.
if [[ -z "${SECRET_KEY:-}" ]]; then
  warn "SECRET_KEY not set — sessions break across gunicorn workers and are lost on restart; set it in .env"
else
  ok "SECRET_KEY present (stable sessions)"
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  warn "ANTHROPIC_API_KEY not set — Charlee's Ask tab will 503"
else
  ok "ANTHROPIC_API_KEY present"
  if [[ -n "${ASK_KEY_EXPIRES:-}" ]] && have date; then
    exp_epoch="$(date -d "$ASK_KEY_EXPIRES" +%s 2>/dev/null || echo 0)"
    days_left=$(( (exp_epoch - $(date +%s)) / 86400 ))
    if   (( days_left < 0 ));  then crit "ANTHROPIC_API_KEY expired ${days_left#-} day(s) ago — Ask tab is down until rotated"
    elif (( days_left <= 14 )); then warn "ANTHROPIC_API_KEY expires in ${days_left} day(s) — rotate soon (billing carries over)"
    else ok "ANTHROPIC_API_KEY has ${days_left} day(s) left"; fi
  fi
fi

# ── Report ──────────────────────────────────────────────────────────────────
case $WORST in
  0) BADGE="🟢 GREEN";  SUMMARY="all systems healthy";;
  1) BADGE="🟡 AMBER";  SUMMARY="attention: warnings below";;
  2) BADGE="🔴 RED";    SUMMARY="ACTION NEEDED: critical issues below";;
esac
STAMP="$(date '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null || echo now)"
REPORT="Ledger Pi Ops — ${BADGE} — ${SUMMARY}  (${STAMP})
$(printf '%s\n' "${LINES[@]}")"

echo "$REPORT"
# Heartbeat: last-run status, so 'was it even running?' is answerable.
printf '%s\n' "$REPORT" > "$OPS_STATUS_FILE" 2>/dev/null || true

# ── Best-effort bridge to the Chief of Staff (only on amber/red) ─────────────
# Files a GitHub issue via the API with curl (no gh CLI needed) using a
# fine-grained PAT (OPS_ALERT_GH_TOKEN, scoped to the repo with issues:write).
# The daily timer means at most one issue per problem-day; the Friday
# Chief-of-Staff reconciles them.
if (( WORST >= 1 )) && [[ -n "${OPS_ALERT_GH_REPO:-}" && -n "${OPS_ALERT_GH_TOKEN:-}" ]] \
   && have curl && have python3; then
  title="Pi Ops ${BADGE} — $(date '+%Y-%m-%d')"
  payload="$(python3 -c 'import json,sys; print(json.dumps({"title":sys.argv[1],"body":sys.argv[2],"labels":["ops-alert"]}))' "$title" "$REPORT")"
  if curl -fsS -X POST \
        -H "Authorization: Bearer $OPS_ALERT_GH_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        -H "X-GitHub-Api-Version: 2022-11-28" \
        "https://api.github.com/repos/$OPS_ALERT_GH_REPO/issues" \
        -d "$payload" >/dev/null 2>&1; then
    echo "  (filed GitHub issue: $title)"
  else
    echo "  (GitHub issue POST failed — check OPS_ALERT_GH_TOKEN / repo / network)"
  fi
fi

exit $WORST
