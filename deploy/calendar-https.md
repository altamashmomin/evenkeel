# Calendar feed over HTTPS (Pi-side, Tailscale Serve)

Why: Apple Calendar rewrites `webcal://` to `https://` before it fetches, so
the "Add to my calendar" link can never subscribe while it points at the
plain-HTTP `:8080` origin (and a single-label MagicDNS host like
`raspberrypi` is flaky in iOS's system resolver anyway). The fix is a real
HTTPS front door: **Tailscale Serve** terminates TLS on 443 with a genuine
Let's Encrypt certificate for the Pi's `*.ts.net` name and proxies to the
app — still tailnet-only. The app then pins its calendar links to that door
via `PUBLIC_BASE_URL`.

**Never use `tailscale funnel` for this** — funnel is public internet
exposure, and the feed carries household finance data behind a token-in-URL.
Serve stays inside the tailnet; funnel does not.

## Install (once, on the Pi — needs sudo)

1. In the tailnet admin console (login.tailscale.com → DNS), make sure
   **HTTPS Certificates** is enabled (MagicDNS already is).

2. Find the Pi's full ts.net name:
   ```bash
   tailscale status --json | python3 -c 'import sys,json; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))'
   ```
   Expect something like `raspberrypi.tail1234.ts.net`.

3. Start the HTTPS proxy (persists across reboots; first run may pause a few
   seconds while the certificate is provisioned):
   ```bash
   sudo tailscale serve --bg 8080
   ```
   Verify:
   ```bash
   tailscale serve status
   curl -sI https://<pi-dns-name>/api/ops/health | head -1
   ```
   (Any app route works for the check; a `401`/`200` means the proxy is up.)

4. Add to `~/pifinance/.env` (substitute the real name from step 2):
   ```
   PUBLIC_BASE_URL=https://<pi-dns-name>
   ```

5. Restart the app so it reads the new knob:
   ```bash
   sudo systemctl restart pifinance
   ```

6. Verify end to end: open the app → Help sheet (`?`) → "Get my calendar
   link" — the button's link must now read
   `webcal://<pi-dns-name>/calendar/….ics`. Tap it on an iPhone (Tailscale
   connected): Calendar's subscribe dialog should verify and add "Ledger".

## Notes

- The phone refreshes the feed only while its Tailscale is connected;
  a refresh attempt while disconnected just fails quietly and retries
  later. Events already subscribed stay visible regardless.
- Rotating `SECRET_KEY` still revokes every feed link at once (the token is
  derived, never stored); `PUBLIC_BASE_URL` only changes where links point.
- The old `:8080` origin keeps working for the app itself — Serve is an
  additional front door, not a move.
