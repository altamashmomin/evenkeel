#!/usr/bin/env python3
"""SimpleFIN Bridge sync for Pi Finance.

One-time setup:
    python simplefin_sync.py --claim <setup-token>
        Exchanges a SimpleFIN setup token for a permanent access URL and
        saves it to SIMPLEFIN_ACCESS_FILE (default: simplefin_access.url,
        chmod 600). The access URL contains credentials — it is never
        printed and must never be committed anywhere.

Normal run (designed for a daily systemd timer):
    python simplefin_sync.py
        Pulls transactions from /accounts, dedupes on SimpleFIN's
        transaction id so re-runs never double-insert. Money out inserts
        shared 50/50 (source='simplefin') — editable later in the app.
        Money in inserts as an inflow (direction='in'), owned by
        SYNC_PAID_BY unless a matching income rule overrides the owner,
        and is classified on the spot by the rules engine — a match sets
        its income_type immediately, no match lands 'unclassified'
        (docs/INCOME-DESIGN.md).

Environment (.env):
    DATABASE_PATH           path to the app's SQLite file
    SIMPLEFIN_ACCESS_FILE   where the access URL is stored
    SYNC_PAID_BY            user id the imports are attributed to (default 1)
    SYNC_LOOKBACK_DAYS      how far back each run looks (default 10)
"""
import argparse
import base64
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import requests
from dotenv import load_dotenv

import actions
from schema_runtime import connect_existing, require_current_schema

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "finance.db"))
ACCESS_FILE = os.environ.get(
    "SIMPLEFIN_ACCESS_FILE", os.path.join(BASE_DIR, "simplefin_access.url"))
PAID_BY = int(os.environ.get("SYNC_PAID_BY", "1"))
LOOKBACK_DAYS = int(os.environ.get("SYNC_LOOKBACK_DAYS", "10"))
TIMEOUT = 30


