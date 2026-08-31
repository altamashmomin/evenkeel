#!/usr/bin/env python3
"""Approval alerts — the Pi-side near-real-time job (NOTIFICATIONS-DESIGN inc 3).

When an automation PROPOSES a two-phase action, F-1 refuses to let it confirm its
own proposal — a human must approve it in the app. This job, on a short timer,
notices a new proposal and files a terse GitHub issue so you know to look before
it expires. Reads /api/actions/pending through the app's API with a read bearer;
posts over the same OPS_ALERT_GH_* bridge the guardian uses. Reads only.

TERSE by rule (NOTIFICATIONS-DESIGN privacy): the KIND (a new rule / a backlog
sweep), who proposed it, and when it expires — NOT the rule's match phrase or any
amount. The specifics (and the Approve button) live in the app, behind the
tailnet + login. So OPS_ALERT_GH_REPO must be PRIVATE.

Already-announced proposals are remembered in a gitignored state file (a JSON
list of tokens), pruned each run to those still pending — so nothing is
re-announced and the file can't grow unbounded.

Env (Pi .env via the unit's EnvironmentFile):
  CHANGE_DIGEST_TOKEN / PANTRY_PULSE_TOKEN   a `read` bearer (either works)
  LEDGER_URL                default http://127.0.0.1:8080
  OPS_ALERT_GH_REPO         PRIVATE repo, e.g. altamashmomin/ledger-alerts
  OPS_ALERT_GH_TOKEN        fine-grained PAT, issues:write on that repo
  NOTIFY_APPROVALS_STATE    default <repo>/.notify-approvals.state

`--dry-run` prints instead of posting. Nothing pending (or nothing new) posts
nothing.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

STATE_DEFAULT = Path(__file__).resolve().parent.parent / ".notify-approvals.state"

_KIND = {"create_rule": "a new auto-tagging rule",
         "apply_rules": "a backlog sweep (apply all rules)"}


def fetch_pending(base_url, token, timeout=20):
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/actions/pending",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def read_announced(state_path):
    try:
        return set(json.loads(Path(state_path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def write_announced(state_path, tokens):
    try:
        Path(state_path).write_text(json.dumps(sorted(tokens)), encoding="utf-8")
    except OSError as e:
        print(f"notify-approvals: could not write state {state_path}: {e}",
              file=sys.stderr)


def _when(expires_at):
    try:                                            # local time, portable format
        return datetime.fromisoformat(expires_at).astimezone().strftime("%a %H:%M")
    except (ValueError, TypeError):
        return expires_at or "soon"


def render_issue(new_pending):
    """(title, body) for a batch of newly-seen proposals — TERSE: kind, who, and
    expiry, never the match phrase or an amount."""
    n = len(new_pending)
    s = "s" if n != 1 else ""
    lines = [f"**{n} proposal{s} awaiting your approval.** Open "
             "**Ledger -> Home -> Pending approvals** to approve or dismiss.", ""]
    for p in new_pending:
        kind = _KIND.get(p.get("action_type"), "a change")
        proposer = p.get("proposed_by", "")
        via = "in the app" if proposer == "in the app" else f"by {proposer}"
        lines.append(f"- {kind} — proposed {via}, expires {_when(p.get('expires_at'))}")
    lines.append("")
    this = "these" if n != 1 else "this"
    lines.append(f"_An automation proposed {this}; only you, signed in, can "
                 "approve. Nothing changes until you do._")
    return f"Ledger: {n} proposal{s} awaiting your approval", "\n".join(lines)


def post_issue(repo, token, title, body, labels=("approval-pending",), timeout=20):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=json.dumps({"title": title, "body": body,
                         "labels": list(labels)}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "ledger-notify-approvals"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r).get("html_url")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Approval alerts (Pi-side)")
    ap.add_argument("--dry-run", action="store_true", help="print, don't post")
    args = ap.parse_args(argv)

    token = (os.environ.get("CHANGE_DIGEST_TOKEN", "").strip()
             or os.environ.get("PANTRY_PULSE_TOKEN", "").strip())
    if not token:
        print("notify-approvals: no read token (set CHANGE_DIGEST_TOKEN or "
              "PANTRY_PULSE_TOKEN)", file=sys.stderr)
        return 2
    base = os.environ.get("LEDGER_URL", "http://127.0.0.1:8080")
    state = os.environ.get("NOTIFY_APPROVALS_STATE", str(STATE_DEFAULT))

    try:
        pending = fetch_pending(base, token)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"notify-approvals: could not read {base}/api/actions/pending: {e}",
              file=sys.stderr)
        return 1

    current = {p["token"] for p in pending}
    announced = read_announced(state)
    new = [p for p in pending if p["token"] not in announced]
    if not new:
        print("notify-approvals: nothing new")
        if not args.dry_run:
            write_announced(state, current)   # prune tokens no longer pending
        return 0

    title, body = render_issue(new)
    if args.dry_run:
        print(title); print(); print(body)
        return 0
    repo = os.environ.get("OPS_ALERT_GH_REPO", "")
    gh = os.environ.get("OPS_ALERT_GH_TOKEN", "")
    if not (repo and gh):
        print("notify-approvals: OPS_ALERT_GH_REPO / OPS_ALERT_GH_TOKEN not set — "
              "printing instead")
        print(title); print(); print(body)
        return 0
    try:
        url = post_issue(repo, gh, title, body)
    except (urllib.error.URLError, OSError) as e:
        print(f"notify-approvals: issue POST failed: {e}", file=sys.stderr)
        return 1   # leave state — next run retries these
    print(f"notify-approvals: filed {url}")
    write_announced(state, current)
    return 0


if __name__ == "__main__":
    sys.exit(main())
