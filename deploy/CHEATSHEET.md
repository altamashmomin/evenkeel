# Ledger — operator cheatsheet

The commands that come up running Ledger. **Almost all run on the Pi**, SSH'd in
from `~/pifinance` (where `venv/`, `finance.db`, and the scripts live).

Two things to burn in:
- Use **`venv/bin/python`, never bare `python`** on the Pi — the deps live in the venv.
- Mind **which machine you're on**. `deploy.sh` / `simplefin_sync.py` are Pi-only;
  running them on the Mac hits the wrong database or none at all.

---

## Deploy (the frequent one)
Ship whatever's on `origin/main` — backs up, gates, migrates, restarts:
```bash
deploy/deploy.sh origin/main
```

## Banks & sync
Connect a new bank (one-time, per bank):
```bash
venv/bin/python simplefin_sync.py --claim 'PASTE_SETUP_TOKEN'
```
Pull now during setup (`--force` skips the 30-min budget guard for back-to-back pulls):
```bash
venv/bin/python simplefin_sync.py --force
```
A normal manual pull (respects the guard; the 06:30/18:00 timers do this automatically):
```bash
venv/bin/python simplefin_sync.py
```

## Backups
Manual snapshot right now (WAL-safe):
```bash
venv/bin/python -c "import sqlite3,sys; sqlite3.connect('finance.db').execute('VACUUM INTO ?', [sys.argv[1]])" finance.db.bak-$(date +%Y-%m-%d-%H%M%S)
```
Run tonight's nightly snapshot on demand:
```bash
deploy/nightly-backup.sh
```
List backups, newest first:
```bash
ls -lt finance.db.bak-* finance.db.nightly-* 2>/dev/null
```

## Health & logs
Is the app alive?
```bash
curl -s localhost:8080/api/status
```
Read the guardian's latest report:
```bash
cat ~/pifinance-ops/ops-status.txt
```
Run the guardian check now:
```bash
~/pifinance-ops/ops-health-check.sh
```
Service state + next timer fires:
```bash
systemctl status pifinance ; systemctl list-timers 'pifinance*'
```
Tail logs when something's off (swap the unit: pifinance-sync, ledger-mcp, pifinance-ops):
```bash
journalctl -u pifinance -e
```
Restart the app + MCP after a manual change:
```bash
sudo systemctl restart pifinance ledger-mcp
```

## systemd unit installs (after a deploy that changes a unit)
`deploy.sh` updates the repo copies but NOT the installed units — re-install the
sed-rewritten copy and reload. Example (twice-daily sync timer):
```bash
cd ~/pifinance/deploy && sed 's#/home/pi#/home/altamash#g; s/^User=pi$/User=altamash/' pifinance-sync.timer | sudo tee /etc/systemd/system/pifinance-sync.timer >/dev/null && sudo systemctl daemon-reload && systemctl list-timers pifinance-sync.timer
```

## The dangerous one — full money reset
Don't free-hand it. Follow the runbook (backup → rehearse on a copy → live run):
```bash
cat deploy/reset-money.md
```

---

## On the dev Mac (usually not you)
Full test suite:
```bash
source venv/bin/activate && python -m unittest discover -s tests
```
Check the Pi over Tailscale:
```bash
curl -s http://raspberrypi:8080/api/status
```
