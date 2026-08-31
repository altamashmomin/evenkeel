# Daily change digest (Pi-side)

A once-a-day GitHub issue summarizing **what you and the assistants changed** —
counts by person and by kind (tagged a deposit, set a budget, added a bill…),
plus anything still **awaiting your approval** (the F-1 Pending-approvals queue).
Read-only: it reads the digest through the app's API with a read token and files
the issue over the same alert bridge the ops guardian + pantry pulse use. Runs
ON THE PI because the cloud can't reach it and the audit trail lives only there
(NOTIFICATIONS-DESIGN inc 2).

**Terse by design:** the issue carries counts, kinds, and who — **never amounts,
descriptions, or balances.** The specifics stay behind the tailnet + login; the
issue just says "go look in the app."

## Privacy — the alert repo MUST be private

The digest names who changed what. `OPS_ALERT_GH_REPO` must point at a **private**
repo (as it already should for the ops guardian). Never point it at the public
`evenkeel` repo.

## Install (once, on the Pi — needs sudo)

1. **Read token.** The digest job reuses `PANTRY_PULSE_TOKEN` if you already run
   the pantry pulse — nothing to do. Otherwise mint a **read-scope** token (log
   in to the app in a browser, then):
   ```bash
   cd ~/pifinance
   curl -s -b cookies.txt -c cookies.txt -X POST http://127.0.0.1:8080/api/login \
     -H 'Content-Type: application/json' -d '{"username":"<you>","password":"<pw>"}' >/dev/null
   curl -s -b cookies.txt -X POST http://127.0.0.1:8080/api/tokens \
     -H 'Content-Type: application/json' -d '{"label":"change-digest","scopes":"read"}' \
     | venv/bin/python -c 'import sys,json; t=json.load(sys.stdin)["token"]; open(".env","a").write("CHANGE_DIGEST_TOKEN=%s\n"%t)'
   ```
   (Piping straight into `.env` so the token is never displayed. `OPS_ALERT_GH_REPO`
   / `OPS_ALERT_GH_TOKEN` are already there from the guardian.)

2. **Install the units**, rewriting user/paths as for the other units:
   ```bash
   cd ~/pifinance
   for u in pifinance-change-digest.service pifinance-change-digest.timer; do
     sed 's#/home/pi/pifinance#/home/altamash/pifinance#g; s/^User=pi/User=altamash/' \
       deploy/$u | sudo tee /etc/systemd/system/$u >/dev/null
   done
   sudo systemctl daemon-reload
   sudo systemctl enable --now pifinance-change-digest.timer
   systemctl list-timers pifinance-change-digest.timer
   ```

3. **Dry run** (prints the markdown, posts nothing):
   ```bash
   set -a; . ~/pifinance/.env; set +a
   ~/pifinance/venv/bin/python ~/pifinance/deploy/change_digest.py --dry-run
   ```

## How the window works

A high-water-mark file (`CHANGE_DIGEST_STATE`, default `~/pifinance/.change-digest.state`,
gitignored) records the last digest time. Each run covers **since the last
digest** — advanced after a successful post (or on a quiet day), left alone on a
post failure so the next run retries the window. First run with no state covers
the last `CHANGE_DIGEST_LOOKBACK_HOURS` (default 24). Force a window with
`--since <ISO>` (that run never touches the state file).

## Tuning
`CHANGE_DIGEST_LOOKBACK_HOURS` (default 24) in `.env`; change the day/time in the
timer's `OnCalendar`. A quiet day (no human/assistant writes, nothing awaiting
approval) posts nothing.

## Rotation
The read token doesn't expire but can be revoked (`POST /api/tokens/<id>/revoke`
with a session; `GET /api/tokens` lists ids). The GitHub PAT rotates with the
guardian's.

---

## Also: approval alerts (a second, faster timer)

The **daily** digest above is for review. Its sibling, `notify_approvals.py`,
runs **every 15 minutes** and files a terse issue the moment an automation
proposes something awaiting your approval — so you can act before it expires
(proposals now live ~24h, up from 10 min, precisely so a notification has time to
land). Same read token, same `OPS_ALERT_GH_*` private repo; it remembers which
proposals it has announced in a gitignored `.notify-approvals.state`, so nothing
is announced twice. Terse by the same rule — the *kind* (a new rule / a backlog
sweep), who proposed it, and when it expires; the specifics and the Approve
button live in the app.

Install the second pair of units the same way:
```bash
cd ~/pifinance
for u in pifinance-notify-approvals.service pifinance-notify-approvals.timer; do
  sed 's#/home/pi/pifinance#/home/altamash/pifinance#g; s/^User=pi/User=altamash/' \
    deploy/$u | sudo tee /etc/systemd/system/$u >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now pifinance-notify-approvals.timer
systemctl list-timers pifinance-notify-approvals.timer
```
Dry run (prints, posts nothing):
```bash
set -a; . ~/pifinance/.env; set +a
~/pifinance/venv/bin/python ~/pifinance/deploy/notify_approvals.py --dry-run
```
