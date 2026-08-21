# Weekly pantry pulse (Pi-side)

A Sunday-morning GitHub issue summarizing the pantry: what's on the list (and
≈ what it costs), what's coming due this week, staples gone quiet (the
curation guard), list rot, one new-staple suggestion. Read-only; it reads the
digest through the app's API with a read token and files the issue over the
same alert bridge the ops guardian uses. It runs ON THE PI because the cloud
routines can't reach it and the pantry lives only there.

## Install (once, on the Pi — needs sudo)

1. Mint a **read-scope** token (log in to the app in a browser, then):
   ```bash
   curl -s -b cookies.txt -c cookies.txt -X POST http://127.0.0.1:8080/api/login \
     -H 'Content-Type: application/json' -d '{"username":"<you>","password":"<pw>"}' >/dev/null
   curl -s -b cookies.txt -X POST http://127.0.0.1:8080/api/tokens \
     -H 'Content-Type: application/json' -d '{"label":"pantry-pulse","scopes":"read"}'
   ```
   Copy the `token` from the response — it is shown exactly once.
2. Add to `~/pifinance/.env` (never commit it) — the bare token, NO angle
   brackets (a literal `<…>` breaks `.env` sourcing and the bearer header):
   ```
   PANTRY_PULSE_TOKEN=the_token_value
   ```
   Safer: pipe the mint response straight into `.env` so the token is never
   displayed or retyped:
   ```bash
   curl -s -b cookies.txt -X POST http://127.0.0.1:8080/api/tokens \
     -H 'Content-Type: application/json' -d '{"label":"pantry-pulse","scopes":"read"}' \
     | venv/bin/python -c 'import sys,json; t=json.load(sys.stdin)["token"]; open(".env","a").write("PANTRY_PULSE_TOKEN=%s\n"%t)'
   ```
   `OPS_ALERT_GH_REPO` / `OPS_ALERT_GH_TOKEN` are already there from the guardian.
3. Install the units, rewriting user/paths as for the other units:
   ```bash
   cd ~/pifinance
   for u in pifinance-pantry-pulse.service pifinance-pantry-pulse.timer; do
     sed 's#/home/pi/pifinance#/home/altamash/pifinance#g; s/^User=pi/User=altamash/' \
       deploy/$u | sudo tee /etc/systemd/system/$u >/dev/null
   done
   sudo systemctl daemon-reload
   sudo systemctl enable --now pifinance-pantry-pulse.timer
   systemctl list-timers pifinance-pantry-pulse.timer
   ```
4. Dry run (prints the markdown, posts nothing):
   ```bash
   set -a; . ~/pifinance/.env; set +a
   ~/pifinance/venv/bin/python ~/pifinance/deploy/pantry_pulse.py --dry-run
   ```

## Tuning
`PANTRY_PULSE_HORIZON_DAYS` (default 7) and `PANTRY_PULSE_STALE_DAYS`
(default 180) in `.env`. Change the day/time in the timer's `OnCalendar`.

## Rotation
The read token doesn't expire but can be revoked (`POST
/api/tokens/<id>/revoke` with a session; `GET /api/tokens` lists ids). If a
token is ever displayed or pasted anywhere, revoke it and mint a fresh one.
The GitHub PAT rotates with the guardian's.