def claim(setup_token: str) -> None:
    """Exchange a one-time setup token for a permanent access URL."""
    try:
        claim_url = base64.b64decode(setup_token.strip()).decode("utf-8")
    except Exception:
        sys.exit("error: that doesn't look like a SimpleFIN setup token")
    if not claim_url.startswith("https://"):
        sys.exit("error: decoded claim URL is not https — refusing")
    resp = requests.post(claim_url, timeout=TIMEOUT)
    if resp.status_code != 200:
        sys.exit(f"error: claim failed (HTTP {resp.status_code}). "
                 "Setup tokens are single-use — generate a fresh one if this one was used.")
    access_url = resp.text.strip()
    fd = os.open(ACCESS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(access_url + "\n")
    print(f"ok: access URL saved to {ACCESS_FILE} (permissions 600).")
    print("Keep that file out of git and backups you share. You're set — run "
          "this script with no arguments to sync.")


def read_access_url() -> str:
    try:
        with open(ACCESS_FILE) as f:
            url = f.read().strip()
    except FileNotFoundError:
        sys.exit(f"error: {ACCESS_FILE} not found. Run --claim <setup-token> first "
                 "(get a token at https://beta-bridge.simplefin.org/).")
    if not url:
        sys.exit(f"error: {ACCESS_FILE} is empty. Re-run --claim with a fresh token.")
    return url


def to_cents(amount_str) -> int:
    try:
        return int((Decimal(str(amount_str)) * 100).to_integral_value(rounding="ROUND_HALF_UP"))
    except (InvalidOperation, ValueError):
        raise ValueError(f"unparseable amount: {amount_str!r}")


def sync() -> None:
    require_current_schema(DB_PATH)
    access_url = read_access_url().rstrip("/")
    start = int(time.time()) - LOOKBACK_DAYS * 86400
    try:
        resp = requests.get(f"{access_url}/accounts",
                            params={"start-date": start}, timeout=TIMEOUT)
    except requests.RequestException as e:
        sys.exit(f"error: could not reach SimpleFIN Bridge: {e.__class__.__name__}")
    if resp.status_code == 403:
        sys.exit("error: access denied (HTTP 403). The access URL may have been "
                 "revoked — re-run --claim with a new setup token.")
    if resp.status_code != 200:
        sys.exit(f"error: SimpleFIN returned HTTP {resp.status_code}")
    data = resp.json()

    for err in data.get("errors", []):
        print(f"simplefin notice: {err}")

    db = connect_existing(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 5000")
    member_ids = [r[0] for r in db.execute(
        "SELECT id FROM members WHERE active = 1 ORDER BY id")]
    if PAID_BY not in member_ids:
        sys.exit(f"error: SYNC_PAID_BY={PAID_BY} is not a member in the app database. "
                 "Finish the app's first-run setup, then set SYNC_PAID_BY in .env.")
    if len(member_ids) != 2:
        sys.exit(f"error: {len(member_ids)} active members — the shared-50/50 import "
                 "default needs exactly two. Adjust the sync before changing the household.")

    inserted_out = inserted_in = skipped_dupe = 0
    for account in data.get("accounts", []):
        acct_id = account.get("id", "unknown")
        acct_name = account.get("name", acct_id)
        for txn in account.get("transactions", []):
            cents = to_cents(txn.get("amount", "0"))
            external_id = f"simplefin:{acct_id}:{txn['id']}"
            posted = txn.get("posted") or txn.get("transacted_at") or time.time()
            txn_date = datetime.fromtimestamp(int(posted)).date().isoformat()
            desc = (txn.get("description") or txn.get("payee") or "Bank transaction").strip()
            # Thin caller: record_transaction owns validation, the insert's
            # dedupe on external_id, direction-aware split handling, the
            # income-rules matching pass for an inflow, and the audit row
            # (CORE-DESIGN's record_transaction verb, "dedupe stays inside
            # it"). Money out keeps the legacy shared-50/50 default; money
            # in gets no share fields at all (INCOME-DESIGN invariant 1) —
            # record_transaction enforces that regardless of what's passed
            # here, but is_shared=False is explicit below anyway so the
            # intent reads without following the call into actions.py.
            if cents < 0:                      # money out
                row = actions.record_transaction(
                    db, actor="sync",
                    data={
                        "date": txn_date,
                        "amount": str(Decimal(abs(cents)) / 100),
                        "description": desc,
                        "paid_by": PAID_BY,
                        "is_shared": True,
                        "payer_share_pct": 50,
                    },
                    source="simplefin", external_id=external_id)
                if row is not None:
                    inserted_out += 1
                    print(f"  + {txn_date}  {abs(cents)/100:>9.2f}  {desc[:48]}  [{acct_name}]")
                else:
                    skipped_dupe += 1
            else:                              # money in -- an inflow
                row = actions.record_transaction(
                    db, actor="sync",
                    data={
                        "date": txn_date,
                        "amount": str(Decimal(cents) / 100),
                        "description": desc,
                        "paid_by": PAID_BY,
                        "is_shared": False,
                    },
                    source="simplefin", external_id=external_id, direction="in")
                if row is not None:
                    inserted_in += 1
                    print(f"  + {txn_date}  {cents/100:>9.2f}  {desc[:48]}  [{acct_name}]  "
                          f"(income: {row['income_type']})")
                else:
                    skipped_dupe += 1
    db.close()
    print(f"done: {inserted_out} outflows inserted, {inserted_in} inflows inserted, "
          f"{skipped_dupe} already present.")


def main():
    ap = argparse.ArgumentParser(description="SimpleFIN sync for Pi Finance")
    ap.add_argument("--claim", metavar="SETUP_TOKEN",
                    help="one-time: exchange a setup token for a permanent access URL")
    args = ap.parse_args()
    if args.claim:
        claim(args.claim)
    else:
        sync()


if __name__ == "__main__":
    main()
