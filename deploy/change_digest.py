#!/usr/bin/env python3
"""Daily change digest — the Pi-side "what the assistants changed" job
(NOTIFICATIONS-DESIGN increment 2).

Why Pi-side: same as the pantry pulse — the cloud can't reach the Pi, and the
audit trail lives only there. Runs from a systemd timer, reads the digest
through the app's OWN API with a read-scope bearer (never the DB directly — hard
rule 6), and files it as a GitHub issue over the existing OPS_ALERT_GH_* bridge
(the same PAT the guardian + pantry pulse use). Reads only; writes nothing.

TERSE by rule (NOTIFICATIONS-DESIGN privacy): kinds, counts, and who — never
amounts, descriptions, or balances. The specifics stay behind the tailnet +
login; the issue is a pointer ("go look in the app"), not a statement. So
OPS_ALERT_GH_REPO must be PRIVATE — it names who changed what.

A high-water-mark file remembers the last digest time, so each run covers
"since the last digest" with no gap and no overlap (advanced after a successful
post, or on a quiet day; left alone on a post failure so the next run retries
the window).

Env (Pi .env via the unit's EnvironmentFile):
  CHANGE_DIGEST_TOKEN   a `read` bearer (mint: log in -> POST /api/tokens); falls
                        back to PANTRY_PULSE_TOKEN (also read-scope) if unset.
  LEDGER_URL            default http://127.0.0.1:8080
  OPS_ALERT_GH_REPO     e.g. altamashmomin/ledger-alerts (PRIVATE)
  OPS_ALERT_GH_TOKEN    fine-grained PAT, issues:write on that repo
  CHANGE_DIGEST_STATE   high-water-mark file; default <repo>/.change-digest.state
  CHANGE_DIGEST_LOOKBACK_HOURS  first-run / no-state window; default 24

`--dry-run` prints instead of posting. A quiet day (no human/assistant writes
and nothing awaiting approval) posts nothing.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DEFAULT = Path(__file__).resolve().parent.parent / ".change-digest.state"

# Friendly phrasing for the raw verb names in by_action. Unknown verbs fall back
# to the name with underscores spaced out.
_ACTION_LABEL = {
    "classify_inflow": "tagged a deposit",
    "recategorize_transaction": "recategorized a transaction",
    "create_income_rule": "created a rule",
    "confirm_action": "approved a proposal",
    "apply_rules": "swept the backlog",
    "merge_category": "merged a category",
    "set_transfer": "marked a transfer",
    "set_rule_enabled": "enabled/disabled a rule",
    "set_budget": "set a budget",
    "create_bill": "added a bill",
    "update_bill": "edited a bill",
    "delete_bill": "removed a bill",
    "mark_bill_paid": "marked a bill paid",
    "unmark_bill_paid": "un-marked a bill",
    "create_goal": "added a goal",
    "delete_goal": "removed a goal",
    "add_item": "added a pantry item",
    "archive_item": "removed a pantry item",
    "restock_items": "restocked items",
    "set_item_status": "changed a pantry status",
    "settle_up": "recorded a settle-up",
    "delete_transaction": "deleted a transaction",
    "change_password": "changed a password",
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_since(state_path, lookback_hours):
    """The window start: the last-recorded digest time, or now-lookback on the
    first run (no state file yet)."""
    try:
        s = Path(state_path).read_text(encoding="utf-8").strip()
        if s:
            return s
    except OSError:
        pass
    return (datetime.now(timezone.utc)
            - timedelta(hours=lookback_hours)).isoformat(timespec="seconds")


def write_since(state_path, now):
    try:
        Path(state_path).write_text(now + "\n", encoding="utf-8")
    except OSError as e:
        print(f"change-digest: could not write state {state_path}: {e}", file=sys.stderr)


def fetch_digest(base_url, token, since, timeout=20):
    url = (base_url.rstrip("/") + "/api/activity/digest?since="
           + urllib.parse.quote(since))
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _label(action):
    return _ACTION_LABEL.get(action, action.replace("_", " "))


def render_markdown(digest):
    """(title, body, quiet) — pure and TERSE: counts, kinds, and who, never a
    single amount. `sync` (the routine bank feed) is a one-line footnote, not a
    section — the digest is about what people and the assistants changed."""
    human = digest.get("assistant_and_human_writes", 0)
    pending = digest.get("pending_approvals", 0)
    sync = digest.get("sync_writes", 0)
    quiet = human == 0 and pending == 0
    lines = []
    if pending:
        s = "s" if pending != 1 else ""
        lines.append(f"## {pending} proposal{s} awaiting your approval")
        lines.append("Open **Ledger -> Home -> Pending approvals** to approve or dismiss.")
        lines.append("")
    if human:
        s = "s" if human != 1 else ""
        lines.append(f"## {human} change{s} by you & the assistants")
        for a in digest.get("by_actor", []):
            if a["actor"] == "sync":
                continue
            n = a["count"]
            lines.append(f"- **{a['actor']}** — {n} change{'s' if n != 1 else ''}")
        kinds = [k for k in digest.get("by_action", [])
                 if k["action"] != "record_transaction"]  # the sync feed's verb
        if kinds:
            lines.append("")
            lines.append("What kind:")
            for k in kinds:
                lines.append(f"- {_label(k['action'])} × {k['count']}")
        lines.append("")
    if sync:
        s = "s" if sync != 1 else ""
        lines.append(f"_Plus {sync} routine bank-feed update{s} (normal sync)._")
        lines.append("")
    lines.append("_Counts only — open the app for the details; nothing here can "
                 "be acted on directly._")
    since = (digest.get("since") or "")[:16].replace("T", " ")
    title = (f"Ledger activity — {human} change{'s' if human != 1 else ''}"
             + (f", {pending} awaiting approval" if pending else "")
             + (f" (since {since})" if since else ""))
    return title, "\n".join(lines), quiet


def post_issue(repo, token, title, body, labels=("change-digest",), timeout=20):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=json.dumps({"title": title, "body": body,
                         "labels": list(labels)}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "ledger-change-digest"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r).get("html_url")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Daily change digest (Pi-side)")
    ap.add_argument("--dry-run", action="store_true", help="print, don't post")
    ap.add_argument("--since", help="override the window start (ISO-8601)")
    args = ap.parse_args(argv)

    token = (os.environ.get("CHANGE_DIGEST_TOKEN", "").strip()
             or os.environ.get("PANTRY_PULSE_TOKEN", "").strip())
    if not token:
        print("change-digest: no read token (set CHANGE_DIGEST_TOKEN or "
              "PANTRY_PULSE_TOKEN)", file=sys.stderr)
        return 2
    base = os.environ.get("LEDGER_URL", "http://127.0.0.1:8080")
    state = os.environ.get("CHANGE_DIGEST_STATE", str(STATE_DEFAULT))
    lookback = int(os.environ.get("CHANGE_DIGEST_LOOKBACK_HOURS", "24"))

    since = args.since or read_since(state, lookback)
    now = _now_iso()
    try:
        digest = fetch_digest(base, token, since)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"change-digest: could not read {base}/api/activity/digest: {e}",
              file=sys.stderr)
        return 1

    title, body, quiet = render_markdown(digest)
    if quiet:
        print("change-digest: quiet — nothing to report, nothing posted")
        if not args.dry_run and not args.since:
            write_since(state, now)   # advance so the window stays bounded
        return 0
    if args.dry_run:
        print(title); print(); print(body)
        return 0

    repo = os.environ.get("OPS_ALERT_GH_REPO", "")
    gh = os.environ.get("OPS_ALERT_GH_TOKEN", "")
    if not (repo and gh):
        print("change-digest: OPS_ALERT_GH_REPO / OPS_ALERT_GH_TOKEN not set — "
              "printing instead")
        print(title); print(); print(body)
        return 0
    try:
        url = post_issue(repo, gh, title, body)
    except (urllib.error.URLError, OSError) as e:
        print(f"change-digest: issue POST failed: {e}", file=sys.stderr)
        return 1   # do NOT advance state — next run retries this window
    print(f"change-digest: filed {url}")
    if not args.since:
        write_since(state, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
