# Pi-side Ops guardian — install & operation

The cloud health-sweep checks the *code*; it structurally cannot see the *Pi*. This
closes that blind spot: a deterministic daily check that runs **on the Pi** and
catches the silent production failures — service down, disk filling, sync gone
stale, a backup that won't restore, a credential about to expire.

- `deploy/ops-health-check.sh` — the guardian (plain shell; no Python, no API key,
  so it works even when everything else is broken). Read-only: it reports and exits,
  it never restarts a service, prunes a backup, or deploys.
- `deploy/pifinance-ops.service` / `deploy/pifinance-ops.timer` — run it daily at
  07:00 (≈30 min after the 06:30 SimpleFIN sync, so it can confirm that sync ran).

## What it checks

| Check | Amber | Red |
|---|---|---|
| `pifinance` web unit + `GET /api/status` | probe unavailable | unit down, or serving non-200 (wedged) |
| `ledger-mcp` sibling | not active (app unaffected) | — |
| Disk usage of the install fs | ≥ `DISK_WARN_PCT` (80) | ≥ `DISK_CRIT_PCT` (90) |
| SimpleFIN sync freshness (from systemd) | last good run > `MAX_SYNC_AGE_H` (26h) | last run FAILED |
| Newest `finance.db.bak-*` age | > `MAX_BACKUP_AGE_H` (7d) | — |
| Newest backup `PRAGMA integrity_check` | — | not `ok` (corrupt) |
| Backup file count | > `MAX_BACKUPS` (12) | — |
| Backup present at all | — | none found |
| `ANTHROPIC_API_KEY` | missing, or ≤14d to `ASK_KEY_EXPIRES` | expired |

Exit code = worst severity (0 green / 1 amber / 2 red). Output goes to the journal
and to `ops-status.txt` (a heartbeat you can `cat` to see the last verdict). It
**never opens `finance.db`** — sync freshness is read from systemd, and only
`*.bak-*` files are ever opened, read-only (CORE-DESIGN invariant 6).

## Install (on the Pi)

The tracked unit files carry the `pi` / `/home/pi` placeholder, like every other
unit here — rewrite to the real user/path on copy; never edit the tracked files (a
future `git pull` would conflict).

```bash
cd /home/altamash/pifinance
git pull                                  # pull in deploy/ops-*

chmod +x deploy/ops-health-check.sh

for u in pifinance-ops.service pifinance-ops.timer; do
  sed -e 's#/home/pi/#/home/altamash/#g' -e 's/^User=pi$/User=altamash/' \
      "deploy/$u" | sudo tee "/etc/systemd/system/$u" >/dev/null
done

sudo systemctl daemon-reload
sudo systemctl enable --now pifinance-ops.timer

# Prove it: run once now and read the verdict.
sudo systemctl start pifinance-ops.service
systemctl status pifinance-ops.service --no-pager
cat ops-status.txt
systemctl list-timers pifinance-ops.timer --no-pager
```

## Optional `.env` additions

The unit loads the app's `.env`, so overrides go there (values, not shell exports):

```
# Remind before the key dies (billing carries over on rotation):
ASK_KEY_EXPIRES=2026-08-30
# Bridge alerts to the cloud Chief of Staff by filing a GitHub issue on amber/red.
# Requires `gh` installed AND authed on the Pi (gh auth login). Leave unset to keep
# alerts local (journal + ops-status.txt only).
OPS_ALERT_GH_REPO=altamashmomin/evenkeel
# Threshold overrides (defaults shown):
# DISK_WARN_PCT=80  DISK_CRIT_PCT=90  MAX_SYNC_AGE_H=26  MAX_BACKUP_AGE_H=168
# MAX_BACKUPS=12
```

## How alerts surface (and reach the Chief of Staff)

1. **Always:** the journal (`journalctl -u pifinance-ops.service`) and `ops-status.txt`.
2. **On amber/red, optionally:** a GitHub issue labelled `ops-alert` on
   `OPS_ALERT_GH_REPO` (one per day max) — this is the bridge the **cloud Chief of
   Staff** reads, since it can't reach the tailnet. Needs `gh` authed on the Pi.
   Without `gh`, run the **`ledger-ops`** agent from a machine on the tailnet to
   investigate an alert live.

## What it deliberately does NOT do

No restarts, no backup pruning, no key rotation, no deploys — every fix stays a
human action (that's the whole point of a red alert). For diagnosis and exact
fix-commands when an alert fires, invoke the **`ledger-ops`** agent (it reaches the
Pi over Tailscale, read-only, and hands you the commands to run yourself).
