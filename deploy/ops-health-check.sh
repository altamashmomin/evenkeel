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
#   OPS_ALERT_GH_REPO   e.g. altamashmomin/evenkeel; enables gh issue on alert
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
bump() { (( $1 > WORST )) && WORST=$1; }
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
  st="$(systemctl show "$SYNC_SERVICE" \
        -p ExecMainStatus -p ActiveExitTimestamp -p Result 2>/dev/null)"
  exit_status="$(sed -n 's/^ExecMainStatus=//p' <<<"$st")"
  last_finish="$(sed -n 's/^ActiveExitTimestamp=//p' <<<"$st")"
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
shopt -s nullglob
baks=( "$APP_DIR"/finance.db.bak-* )
shopt -u nullglob
if (( ${#baks[@]} == 0 )); then
  crit "no finance.db.bak-* backup found in $APP_DIR — no local rollback point"
else
  newest="$(ls -1t "$APP_DIR"/finance.db.bak-* 2>/dev/null | head -1)"
  if have date && have stat; then
    b_epoch="$(stat -c %Y "$newest" 2>/dev/null || echo 0)"
    b_age_h=$(( ($(date +%s) - b_epoch) / 3600 ))
    (( b_age_h > MAX_BACKUP_AGE_H )) \
      && warn "newest backup is ${b_age_h}h old (> ${MAX_BACKUP_AGE_H}h) — no fresh safety net" \
      || ok "newest backup ${b_age_h}h old ($(basename "$newest"))"
  fi
  # Restorability: integrity-check the newest backup (a .bak, never finance.db).
  if have sqlite3; then
    res="$(sqlite3 "$newest" 'PRAGMA integrity_check;' 2>&1 | head -1)"
    [[ "$res" == "ok" ]] \
      && ok "newest backup passes integrity_check (restorable)" \
      || crit "newest backup FAILED integrity_check ('$res') — backup may be corrupt"
  fi
  # Bloat: too many bak files slowly fills the SD card.
  (( ${#baks[@]} > MAX_BACKUPS )) \
    && warn "${#baks[@]} backup files (> ${MAX_BACKUPS}) — consider pruning the oldest (human action)" \
    || ok "${#baks[@]} backup file(s) retained"
fi

# ── 6. Credentials before they die silently ─────────────────────────────────
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
if (( WORST >= 1 )) && [[ -n "${OPS_ALERT_GH_REPO:-}" ]] && have gh; then
  title="Pi Ops ${BADGE} — $(date '+%Y-%m-%d')"
  # One issue per day at most (the timer runs daily); dedup on today's title.
  if ! gh issue list --repo "$OPS_ALERT_GH_REPO" --state open --label ops-alert \
        --search "$title" 2>/dev/null | grep -q .; then
    gh issue create --repo "$OPS_ALERT_GH_REPO" --label ops-alert \
      --title "$title" --body "$REPORT" >/dev/null 2>&1 \
      && echo "  (filed GitHub issue: $title)" \
      || echo "  (gh issue create failed — check gh auth on the Pi)"
  fi
fi

exit $WORST
