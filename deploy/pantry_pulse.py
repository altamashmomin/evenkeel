#!/usr/bin/env python3
"""Weekly pantry pulse — the Pi-side digest job (Pantry v2 inc 6).

Why Pi-side: the cloud routines can't reach the Pi, and the pantry lives only
there. So this runs from a systemd timer on the Pi (like the ops guardian and
the nightly backup), reads the digest through the app's OWN API with a
read-scope bearer token (never the database directly — hard rule 6), and
files it as a GitHub issue over the existing alert bridge (the same
OPS_ALERT_GH_* PAT the guardian uses; issues:write only). Nothing is written
to Ledger; the job is a reader.

Env (from the Pi's .env via the unit's EnvironmentFile):
  PANTRY_PULSE_TOKEN     a `read` bearer token (mint: log in → POST /api/tokens)
  LEDGER_URL             default http://127.0.0.1:8080
  OPS_ALERT_GH_REPO      e.g. altamashmomin/evenkeel
  OPS_ALERT_GH_TOKEN     fine-grained PAT, issues:write on that repo
  PANTRY_PULSE_HORIZON_DAYS  default 7   ("coming due" window)
  PANTRY_PULSE_STALE_DAYS    default 180 (curation-guard grace)

The derivation is clock-free; THIS job is the consumer that applies today's
date for the horizon and the grace — the same split the web view uses.
`--dry-run` prints the markdown instead of posting. A quiet week (nothing on
the list, nothing due, nothing stale, no suggestion) posts nothing.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date


def fetch_pulse(base_url, token, timeout=20):
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/inventory/pulse",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def render_markdown(pulse, today, horizon_days=7, stale_days=180):
    """(title, body, quiet) — pure. `today` is 'YYYY-MM-DD'. Applies the
    horizon to due_soon and the grace to stale_staples; skips snoozed rows."""
    lines = []
    live = [l for l in pulse.get("list", [])
            if not (l.get("snoozed_until") and l["snoozed_until"] > today)]
    due = [d for d in pulse.get("due_soon", [])
           if not (d.get("snoozed_until") and d["snoozed_until"] > today)
           and _days(today, d["predicted_date"]) <= horizon_days]
    stale = [s for s in pulse.get("stale_staples", [])
             if _days(s["last_activity"], today) >= stale_days]
    rot = pulse.get("stale_shopping_items", [])
    suggestion = pulse.get("new_staple_suggestion")
    quiet = not (live or due or stale or suggestion)

    total = (pulse.get("list_total") or {}).get("display")
    if live:
        cover = ""
        if pulse.get("unpriced_count"):
            cover = f" ({pulse['priced_count']} of {len(pulse['list'])} priced)"
        lines.append(f"## On the list — {len(live)}"
                     + (f" · ≈ {total}{cover}" if total and pulse.get("priced_count") else ""))
        for l in live:
            bits = [l.get("status") if l.get("kind") != "oneoff" else "need"]
            if l.get("store"):
                bits.append(f"@ {l['store']}")
            if l.get("typical"):
                bits.append(f"~{l['typical']['display']}")
            lines.append(f"- **{l['name']}** — " + " · ".join(b for b in bits if b))
        lines.append("")
    if due:
        lines.append(f"## Coming due within {horizon_days} days — {len(due)}")
        for d in due:
            n = _days(today, d["predicted_date"])
            when = "overdue" if n < 0 else "today" if n == 0 else f"in {n} day{'s' if n != 1 else ''}"
            bits = [when, f"@ {d['store']}" if d.get("store") else "",
                    f"~{d['typical']['display']}" if d.get("typical") else "",
                    f"({d.get('interval_source')})"]
            lines.append(f"- **{d['name']}** — " + " · ".join(b for b in bits if b))
        lines.append("")
    if stale:
        lines.append(f"## Still tracking these? — {len(stale)} quiet for {stale_days}+ days")
        for s in stale:
            lines.append(f"- **{s['name']}** — no sign of life since {s['last_activity']}")
        lines.append("_Stocked for months with no restock seen. Leaving one is fine (salt lasts)._")
        lines.append("")
    if rot:
        lines.append(f"## On the list a while — {len(rot)}")
        for r in rot:
            lines.append(f"- **{r['name']}** — {r.get('status')} since {r.get('low_since')} — still need it?")
        lines.append("")
    if suggestion:
        lines.append("## Track it?")
        lines.append(f"- **{suggestion['merchant']}** — bought {suggestion['purchases_seen']}× "
                     f"(last {suggestion['last_purchase']}, {suggestion['total_spent']['display']} total)")
        lines.append("")
    if pulse.get("unmatched_count"):
        lines.append(f"_{pulse['unmatched_count']} staple(s) have never matched a purchase — "
                     "worth a look at their match phrase in the app._")
        lines.append("")
    lines.append(f"_Pantry pulse for the week of {today} · read-only · "
                 "act on it in the app or by asking the assistant._")
    title = f"Pantry pulse — {today}: {len(live)} on the list, {len(due)} coming due"
    return title, "\n".join(lines), quiet


def post_issue(repo, token, title, body, labels=("pantry-pulse",), timeout=20):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=json.dumps({"title": title, "body": body, "labels": list(labels)}).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "ledger-pantry-pulse"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r).get("html_url")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Weekly pantry pulse (Pi-side)")
    ap.add_argument("--dry-run", action="store_true", help="print, don't post")
    ap.add_argument("--today", default=date.today().isoformat())
    args = ap.parse_args(argv)
    token = os.environ.get("PANTRY_PULSE_TOKEN", "").strip()
    if not token:
        print("pantry-pulse: PANTRY_PULSE_TOKEN not set (a read-scope bearer token)", file=sys.stderr)
        return 2
    base = os.environ.get("LEDGER_URL", "http://127.0.0.1:8080")
    horizon = int(os.environ.get("PANTRY_PULSE_HORIZON_DAYS", "7"))
    grace = int(os.environ.get("PANTRY_PULSE_STALE_DAYS", "180"))
    try:
        pulse = fetch_pulse(base, token)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"pantry-pulse: could not read {base}/api/inventory/pulse: {e}", file=sys.stderr)
        return 1
    title, body, quiet = render_markdown(pulse, args.today, horizon, grace)
    if quiet:
        print("pantry-pulse: quiet week — nothing to say, nothing posted")
        return 0
    if args.dry_run:
        print(title); print(); print(body)
        return 0
    repo, gh = os.environ.get("OPS_ALERT_GH_REPO", ""), os.environ.get("OPS_ALERT_GH_TOKEN", "")
    if not (repo and gh):
        print("pantry-pulse: OPS_ALERT_GH_REPO / OPS_ALERT_GH_TOKEN not set — printing instead")
        print(title); print(); print(body)
        return 0
    try:
        url = post_issue(repo, gh, title, body)
    except (urllib.error.URLError, OSError) as e:
        print(f"pantry-pulse: issue POST failed: {e}", file=sys.stderr)
        return 1
    print(f"pantry-pulse: filed {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
