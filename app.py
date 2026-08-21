"""Pi Finance — a household finance app.

Flask + SQLite, no build step. See README.md for setup.
"""
import functools
import os
import secrets
import sqlite3
import time
from datetime import date, datetime

from dotenv import load_dotenv
from flask import Flask, g, jsonify, redirect, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

import actions
import agent_catalog
import ask_loop
import ontology
from actions import (active_members, current_period, payer_share_pct,
                     prefetch_payer_shares, to_cents)
from derivations import (anomaly_flags, bill_variance, budget_status,
                         cash_flow_forecast, category_trend,
                         compute_balance as derive_balance, goal_pace,
                         settle_breakdown,
                         income_summary, last_shopping_trip,
                         income_trend, low_stock, member_breakdown,
                         new_staple_suggestions,
                         recurring_charges, restock_forecast, restock_suggestions,
                         savings_rate_trend, shopping_list,
                         spending_summary, stale_shopping_items, staple_spend, list_estimate, trip_plan, trip_closure,
                         top_merchants, unmatched_staples)
from schema_runtime import connect_existing, require_current_schema

load_dotenv()

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "finance.db"))
require_current_schema(DB_PATH)

app = Flask(__name__, static_folder="static", static_url_path="")

_secret = os.environ.get("SECRET_KEY")
if not _secret:
    _secret = secrets.token_hex(32)
    app.logger.warning(
        "SECRET_KEY not set in .env — using a temporary key. "
        "Sessions will not survive a restart. Set SECRET_KEY in .env."
    )
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 90,  # 90 days
    MAX_CONTENT_LENGTH=64 * 1024,
)

# Security response headers (CODE-REVIEW-2026-08-07 #13). All scripts/styles are
# same-origin files and there are no inline <script> blocks, so a strict
# script-src 'self' holds — and would have neutralised the P0 XSS payload.
# style-src needs 'unsafe-inline' for the inline style="width:…" bars render.js
# emits; script-src does NOT, which is the load-bearing directive.
_CSP = ("default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")

# A fixed hash for the login timing-equalizer (#17): comparing an attempted
# password against this when the username is unknown costs the same as a real
# check, so response time doesn't reveal whether a username exists.
_DUMMY_PW_HASH = generate_password_hash("timing-equalizer-not-a-real-password")

# In-process fixed-window rate limiter (CODE-REVIEW-2026-08-07 #17): guards
# /api/login against brute force and /api/ask against runaway cost. Keyed by
# username / user_id. NOTE: with gunicorn --workers 4 the buckets are per
# worker, so the real ceiling is ~4x the numbers below — deliberately accepted
# for a two-person home app on a tailnet (a DB-backed counter would be exact but
# needs a migration). It still stops brute force and bounds API spend.
LOGIN_MAX_FAILS, LOGIN_WINDOW_S = 10, 900       # 10 failed logins / 15 min
ASK_MAX, ASK_WINDOW_S = 30, 3600                # 30 assistant queries / hour
_RATE_BUCKETS = {}  # key -> (window_start_epoch, count)


def _rate_over(key, limit, window_s):
    """True if `key` has already reached `limit` hits in the current window.
    Read-only — does not count this call."""
    start, count = _RATE_BUCKETS.get(key, (0, 0))
    if time.time() - start >= window_s:
        return False  # window expired → not over
    return count >= limit


def _rate_hit(key, window_s):
    """Record one hit against `key`, rolling the window if it has expired."""
    now = time.time()
    start, count = _RATE_BUCKETS.get(key, (now, 0))
    if now - start >= window_s:
        start, count = now, 0
    _RATE_BUCKETS[key] = (start, count + 1)


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


@app.before_request
def _gate_trace_web():
    """`/trace` and its script `/trace-web.js` render the internal data model —
    every verb, table, derivation, door, and the two-phase confirm targets. That
    is reconnaissance surface, so it is session-gated (CODE-REVIEW-2026-08-08 #P3-1;
    the JS is static-served at the root, so gating the HTML route alone would leave
    the model directly fetchable). The rest of the frontend shell (index, app.js,
    render.js, style.css) stays public — it carries no model, and the SPA gates its
    own data through the API. Session-only by design: bearer/MCP clients read the
    model through the tools, never this page; an unauthenticated browser is sent to
    the SPA login."""
    p = request.path
    if (p == "/trace" or p.startswith("/trace-web.")) and "user_id" not in session:
        return redirect("/")


@app.errorhandler(Exception)
def _json_errors(e):
    """Backstop so /api/* always answers JSON, never Flask's HTML page — an
    unhandled 500 as HTML made api() in app.js fail to parse and blank the whole
    tab (CODE-REVIEW-2026-08-07 #14). HTTP errors keep their status; a truly
    unhandled exception is logged with its stack and returned as a generic 500
    with no internals leaked. Routes that already catch NotFound/ValueError are
    unaffected — this only sees what reaches the top."""
    if isinstance(e, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": e.description}), e.code
        return e  # non-API (static, redirects): Flask's default response
    app.logger.exception("unhandled error on %s", request.path)
    return jsonify({"error": "internal error"}), 500

DEFAULT_CATEGORIES = [
    "Groceries", "Dining", "Rent", "Utilities", "Internet", "Phone",
    "Transport", "Gas", "Health", "Pets", "Household", "Entertainment",
    "Subscriptions", "Travel", "Gifts", "Other",
]

# ---------------------------------------------------------------- database

def get_db():
    if "db" not in g:
        g.db = connect_existing(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

# ---------------------------------------------------------------- helpers

def dollars(cents):
    return round(cents / 100.0, 2)


def money_display(cents):
    """A money value as a display string a client can quote verbatim:
    '$1,234.56', negatives as '-$1,234.56' (sign before the symbol). Used by
    the assistant snapshot so an agent never formats or converts units."""
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def money(cents):
    """The dual money shape the assistant layer emits everywhere: integer
    cents plus a ready-to-quote display string."""
    return {"cents": cents, "display": money_display(cents)}


def bad_request(msg):
    return jsonify({"error": msg}), 400


def _resolve_auth():
    """Populate g.auth from the request's session cookie or bearer token, and
    return it (or None). A browser session carries both scopes — a human in
    their own browser; a token carries exactly its granted scopes."""
    if "auth" in g:
        return g.auth
    g.auth = None
    if "user_id" in session:
        g.auth = {"via": "session", "user_id": session["user_id"],
                  "scopes": {"read", "write"}}
    else:
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            row = actions.find_active_api_token(get_db(), header[7:].strip())
            if row is not None:
                g.auth = {
                    "via": "token", "user_id": row["user_id"],
                    "label": row["label"], "token_id": row["id"],
                    "scopes": {s.strip() for s in row["scopes"].split(",") if s.strip()},
                }
    return g.auth


def login_required(fn):
    """Session OR a valid bearer token. Bearer scope is enforced by HTTP
    method — this API is method-consistent, so GET needs 'read' and any
    mutating method needs 'write'. A browser session carries both."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        auth = _resolve_auth()
        if auth is None:
            return jsonify({"error": "authentication required"}), 401
        if auth["via"] == "token":
            needed = "read" if request.method == "GET" else "write"
            if needed not in auth["scopes"]:
                return jsonify({"error": f"token lacks '{needed}' scope"}), 403
        return fn(*args, **kwargs)
    return wrapper


def session_required(fn):
    """Browser-session only — rejects bearer tokens. For human-only surfaces:
    account setup and token management (a bearer token must never mint or
    revoke tokens — that would be privilege escalation)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "authentication required"}), 401
        g.auth = {"via": "session", "user_id": session["user_id"],
                  "scopes": {"read", "write"}}
        return fn(*args, **kwargs)
    return wrapper


# v1.0 name kept for its many read-side call sites; the canonical query
# lives with the verbs now (actions.active_members).
get_users = active_members


def ui_actor(db):
    """Actor string for the authenticated identity: 'ui:<username>' for a
    browser session, 'mcp:<label>' for a bearer token."""
    auth = g.get("auth") or _resolve_auth()
    if auth and auth["via"] == "token":
        return f"mcp:{auth['label']}"
    uid = auth["user_id"] if auth else session.get("user_id")
    member = db.execute("SELECT username FROM members WHERE id = ?", (uid,)).fetchone()
    return f"ui:{member['username']}" if member else f"ui:{uid}"


def txn_to_json(db, r, shares=None):
    # `shares` (optional): a prefetched {(txn_id, member_id): share_bp} dict from
    # prefetch_payer_shares, so a list endpoint resolves the payer share without
    # one SELECT per row (#16). Output is byte-identical either way.
    return {
        "id": r["id"],
        "date": r["txn_date"],
        "amount": dollars(r["amount_cents"]),
        "description": r["description"],
        "category": r["category"],
        "paid_by": r["paid_by"],
        "is_shared": bool(r["is_shared"]),
        "payer_share_pct": payer_share_pct(db, r["id"], r["paid_by"], shares),
        "source": r["source"],
    }


def compute_balance(db):
    """Present the canonical cent-based balance in the deployed API shape."""
    result = derive_balance(db)
    if result["state"] == "waiting":
        return {"settled": True, "amount": 0, "message": "Waiting for setup"}
    users = result["members"][:2]
    if result["state"] == "settled":
        return {
            "settled": True, "amount": 0,
            "owes": None, "owed": None,
            "message": "All settled up",
            "users": [{"id": u["id"], "name": u["display_name"]} for u in users],
        }
    ower, owed = result["ower"], result["owed"]
    amount_cents = result["amount_cents"]
    return {
        "settled": False,
        "amount": dollars(amount_cents),
        "owes": {"id": ower["id"], "name": ower["display_name"]},
        "owed": {"id": owed["id"], "name": owed["display_name"]},
        "message": f"{ower['display_name']} owes {owed['display_name']} "
                   f"${dollars(amount_cents):,.2f}",
        "users": [{"id": u["id"], "name": u["display_name"]} for u in users],
    }


# ---------------------------------------------------------------- auth & setup

@app.get("/api/status")
def status():
    db = get_db()
    count = db.execute("SELECT COUNT(*) AS c FROM members").fetchone()["c"]
    out = {"setup_required": count == 0, "logged_in": "user_id" in session}
    if out["logged_in"]:
        out["user_id"] = session["user_id"]
    return jsonify(out)


@app.post("/api/setup")
def setup():
    """One-time creation of exactly two accounts. Disabled once users exist."""
    db = get_db()
    if db.execute("SELECT COUNT(*) AS c FROM members").fetchone()["c"] > 0:
        return jsonify({"error": "setup already completed"}), 403
    data = request.get_json(silent=True) or {}
    users = data.get("users")
    if not isinstance(users, list) or len(users) != 2:
        return bad_request("provide exactly two users")
    seen = set()
    for u in users:
        username = (u.get("username") or "").strip().lower()
        display = (u.get("display_name") or "").strip()
        password = u.get("password") or ""
        if not username or not display:
            return bad_request("each user needs a username and display name")
        if len(password) < 8:
            return bad_request("passwords must be at least 8 characters")
        if username in seen:
            return bad_request("usernames must be different")
        seen.add(username)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for u in users:
        db.execute(
            "INSERT INTO members (username, display_name, password_hash, created_at) "
            "VALUES (?, ?, ?, ?)",
            (u["username"].strip().lower(), u["display_name"].strip(),
             generate_password_hash(u["password"]), now),
        )
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    # Brute-force guard (#17): reject early — before spending a password hash —
    # once this username has burned its failure budget. Counted on failure and
    # cleared on success below, so a legitimate login is never blocked as long
    # as the password is right.
    rl_key = f"login:{username}"
    if _rate_over(rl_key, LOGIN_MAX_FAILS, LOGIN_WINDOW_S):
        return jsonify({"error": "too many attempts — wait a few minutes"}), 429
    db = get_db()
    row = db.execute(
        "SELECT * FROM members WHERE username = ? AND active = 1", (username,)
    ).fetchone()
    # Always run a hash comparison — against a dummy hash when the user doesn't
    # exist — so an unknown username takes the same time as a wrong password.
    # Otherwise the short-circuit `or` skipped check_password_hash for unknown
    # users, a username-enumeration timing oracle (CODE-REVIEW-2026-08-07 #17).
    ok = check_password_hash(row["password_hash"] if row else _DUMMY_PW_HASH,
                             password)
    if row is None or not ok:
        _rate_hit(rl_key, LOGIN_WINDOW_S)
        return jsonify({"error": "wrong username or password"}), 401
    _RATE_BUCKETS.pop(rl_key, None)  # success clears the failure count
    session.permanent = True
    session["user_id"] = row["id"]
    return jsonify({"ok": True, "user": {"id": row["id"], "display_name": row["display_name"]}})


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
@login_required
def me():
    db = get_db()
    users = [{"id": u["id"], "username": u["username"], "display_name": u["display_name"]}
             for u in get_users(db)]
    return jsonify({"user_id": g.auth["user_id"], "users": users})


@app.get("/api/agents")
@login_required
def agents():
    """Catalog of Ledger's autonomous layer (the Agents tab) — a read-only map
    of the agents, assistants, and scheduled jobs acting on the app. Shared
    with the household like every other tab."""
    return jsonify(agent_catalog.catalog())


# ------------------------------------------------- ops panel (Agents tab)
# In-app operations VIEWS: the Pi guardian's latest report and recent audit
# activity — read-only, shared like every other tab. (An on-demand "sync now"
# was considered and dropped: the 06:30 timer already surfaces data under
# Activity, and SimpleFIN only refreshes ~daily + disables tokens past ~24
# requests/day, so a one-tap button was redundant risk. Sync stays scheduled;
# its budget guard lives in simplefin_sync.py.)

OPS_STATUS_FILE = os.environ.get(
    "OPS_STATUS_FILE", os.path.expanduser("~/pifinance-ops/ops-status.txt"))


@app.get("/api/ops/health")
@login_required
def ops_health():
    """The Pi Ops guardian's latest heartbeat report (ops-status.txt), plus
    its age — surfacing the existing daily check in-app instead of via SSH.
    Absent file (e.g. local dev, or the guardian not installed) is a normal
    state, not an error."""
    try:
        with open(OPS_STATUS_FILE) as f:
            report = f.read().strip()
        age_h = int((datetime.now().timestamp()
                     - os.path.getmtime(OPS_STATUS_FILE)) // 3600)
        return jsonify({"available": True, "report": report, "age_hours": age_h})
    except OSError:
        return jsonify({"available": False, "report": None, "age_hours": None})


@app.get("/api/ops/audit")
@login_required
def ops_audit():
    """Recent audit-log activity (newest first) — the system of record made
    visible without sqlite3 on the Pi. Row summaries only; detail payloads
    stay out of the response."""
    limit = max(1, min(int(request.args.get("limit", 30)), 200))
    db = get_db()
    rows = db.execute(
        "SELECT id, at, actor, action, target FROM audit_log "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return jsonify({"entries": [dict(r) for r in rows]})


# ------------------------------------------------------ api tokens (agent auth)

@app.post("/api/tokens")
@session_required
def create_token():
    """Mint a bearer token for the current member. Session-only — a token
    must never mint tokens. Scope is caller-chosen — 'read' (default) or
    'read,write' now that the agent write tier exists; the create_api_token
    verb validates the value. The plaintext token is returned ONCE and never
    stored; only its SHA-256 hash is kept."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        result = actions.create_api_token(db, ui_actor(db), {
            "label": data.get("label"),
            "user_id": g.auth["user_id"],
            "scopes": data.get("scopes", "read"),
        })
    except actions.ActionError as e:
        return bad_request(str(e))
    return jsonify({
        "id": result["id"], "label": result["label"], "scopes": result["scopes"],
        "created_at": result["created_at"],
        "token": result["token"],   # shown once — cannot be recovered later
    }), 201


@app.get("/api/tokens")
@session_required
def list_tokens():
    """The current member's tokens — never the token or its hash."""
    db = get_db()
    rows = db.execute(
        "SELECT id, label, scopes, created_at, last_used_at, revoked "
        "FROM api_tokens WHERE user_id = ? ORDER BY created_at DESC, id DESC",
        (g.auth["user_id"],)).fetchall()
    return jsonify([{
        "id": r["id"], "label": r["label"], "scopes": r["scopes"],
        "created_at": r["created_at"], "last_used_at": r["last_used_at"],
        "revoked": bool(r["revoked"]),
    } for r in rows])


@app.post("/api/tokens/<int:token_id>/revoke")
@session_required
def revoke_token(token_id):
    """Revoke one of the current member's own tokens."""
    db = get_db()
    owned = db.execute(
        "SELECT id FROM api_tokens WHERE id = ? AND user_id = ?",
        (token_id, g.auth["user_id"])).fetchone()
    if owned is None:
        return jsonify({"error": "not found"}), 404
    actions.revoke_api_token(db, ui_actor(db), token_id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- transactions

@app.get("/api/transactions")
@login_required
def list_transactions():
    db = get_db()
    month = request.args.get("month")  # YYYY-MM
    q = "SELECT * FROM transactions"
    params = []
    if month:
        q += " WHERE substr(txn_date, 1, 7) = ?"
        params.append(month)
    q += " ORDER BY txn_date DESC, id DESC LIMIT 500"
    rows = db.execute(q, params).fetchall()
    shares = prefetch_payer_shares(db, [r["id"] for r in rows])
    return jsonify([txn_to_json(db, r, shares) for r in rows])


@app.get("/api/activity")
@login_required
def activity():
    """The Activity feed's income-aware read surface. Extends the v1 txn
    shape with direction/income_type (merged on top of txn_to_json rather
    than added to it, so /api/transactions stays byte-frozen), offers a
    spending/income filter, and carries the household-wide unclassified
    inflow count that drives the "tag this" nudge. Read-only."""
    db = get_db()
    month = request.args.get("month")            # YYYY-MM, optional
    filt = request.args.get("filter", "all")     # all | spending | income
    if filt not in ("all", "spending", "income"):
        return bad_request("filter must be all, spending, or income")
    # Optional exact-category filter — the drill-in behind a "Spent by
    # category" row (the recategorize sheet reads exactly this). Matched
    # verbatim against the stored tag, so an empty value means "no filter",
    # not "the empty category".
    category = request.args.get("category") or None

    clauses, params = [], []
    if month:
        clauses.append("substr(txn_date, 1, 7) = ?")
        params.append(month)
    if filt == "spending":
        # Transfers are neither spend nor income, so the filtered views hide
        # them (they stay visible under 'all'); the 'all' feed shows every real
        # movement, transfers included but rendered neutrally.
        clauses.append("direction = 'out' AND is_transfer = 0")
    elif filt == "income":
        clauses.append("direction = 'in' AND is_transfer = 0")
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    q = "SELECT * FROM transactions"
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY txn_date DESC, id DESC LIMIT 500"
    rows = db.execute(q, params).fetchall()

    # Global (all-time), not month-scoped: the tag-me queue is everything
    # still waiting, so the badge doesn't hide work in another month.
    unclassified = db.execute(
        "SELECT COUNT(*) AS c FROM transactions "
        "WHERE direction = 'in' AND income_type = 'unclassified'").fetchone()["c"]

    shares = prefetch_payer_shares(db, [r["id"] for r in rows])
    return jsonify({
        "month": month,
        "filter": filt,
        "category": category,
        "transactions": [
            {**txn_to_json(db, r, shares),
             "direction": r["direction"], "income_type": r["income_type"],
             "is_transfer": bool(r["is_transfer"])}
            for r in rows
        ],
        "unclassified_count": unclassified,
    })


@app.post("/api/transactions")
@login_required
def create_transaction():
    db = get_db()
    data = request.get_json(silent=True) or {}
    if data.get("source") == "settlement":
        # Thin caller: the settle_up verb owns validation, the edit
        # (row + splits + settles links + audit, one transaction).
        try:
            row = actions.settle_up(db, actor=ui_actor(db), data=data)
        except ValueError as e:
            return bad_request(str(e))
        return jsonify(txn_to_json(db, row)), 201
    # Thin caller: the record_transaction verb owns validation and the
    # edit (row + splits + audit, one transaction). A manual entry never
    # carries an external_id — dedupe is sync's concern, not the UI's.
    try:
        row = actions.record_transaction(
            db, actor=ui_actor(db), data=data, source="manual")
    except ValueError as e:
        return bad_request(str(e))
    return jsonify(txn_to_json(db, row)), 201


@app.put("/api/transactions/<int:txn_id>")
@login_required
def update_transaction(txn_id):
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        row = actions.edit_transaction(db, ui_actor(db), txn_id, data)
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return bad_request(str(e))
    return jsonify(txn_to_json(db, row))


@app.delete("/api/transactions/<int:txn_id>")
@login_required
def delete_transaction(txn_id):
    db = get_db()
    try:
        actions.delete_transaction(db, ui_actor(db), txn_id)
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True})


@app.put("/api/transactions/<int:txn_id>/transfer")
@login_required
def set_transfer_view(txn_id):
    """Thin caller: the set_transfer verb marks/unmarks a row as a transfer
    between the household's own accounts. Response extends the deployed
    txn_to_json shape with direction/income_type/is_transfer — new fields on a
    new endpoint, so the byte-frozen listing shape stays untouched."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        row = actions.set_transfer(db, ui_actor(db), txn_id, data.get("is_transfer"))
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return bad_request(str(e))
    return jsonify({**txn_to_json(db, row),
                     "direction": row["direction"], "income_type": row["income_type"],
                     "is_transfer": bool(row["is_transfer"]),
                     "rule_suggestion": actions.suggest_transfer_rule_after_mark(db, row)})


@app.put("/api/transactions/<int:txn_id>/classify")
@login_required
def classify_transaction(txn_id):
    """Thin caller: the classify_inflow verb owns validation and the edit.
    Response extends the deployed txn_to_json shape with direction/
    income_type — new fields on a new endpoint, so the frozen listing
    shape (byte-pinned to v1.0) stays untouched."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        row = actions.classify_inflow(
            db, ui_actor(db), txn_id, data.get("income_type"))
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return bad_request(str(e))
    return jsonify({**txn_to_json(db, row),
                     "direction": row["direction"], "income_type": row["income_type"],
                     "rule_suggestion": actions.suggest_rule_after_classify(db, row)})


# ---------------------------------------------------------------- income rules

def rule_to_json(r):
    return {
        "id": r["id"],
        "priority": r["priority"],
        "match_desc": r["match_desc"],
        "match_account": r["match_account"],
        "min_cents": r["min_cents"],
        "max_cents": r["max_cents"],
        "set_type": r["set_type"],
        "set_paid_by": r["set_paid_by"],
        "set_transfer": bool(r["set_transfer"]),
        "enabled": bool(r["enabled"]),
        "created_at": r["created_at"],
        "hit_count": r["hit_count"],
    }


@app.get("/api/income/rules")
@login_required
def list_income_rules():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM income_rules ORDER BY priority ASC, id ASC").fetchall()
    return jsonify([rule_to_json(r) for r in rows])


@app.post("/api/income/rules")
@login_required
def create_income_rule():
    """Thin caller: the create_income_rule verb owns validation, the
    conflict check, and the edit."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        rule = actions.create_income_rule(db, actor=ui_actor(db), data=data)
    except ValueError as e:
        return bad_request(str(e))
    return jsonify(rule_to_json(rule)), 201


@app.put("/api/income/rules/<int:rule_id>")
@login_required
def update_income_rule(rule_id):
    """Thin caller over set_rule_enabled — the only edit the registry
    backs. There is no general rule-editing verb: correcting match
    criteria is delete-and-recreate until a standalone caller justifies
    one (same growth-rule discipline as every other verb here)."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    if "enabled" not in data:
        return bad_request("only 'enabled' can be changed")
    try:
        rule = actions.set_rule_enabled(
            db, actor=ui_actor(db), rule_id=rule_id, enabled=bool(data["enabled"]))
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    return jsonify(rule_to_json(rule))


@app.post("/api/income/rules/apply")
@login_required
def apply_income_rules():
    """Thin caller: the apply_rules verb owns the matching pass. dry_run
    previews without writing, per AGENT-DESIGN's dry-run-first pattern."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    dry_run = bool(data.get("dry_run", False))
    changes = actions.apply_rules(db, actor=ui_actor(db), dry_run=dry_run)
    return jsonify({"dry_run": dry_run, "changes": changes})


# ------------------------------------------ two-phase agent actions (step 7)

@app.post("/api/actions/propose")
@login_required
def propose_action_view():
    """Thin caller: PHASE 1 of the agent write tier's two-phase choreography
    (AGENT-DESIGN step 4). Dry-runs the action, parks a frozen payload +
    preview server-side, and returns a single-use confirmation token — no
    financial table is written here. Body is {action_type, ...payload}; the
    proposing token (when a bearer) is recorded as created_by."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    action_type = data.get("action_type")
    payload = {k: v for k, v in data.items() if k != "action_type"}
    created_by = (g.get("auth") or {}).get("token_id")
    try:
        result = actions.propose_action(
            db, actor=ui_actor(db), created_by=created_by,
            action_type=action_type, payload=payload)
    except ValueError as e:
        return bad_request(str(e))
    return jsonify(result), 201


@app.post("/api/actions/confirm")
@login_required
def confirm_action_view():
    """Thin caller: PHASE 2. Executes exactly the frozen payload the token
    points to. Single-use; an expired or already-consumed token is refused."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    token = data.get("confirmation_token")
    if not token:
        return bad_request("confirmation_token is required")
    try:
        result = actions.confirm_action(db, ui_actor(db), token)
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return bad_request(str(e))
    return jsonify(result)


# ------------------------------------------- inventory (INVENTORY-DESIGN)

def item_to_json(r):
    return {"id": r["id"], "name": r["name"], "category": r["category"],
            "kind": r["kind"], "status": r["status"], "note": r["note"],
            "restock_match": r["restock_match"],
            "restock_interval_days": r["restock_interval_days"],
            "last_stocked_at": r["last_stocked_at"], "updated_at": r["updated_at"],
            "store": r["store"], "need_by": r["need_by"],
            "snoozed_until": r["snoozed_until"]}


@app.get("/api/inventory")
@login_required
def inventory_view():
    """The pantry: the tracked staples, the computed shopping list, a low-stock
    count, purchase-feed restock suggestions (low/out staples with a matching
    purchase since they ran low — hints to confirm, INVENTORY-DESIGN step 5), a
    cadence-based restock forecast, and new-staple suggestions (frequently-bought
    merchants you don't yet track — an offer to start). Staples ordered
    most-urgent (out, then low) first; every money amount is dollars at the JSON
    edge."""
    db = get_db()
    staples = db.execute(
        "SELECT * FROM items WHERE active = 1 AND kind = 'staple' "
        "ORDER BY CASE status WHEN 'out' THEN 0 WHEN 'low' THEN 1 ELSE 2 END, "
        "name COLLATE NOCASE").fetchall()
    suggestions = restock_suggestions(db)
    for s in suggestions:
        cents = s["purchase"].pop("amount_cents")
        s["purchase"]["amount"] = {"cents": cents, "display": money_display(cents)}
    new_staples = new_staple_suggestions(db)
    for s in new_staples:
        cents = s.pop("total_spent_cents")
        s["total_spent"] = {"cents": cents, "display": money_display(cents)}
    spend = staple_spend(db)
    for s in spend:
        for key in ("total", "monthly", "recent", "earlier"):
            cents = s.pop(f"{key}_cents")
            s[key] = ({"cents": cents, "display": money_display(cents)}
                      if cents is not None else None)
    estimate = list_estimate(db)
    for line in estimate["lines"]:
        cents = line.pop("typical_cents")
        line["typical"] = ({"cents": cents, "display": money_display(cents)}
                           if cents is not None else None)
    cents = estimate.pop("total_cents")
    estimate["total"] = {"cents": cents, "display": money_display(cents)}
    plan = trip_plan(db)
    plan["list"] = estimate["lines"]            # already converted above
    cents = plan.pop("list_total_cents")
    plan["list_total"] = {"cents": cents, "display": money_display(cents)}
    for d in plan["due_soon"]:
        cents = d.pop("typical_cents")
        d["typical"] = ({"cents": cents, "display": money_display(cents)}
                        if cents is not None else None)
    closure = trip_closure(db)
    for g in closure:
        cents = g["purchase"].pop("amount_cents")
        g["purchase"]["amount"] = {"cents": cents, "display": money_display(cents)}
    return jsonify({
        "items": [item_to_json(r) for r in staples],
        "shopping": [item_to_json(r) for r in shopping_list(db)],
        "low_count": len(low_stock(db)),
        "restock_suggestions": suggestions,
        "restock_forecast": restock_forecast(db),
        "new_staple_suggestions": new_staples,
        "unmatched_staples": unmatched_staples(db),
        "stale_shopping_items": stale_shopping_items(db),
        "staple_spend": spend,
        "last_shopping_trip": last_shopping_trip(db),
        "list_estimate": estimate,
        "trip_plan": plan,
        "trip_closure": closure,
    })


@app.post("/api/inventory")
@login_required
def add_inventory_item():
    """Thin caller: the add_item verb owns validation and the edit."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        row = actions.add_item(db, ui_actor(db), data)
    except ValueError as e:
        return bad_request(str(e))
    return jsonify(item_to_json(row)), 201


@app.put("/api/inventory/<int:item_id>")
@login_required
def update_inventory_item(item_id):
    """Thin caller: set the item's status, its restock-match phrase, and/or its
    manual restock cadence (each its own verb). Pass 'status' (mark
    stocked/low/out), 'restock_match' (the optional purchase-feed override; blank
    clears it), and/or 'restock_interval_days' (remind every N days; null/blank
    clears it → back to the inferred cadence)."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    row = None
    try:
        if "status" in data:
            row = actions.set_item_status(db, ui_actor(db), item_id, data.get("status"))
        if "restock_match" in data:
            row = actions.set_item_match(db, ui_actor(db), item_id, data.get("restock_match"))
        if "restock_interval_days" in data:
            row = actions.set_item_interval(
                db, ui_actor(db), item_id, data.get("restock_interval_days"))
        if "store" in data:
            row = actions.set_item_store(db, ui_actor(db), item_id, data.get("store"))
        if "need_by" in data:
            row = actions.set_item_need_by(db, ui_actor(db), item_id, data.get("need_by"))
        if "snoozed_until" in data:
            row = actions.set_item_snooze(
                db, ui_actor(db), item_id, data.get("snoozed_until"))
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return bad_request(str(e))
    if row is None:
        return bad_request(
            "nothing to change: pass 'status', 'restock_match', "
            "'restock_interval_days', 'store', 'need_by', and/or 'snoozed_until'")
    return jsonify(item_to_json(row))


@app.post("/api/categories/merge")
@login_required
def merge_category_route():
    """Thin caller: merge_category relabels every reference to a category
    (all months, plus bills/pantry/budget) into another — the safe form of
    'delete a category'. The verb owns validation."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        result = actions.merge_category(db, ui_actor(db), data)
    except ValueError as e:
        return bad_request(str(e))
    return jsonify(result)


@app.post("/api/inventory/restock")
@login_required
def restock_inventory_items():
    """Thin caller: restock_items marks a bought set stocked in one action
    (all-or-nothing; the verb owns validation)."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        rows = actions.restock_items(db, ui_actor(db), data.get("item_ids"))
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return bad_request(str(e))
    return jsonify({"items": [item_to_json(r) for r in rows]})


@app.delete("/api/inventory/<int:item_id>")
@login_required
def delete_inventory_item(item_id):
    """Thin caller: archive_item soft-deletes (active=0)."""
    db = get_db()
    try:
        actions.archive_item(db, ui_actor(db), item_id)
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True})


def budget_to_json(row):
    """A budget row for the edge: its monthly limit as dollars {cents, display}."""
    return {"id": row["id"], "category": row["category"],
            "amount": {"cents": row["amount_cents"],
                       "display": money_display(row["amount_cents"])}}


@app.get("/api/budgets")
@login_required
def list_budgets():
    """Active category budgets — the set/edit surface's source (Analytics Tier C)."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM budgets WHERE active = 1 ORDER BY category").fetchall()
    return jsonify([budget_to_json(r) for r in rows])


@app.post("/api/budgets")
@login_required
def set_budget_route():
    """Thin caller: set_budget upserts one category's monthly spending limit."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        row = actions.set_budget(db, ui_actor(db), data)
    except ValueError as e:
        return bad_request(str(e))
    return jsonify(budget_to_json(row)), 201


@app.delete("/api/budgets/<int:budget_id>")
@login_required
def remove_budget_route(budget_id):
    """Thin caller: remove_budget soft-deletes (active=0)."""
    db = get_db()
    try:
        actions.remove_budget(db, ui_actor(db), budget_id)
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True})


@app.get("/api/income/summary")
@login_required
def income_summary_view():
    """The dashboard income card's data: cents from the income_summary
    derivation, presented as dollars at the JSON edge (like /api/balance).
    A new endpoint, not a field on /api/dashboard, so the byte-pinned v1
    dashboard shape stays frozen."""
    db = get_db()
    month = request.args.get("month") or current_period()
    s = income_summary(db, month)
    return jsonify({
        "month": month,
        "gross_inflows": dollars(s["gross_inflows_cents"]),
        "true_income": dollars(s["true_income_cents"]),
        "month_spend": dollars(s["month_spend_cents"]),
        "net_cash_flow": dollars(s["net_cash_flow_cents"]),
        "savings_rate": s["savings_rate"],
        "unclassified_count": s["unclassified_count"],
    })


@app.get("/api/income/trend")
@login_required
def income_trend_view():
    """Per-month income vs. net spend over a trailing window — the analytics
    chart's data source. Cents from the income_trend derivation, dollars at
    the JSON edge (like the income card). `anchor` defaults to the current
    period (as the dashboard card's month does); `months_back` is clamped to
    a sane window. New endpoint — the byte-pinned v1 surface stays frozen."""
    db = get_db()
    anchor = request.args.get("anchor") or current_period()
    ym = anchor.split("-")
    if len(ym) != 2 or not (ym[0].isdigit() and ym[1].isdigit()
                            and 1 <= int(ym[1]) <= 12):
        return bad_request("anchor must be YYYY-MM")
    try:
        months_back = int(request.args.get("months_back", 6))
    except (TypeError, ValueError):
        return bad_request("months_back must be an integer")
    months_back = max(1, min(months_back, 24))
    series = income_trend(db, months_back=months_back, anchor=anchor)
    return jsonify({
        "anchor": anchor,
        "months_back": months_back,
        "series": [{
            "month": e["month"],
            "gross_inflows": dollars(e["gross_inflows_cents"]),
            "true_income": dollars(e["true_income_cents"]),
            "month_spend": dollars(e["month_spend_cents"]),
            "net_cash_flow": dollars(e["net_cash_flow_cents"]),
            "savings_rate": e["savings_rate"],
            "unclassified_count": e["unclassified_count"],
        } for e in series],
    })


@app.get("/api/analytics/category-trend")
@login_required
def category_trend_view():
    """Per-month net spend for one category over a trailing window, with a
    trailing 3-month rolling average and MoM delta. Cents from the
    category_trend derivation, dollars at the JSON edge. First of the
    deeper-analytics endpoints (increment 8)."""
    db = get_db()
    category = (request.args.get("category") or "").strip()
    if not category:
        return bad_request("category is required")
    anchor = request.args.get("anchor") or current_period()
    ym = anchor.split("-")
    if len(ym) != 2 or not (ym[0].isdigit() and ym[1].isdigit()
                            and 1 <= int(ym[1]) <= 12):
        return bad_request("anchor must be YYYY-MM")
    try:
        months_back = int(request.args.get("months_back", 6))
    except (TypeError, ValueError):
        return bad_request("months_back must be an integer")
    months_back = max(1, min(months_back, 24))
    series = category_trend(db, category, months_back=months_back, anchor=anchor)
    return jsonify({
        "category": category,
        "anchor": anchor,
        "months_back": months_back,
        "series": [{
            "month": e["month"],
            "spend": dollars(e["spend_cents"]),
            "rolling_avg": dollars(e["rolling_avg_cents"]),
            "mom_delta": None if e["mom_delta_cents"] is None
                         else dollars(e["mom_delta_cents"]),
        } for e in series],
    })


@app.get("/api/analytics/savings-rate-trend")
@login_required
def savings_rate_trend_view():
    """Per-month savings rate over a trailing window, plus a trailing 3-month
    rolling rate that smooths single-month noise (analytics #9). Both are
    ratios (0.58 = 58%), not money, so they pass through unconverted; null
    when the relevant income is 0."""
    db = get_db()
    anchor = request.args.get("anchor") or current_period()
    ym = anchor.split("-")
    if len(ym) != 2 or not (ym[0].isdigit() and ym[1].isdigit()
                            and 1 <= int(ym[1]) <= 12):
        return bad_request("anchor must be YYYY-MM")
    try:
        months_back = int(request.args.get("months_back", 6))
    except (TypeError, ValueError):
        return bad_request("months_back must be an integer")
    months_back = max(1, min(months_back, 24))
    series = savings_rate_trend(db, months_back=months_back, anchor=anchor)
    return jsonify({
        "anchor": anchor,
        "months_back": months_back,
        "series": [{
            "month": e["month"],
            "savings_rate": e["savings_rate"],
            "rolling_savings_rate": e["rolling_savings_rate"],
        } for e in series],
    })


@app.get("/api/analytics/spending-composition")
@login_required
def spending_composition_view():
    """What made up a month's spend (analytics #10): each category's NET
    spend with its share of the month total, and the top merchants by
    description. Category shares come from `spending_summary` (net of
    refunds; share is a ratio, null when the total isn't positive); top
    merchants come from the `top_merchants` derivation (outflows only). Money
    as {cents, display}. Pure read."""
    db = get_db()
    month = request.args.get("month") or current_period()
    try:
        merchant_limit = int(request.args.get("merchant_limit", 10))
    except (TypeError, ValueError):
        return bad_request("merchant_limit must be an integer")
    merchant_limit = max(1, min(merchant_limit, 50))

    summary = spending_summary(db, month)[month]
    total = summary["total_cents"]
    by_category = [{
        "category": c["category"],
        "amount": money(c["amount_cents"]),
        "share": round(c["amount_cents"] / total, 4) if total > 0 else None,
    } for c in summary["by_category"]]
    merchants = [{
        "description": m["description"],
        "amount": money(m["amount_cents"]),
        "count": m["count"],
    } for m in top_merchants(db, month, merchant_limit)]

    return jsonify({
        "month": month,
        "total": money(total),
        "by_category": by_category,
        "top_merchants": merchants,
    })


@app.get("/api/analytics/budget-status")
@login_required
def budget_status_view():
    """Category budgets vs actual net spend for a period (Analytics Tier C),
    from the `budget_status` derivation. Actual is refund-netted (spending_
    summary). Money as {cents, display}; `pct`/`over` pass through. Pure read."""
    db = get_db()
    period = request.args.get("period") or current_period()
    status = budget_status(db, period)
    return jsonify({
        "period": status["period"],
        "budgets": [{
            "category": b["category"],
            "budgeted": money(b["budgeted_cents"]),
            "actual": money(b["actual_cents"]),
            "remaining": money(b["remaining_cents"]),
            "over": b["over"],
            "pct": b["pct"],
        } for b in status["budgets"]],
        "unbudgeted_spend": money(status["unbudgeted_spend_cents"]),
    })


@app.get("/api/analytics/bill-variance")
@login_required
def bill_variance_view():
    """Defined bill amount vs what actually got paid, per bill, for a period
    (analytics #12), from the `bill_variance` derivation. Unpaid bills report
    actual/variance = null. Money as {cents, display}. Pure read."""
    db = get_db()
    period = request.args.get("period") or current_period()
    return jsonify({
        "period": period,
        "bills": [{
            "bill_id": b["bill_id"],
            "name": b["name"],
            "due_day": b["due_day"],
            "category": b["category"],
            "defined": money(b["defined_cents"]),
            "actual": None if b["actual_cents"] is None else money(b["actual_cents"]),
            "variance": None if b["variance_cents"] is None else money(b["variance_cents"]),
            "paid": b["paid"],
        } for b in bill_variance(db, period)],
    })


@app.get("/api/analytics/member-breakdown")
@login_required
def member_breakdown_view():
    """Per-member shared-expense breakdown for a month (analytics #11): each
    person's paid (fronted) vs owed (fair share) vs net, from the
    `member_breakdown` derivation. Complements the who-owes-whom balance with
    the per-person composition. Money as {cents, display}. Pure read."""
    db = get_db()
    month = request.args.get("month") or current_period()
    return jsonify({
        "month": month,
        "members": [{
            "member_id": m["member_id"],
            "username": m["username"],
            "name": m["name"],
            "paid": money(m["paid_cents"]),
            "owed": money(m["owed_cents"]),
            "net": money(m["net_cents"]),
        } for m in member_breakdown(db, month)],
    })


@app.get("/api/analytics/recurring")
@login_required
def recurring_view():
    """Likely recurring charges / subscriptions (analytics Tier B #13), from the
    `recurring_charges` derivation: outflows that repeat at a stable amount on a
    regular cadence. A *suggestion* surface — a coincidence must not silently
    become a "subscription" — so it's conservative (same merchant + identical
    amount + ≥3 charges + regular gaps). Reports the detected cadence and the
    next expected date. Money as {cents, display}. Pure read."""
    db = get_db()
    return jsonify({
        "recurring": [{
            "merchant": c["merchant"],
            "example_description": c["example_description"],
            "amount": money(c["amount_cents"]),
            "occurrences": c["occurrences"],
            "interval_days": c["interval_days"],
            "cadence": c["cadence"],
            "first_charge": c["first_charge"],
            "last_charge": c["last_charge"],
            "predicted_next": c["predicted_next"],
        } for c in recurring_charges(db)],
    })


@app.get("/api/analytics/cash-flow-forecast")
@login_required
def cash_flow_forecast_view():
    """Projected month-end net cash flow — a conservative floor (analytics Tier
    B #14), from the `cash_flow_forecast` derivation: net-so-far (income − spend)
    minus the bills still unpaid this month. It's NET FLOW, not an account
    balance, and does NOT assume a not-yet-landed paycheck, so real month-end is
    usually better. Goals excluded (no scheduled contributions). Money as
    {cents, display}. Pure read."""
    db = get_db()
    period = request.args.get("period") or current_period()
    f = cash_flow_forecast(db, period)
    return jsonify({
        "period": period,
        "income_so_far": money(f["income_so_far_cents"]),
        "spend_so_far": money(f["spend_so_far_cents"]),
        "net_so_far": money(f["net_so_far_cents"]),
        "bills_remaining": [{
            "bill_id": b["bill_id"], "name": b["name"], "due_day": b["due_day"],
            "amount": money(b["amount_cents"]),
        } for b in f["bills_remaining"]],
        "bills_remaining_total": money(f["bills_remaining_cents"]),
        "projected_net": money(f["projected_net_cents"]),
    })


@app.get("/api/analytics/anomalies")
@login_required
def anomalies_view():
    """Categories spending unusually high this month vs their trailing 3-month
    average (analytics Tier B #15), from the `anomaly_flags` derivation. A
    passive heads-up: same normalized-month spend as the dashboard (refund-
    netted), flagged when `pct_over` clears the threshold and the jump clears a
    noise floor. `month` defaults to the current month; `threshold` (percent
    over baseline, default 50) is tunable. Money as {cents, display}. Pure read."""
    db = get_db()
    month = request.args.get("month") or current_period()
    try:
        threshold = int(request.args.get("threshold", 50))
    except ValueError:
        return bad_request("threshold must be an integer percent")
    return jsonify({
        "month": month,
        "threshold_pct": threshold,
        "anomalies": [{
            "category": a["category"],
            "current": money(a["current_cents"]),
            "baseline": money(a["baseline_cents"]),
            "delta": money(a["delta_cents"]),
            "pct_over": a["pct_over"],
        } for a in anomaly_flags(db, month, threshold_pct=threshold)],
    })


@app.get("/api/analytics/goal-pace")
@login_required
def goal_pace_view():
    """Per-goal completion-date projection at the lifetime-average contribution
    rate (analytics Tier B #16), from the `goal_pace` derivation: net saved vs
    target, a monthly rate, a projected finish date, and whether that beats the
    goal's target_date (on_track / behind / projected / complete / no_pace).
    `as_of` defaults to today. Money as {cents, display}; rate/projection null
    when there's no net pace to extrapolate. Pure read."""
    db = get_db()
    as_of = request.args.get("as_of") or date.today().isoformat()
    return jsonify({
        "as_of": as_of,
        "goals": [{
            "goal_id": g["goal_id"], "name": g["name"],
            "target": money(g["target_cents"]),
            "saved": money(g["saved_cents"]),
            "remaining": money(g["remaining_cents"]),
            "target_date": g["target_date"],
            "monthly_rate": None if g["monthly_rate_cents"] is None
                            else money(g["monthly_rate_cents"]),
            "projected_date": g["projected_date"],
            "status": g["status"],
        } for g in goal_pace(db, as_of)],
    })


@app.get("/api/household_snapshot")
@login_required
def household_snapshot_view():
    """One-call household overview for a month — the assistant layer's entry
    point (AGENT-DESIGN read tier: 'start here' for open-ended questions).

    Composes the canonical derivations — `compute_balance` (via
    derive_balance for cents), `spending_summary` (net of refunds), and
    `income_summary` — with the household's goals and bills. It runs no math
    of its own; every number comes from the same function a matching surface
    already uses, so the snapshot can't disagree with the dashboard. Every
    money field ships as `{cents, display}` so a client never converts
    units. Matches the app's current full-visibility behaviour; if income
    visibility is later scoped, that gets enforced upstream and this
    inherits it. Pure read — no schema, no write."""
    db = get_db()
    month = request.args.get("month") or current_period()

    spend = spending_summary(db, month)[month]
    inc = income_summary(db, month)
    bal = derive_balance(db)
    if bal["state"] == "owing":
        balance = {"state": "owing", **money(bal["amount_cents"]),
                   "ower": bal["ower"]["display_name"],
                   "owed": bal["owed"]["display_name"]}
    else:  # 'settled' or 'waiting'
        balance = {"state": bal["state"], **money(0), "ower": None, "owed": None}

    goal_rows = db.execute("SELECT * FROM goals ORDER BY created_at, id").fetchall()
    goals = []
    for g in goal_rows:
        saved = db.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM goal_contributions "
            "WHERE goal_id = ?", (g["id"],)).fetchone()["s"]
        goals.append({
            "name": g["name"],
            "target": money(g["target_cents"]),
            "saved": money(saved),
            "progress": min(1.0, saved / g["target_cents"]) if g["target_cents"] else 0,
            "target_date": g["target_date"],
        })

    bill_rows = db.execute(
        "SELECT * FROM bills WHERE active = 1 ORDER BY due_day, name").fetchall()
    bills = []
    for b in bill_rows:
        paid = db.execute(
            "SELECT 1 FROM bill_payments WHERE bill_id = ? AND period = ?",
            (b["id"], month)).fetchone() is not None
        bills.append({
            "name": b["name"], "amount": money(b["amount_cents"]),
            "due_day": b["due_day"], "category": b["category"],
            "paid_this_period": paid,
        })

    return jsonify({
        "month": month,
        "spend": {
            "total": money(spend["total_cents"]),
            "by_category": [{"category": c["category"], "amount": money(c["amount_cents"])}
                            for c in spend["by_category"]],
        },
        "balance": balance,
        "income": {
            "gross_inflows": money(inc["gross_inflows_cents"]),
            "true_income": money(inc["true_income_cents"]),
            "net_cash_flow": money(inc["net_cash_flow_cents"]),
            "savings_rate": inc["savings_rate"],
            "unclassified_count": inc["unclassified_count"],
        },
        "goals": goals,
        "bills": bills,
    })


@app.get("/api/transactions/search")
@login_required
def search_transactions_view():
    """Filtered, paginated transaction search — the assistant read tier's
    EVIDENCE tool (AGENT-DESIGN): 'show me the three biggest grocery runs',
    'did the deposit land?'. NOT for totals — those come from the summary
    endpoints, computed once by the same code as the dashboard. Filters
    (all optional, ANDed): query (case-insensitive substring on
    description), date_from/date_to (inclusive ISO), direction, income_type,
    category, paid_by (username; for inflows this is the money's owner).
    Paginated: limit 1..100 (default 20), offset. Returns total_matches +
    has_more so a caller pages rather than extrapolates; money as
    {cents, display}. Pure read."""
    db = get_db()
    args = request.args
    clauses, params = [], []

    query = (args.get("query") or "").strip()
    if query:
        clauses.append("lower(description) LIKE '%' || lower(?) || '%'")
        params.append(query)
    if args.get("date_from"):
        clauses.append("txn_date >= ?"); params.append(args["date_from"])
    if args.get("date_to"):
        clauses.append("txn_date <= ?"); params.append(args["date_to"])

    direction = args.get("direction")
    if direction is not None:
        if direction not in ("in", "out"):
            return bad_request("direction must be 'in' or 'out'")
        clauses.append("direction = ?"); params.append(direction)

    income_type = args.get("income_type")
    if income_type is not None:
        if income_type not in actions.INCOME_TYPES:
            return bad_request(
                "income_type must be one of: " + ", ".join(sorted(actions.INCOME_TYPES)))
        clauses.append("income_type = ?"); params.append(income_type)

    if args.get("category"):
        clauses.append("category = ?"); params.append(args["category"])

    paid_by = args.get("paid_by")
    if paid_by:
        member = db.execute(
            "SELECT id FROM members WHERE username = ?", (paid_by,)).fetchone()
        if member is None:
            return bad_request(f"unknown user: {paid_by}")
        clauses.append("paid_by = ?"); params.append(member["id"])

    try:
        limit = int(args.get("limit", 20))
        offset = int(args.get("offset", 0))
    except (TypeError, ValueError):
        return bad_request("limit and offset must be integers")
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    total = db.execute(
        f"SELECT COUNT(*) AS n FROM transactions {where}", params).fetchone()["n"]
    rows = db.execute(
        f"""SELECT * FROM transactions {where}
            ORDER BY txn_date DESC, id DESC LIMIT ? OFFSET ?""",
        params + [limit, offset]).fetchall()
    usernames = {m["id"]: m["username"] for m in db.execute(
        "SELECT id, username FROM members").fetchall()}
    return jsonify({
        "total_matches": total,
        "has_more": offset + limit < total,
        "limit": limit,
        "offset": offset,
        "transactions": [{
            "id": r["id"],
            "date": r["txn_date"],
            "description": r["description"],
            "amount": money(r["amount_cents"]),
            "direction": r["direction"],
            "category": r["category"],
            "income_type": r["income_type"],
            "paid_by": usernames.get(r["paid_by"]),
        } for r in rows],
    })


@app.post("/api/ask")
@session_required
def ask():
    """Charlee's in-app assistant. Runs a bounded Anthropic tool-use loop
    IN-PROCESS under the caller's own session (no MCP, no bearer token) and
    returns the answer. session_required — not bearer: a read token must never
    trigger paid API calls. The loop reads freely and can TAG an inflow
    (classify_inflow) through the same route the SPA uses, under the caller's
    identity; it cannot move money, settle up, make rules, or edit/delete.
    History is client-held and passed back each turn."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return bad_request("ask a question")
    history = data.get("history")
    if history is not None and not isinstance(history, list):
        return bad_request("history must be a list of prior messages")
    # Cost guard (#17): each query can drive several paid model round-trips, so
    # cap per-user request rate before running the loop.
    rl_key = f"ask:{session['user_id']}"
    if _rate_over(rl_key, ASK_MAX, ASK_WINDOW_S):
        return jsonify({"error": "you've asked a lot in a short time — "
                                 "give it a few minutes"}), 429
    _rate_hit(rl_key, ASK_WINDOW_S)
    try:
        result = ask_loop.answer(
            app, session["user_id"], message,
            period=current_period(), history=history)
    except ask_loop.NotConfigured:
        return jsonify({"error": "The assistant isn't set up yet — no API key "
                                 "is configured on the server."}), 503
    return jsonify(result)


@app.get("/api/categories")
@login_required
def categories():
    db = get_db()
    used = [r["category"] for r in db.execute(
        "SELECT DISTINCT category FROM transactions ORDER BY category")]
    merged = list(dict.fromkeys(DEFAULT_CATEGORIES + used))
    return jsonify(merged)


@app.get("/api/balance")
@login_required
def balance():
    return jsonify(compute_balance(get_db()))


@app.get("/api/settle/breakdown")
@login_required
def settle_breakdown_view():
    """Line-by-line explanation of the settle-up amount (the 'why is it this
    number' disclosure), from the `settle_breakdown` derivation. Each line is
    one unsettled shared expense with its signed owed delta; the signed lines
    sum to the same figure /api/balance shows, by construction. Money as
    {cents, display}; `owed` per line is SIGNED (+ = the second member owes the
    first, matching the balance's sign). Pure read."""
    db = get_db()
    bd = settle_breakdown(db)
    name_of = {m["id"]: m["display_name"] for m in bd["members"]}
    return jsonify({
        "state": bd["state"],
        "amount": money(bd["amount_cents"]),
        "ower": None if bd["ower"] is None
                else {"id": bd["ower"]["id"], "name": bd["ower"]["display_name"]},
        "owed": None if bd["owed"] is None
                else {"id": bd["owed"]["id"], "name": bd["owed"]["display_name"]},
        "owed_to_first": money(bd["owed_to_first_cents"]),
        "owed_to_second": money(bd["owed_to_second_cents"]),
        "carryover": money(bd["carryover_cents"]),
        "members": [{"id": m["id"], "name": m["display_name"]} for m in bd["members"]],
        "lines": [{
            "transaction_id": ln["transaction_id"],
            "date": ln["date"],
            "description": ln["description"],
            "category": ln["category"],
            "amount": money(ln["amount_cents"]),
            "paid_by": {"id": ln["paid_by"], "name": name_of.get(ln["paid_by"], "?")},
            "share_pct": ln["share_bp"] / 100,
            "owed": money(ln["owed_cents"]),
        } for ln in bd["lines"]],
    })


# ---------------------------------------------------------------- bills

def bill_to_json(db, r, period):
    payment = db.execute(
        "SELECT * FROM bill_payments WHERE bill_id = ? AND period = ?", (r["id"], period)
    ).fetchone()
    return {
        "id": r["id"],
        "name": r["name"],
        "amount": dollars(r["amount_cents"]),
        "due_day": r["due_day"],
        "category": r["category"],
        "paid_this_period": payment is not None,
        "paid_on": payment["paid_on"] if payment else None,
        "period": period,
    }


@app.get("/api/bills")
@login_required
def list_bills():
    db = get_db()
    period = request.args.get("period") or current_period()
    rows = db.execute("SELECT * FROM bills WHERE active = 1 ORDER BY due_day, name").fetchall()
    return jsonify([bill_to_json(db, r, period) for r in rows])


@app.post("/api/bills")
@login_required
def create_bill():
    """Thin caller: the create_bill verb owns validation and the edit."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        row = actions.create_bill(db, actor=ui_actor(db), data=data)
    except ValueError as e:
        return bad_request(str(e))
    return jsonify(bill_to_json(db, row, current_period())), 201


@app.put("/api/bills/<int:bill_id>")
@login_required
def update_bill(bill_id):
    """Thin caller: the update_bill verb owns validation and the edit."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        row = actions.update_bill(db, actor=ui_actor(db), bill_id=bill_id, data=data)
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return bad_request(str(e))
    return jsonify(bill_to_json(db, row, current_period()))


@app.delete("/api/bills/<int:bill_id>")
@login_required
def delete_bill(bill_id):
    """Thin caller: the delete_bill verb soft-deletes and audits the
    bill's state before deactivation."""
    db = get_db()
    try:
        actions.delete_bill(db, actor=ui_actor(db), bill_id=bill_id)
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True})


@app.post("/api/bills/<int:bill_id>/pay")
@login_required
def pay_bill(bill_id):
    """Thin caller: the mark_bill_paid verb owns validation and the edit
    (transaction + splits + bill_payments row + audit, one transaction)."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        bill, period = actions.mark_bill_paid(
            db, actor=ui_actor(db), bill_id=bill_id, data=data,
            default_paid_by=g.auth["user_id"])
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return bad_request(str(e))
    return jsonify(bill_to_json(db, bill, period)), 201


@app.delete("/api/bills/<int:bill_id>/pay")
@login_required
def unpay_bill(bill_id):
    """Thin caller: the unmark_bill_paid verb removes the payment, its
    transaction, splits, links, and writes the audit row atomically."""
    db = get_db()
    period = request.args.get("period") or current_period()
    try:
        actions.unmark_bill_paid(db, actor=ui_actor(db), bill_id=bill_id,
                                 period=period)
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------- goals

def goal_to_json(db, r):
    saved = db.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM goal_contributions WHERE goal_id = ?",
        (r["id"],),
    ).fetchone()["s"]
    return {
        "id": r["id"],
        "name": r["name"],
        "target": dollars(r["target_cents"]),
        "target_date": r["target_date"],
        "saved": dollars(saved),
        "progress": min(1.0, saved / r["target_cents"]) if r["target_cents"] else 0,
    }


@app.get("/api/goals")
@login_required
def list_goals():
    db = get_db()
    rows = db.execute("SELECT * FROM goals ORDER BY created_at, id").fetchall()
    return jsonify([goal_to_json(db, r) for r in rows])


@app.post("/api/goals")
@login_required
def create_goal():
    """Thin caller: the create_goal verb owns validation and the edit."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        row = actions.create_goal(db, actor=ui_actor(db), data=data)
    except ValueError as e:
        return bad_request(str(e))
    return jsonify(goal_to_json(db, row)), 201


@app.delete("/api/goals/<int:goal_id>")
@login_required
def delete_goal(goal_id):
    """Thin caller: the delete_goal verb audits the goal summary before
    the contribution cascade erases the history."""
    db = get_db()
    try:
        actions.delete_goal(db, actor=ui_actor(db), goal_id=goal_id)
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True})


@app.post("/api/goals/<int:goal_id>/contribute")
@login_required
def contribute(goal_id):
    """Thin dispatcher: sign picks the verb (correction-pass disposition —
    intent lives in the verb name; the row stores the signed amount)."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        cents = to_cents(data.get("amount"))
    except ValueError:
        return bad_request("invalid amount")
    if cents == 0:
        return bad_request("amount cannot be zero")
    verb = actions.contribute_to_goal if cents > 0 else actions.withdraw_from_goal
    try:
        goal = verb(db, ui_actor(db), goal_id, g.auth["user_id"],
                    abs(cents), data.get("note"))
    except actions.NotFound as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return bad_request(str(e))
    return jsonify(goal_to_json(db, goal)), 201


@app.get("/api/goals/<int:goal_id>/contributions")
@login_required
def contributions(goal_id):
    db = get_db()
    rows = db.execute(
        """SELECT gc.*, u.display_name FROM goal_contributions gc
           JOIN members u ON u.id = gc.user_id
           WHERE gc.goal_id = ? ORDER BY gc.c_date DESC, gc.id DESC""",
        (goal_id,),
    ).fetchall()
    return jsonify([
        {"id": r["id"], "amount": dollars(r["amount_cents"]), "date": r["c_date"],
         "by": r["display_name"], "note": r["note"]}
        for r in rows
    ])


# ---------------------------------------------------------------- dashboard

@app.get("/api/dashboard")
@login_required
def dashboard():
    db = get_db()
    month = request.args.get("month") or current_period()
    spending = spending_summary(db, month)[month]
    bills = db.execute("SELECT * FROM bills WHERE active = 1 ORDER BY due_day").fetchall()
    upcoming = [bill_to_json(db, b, month) for b in bills]
    unpaid = [b for b in upcoming if not b["paid_this_period"]]
    goals = [goal_to_json(db, r) for r in
             db.execute("SELECT * FROM goals ORDER BY created_at, id").fetchall()]
    recent_rows = db.execute(
        "SELECT * FROM transactions ORDER BY txn_date DESC, id DESC LIMIT 6").fetchall()
    recent_shares = prefetch_payer_shares(db, [r["id"] for r in recent_rows])
    recent = [txn_to_json(db, r, recent_shares) for r in recent_rows]
    return jsonify({
        "month": month,
        "month_total": dollars(spending["total_cents"]),
        "by_category": [
            {"category": row["category"], "amount": dollars(row["amount_cents"])}
            for row in spending["by_category"]
        ],
        "balance": compute_balance(db),
        "unpaid_bills": unpaid,
        "goals": goals,
        "recent": recent,
    })


# ---------------------------------------------------------------- static

# The SPA's cacheable assets. The index shell stamps each with a version query
# so a frontend deploy is picked up without a per-device hard refresh.
_VERSIONED_ASSETS = ("style.css", "render.js", "app.js")


def _asset_version(filename):
    """A cache-busting token = the asset's mtime in whole seconds. It changes
    only when the file changes — a deploy's `git checkout` rewrites just the
    files that differ — so the browser re-fetches exactly the changed assets
    and keeps the rest from cache. Falls back to '0' if the file is missing."""
    try:
        return str(int(os.path.getmtime(os.path.join(app.static_folder, filename))))
    except OSError:
        return "0"


@app.get("/")
def index():
    """Serve the SPA shell, version-stamping each asset URL (style.css /
    render.js / app.js). The HTML itself is sent no-cache — it's tiny, and it
    must be re-read each load so the stamps are always current; the stamped
    assets are what the browser caches until their content changes."""
    with open(os.path.join(app.static_folder, "index.html"), encoding="utf-8") as f:
        html = f.read()
    for name in _VERSIONED_ASSETS:
        v = _asset_version(name)
        html = html.replace(f'href="{name}"', f'href="{name}?v={v}"')
        html = html.replace(f'src="{name}"', f'src="{name}?v={v}"')
    return html, 200, {"Content-Type": "text/html; charset=utf-8",
                       "Cache-Control": "no-cache"}


@app.get("/api/ontology")
@login_required
def ontology_manifest():
    """The ontology manifest as JSON — the whole system described as one
    artifact, computed from source on every call (ONTOLOGY-MANIFEST-DESIGN;
    'nothing derived is stored' applied to metadata). Gated like /trace and for
    the same reason: it carries no household data, but the code's shape is
    reconnaissance surface (CODE-REVIEW-2026-08-08 #P3-1) — login_required is
    the API-shaped equivalent of the page's session gate (401 JSON, and a
    bearer read token qualifies, matching every other GET under /api/). The
    Trace Web is its consumer — the map draws whatever this reports, so the
    facts can never drift from the source."""
    return jsonify(ontology.manifest()), 200, {"Cache-Control": "no-cache"}


@app.get("/trace")
def trace_web():
    """The architecture Trace Web: a static, self-contained interactive map of
    the ontology (callers → verbs → objects → derivations → doors), reconciled
    against actions.py / derivations.py. Session-gated by `_gate_trace_web` (it
    carries no live household data, but the code's shape is reconnaissance
    surface — CODE-REVIEW-2026-08-08). Its edge logic is a SAME-ORIGIN script
    (trace-web.js, also gated), never
    an inline block, so the strict `script-src 'self'` CSP that neutralised the
    P0 XSS still holds; the shell version-stamps that script exactly as index()
    stamps the SPA assets, so a deploy is picked up without a hard refresh."""
    with open(os.path.join(app.static_folder, "trace-web.html"), encoding="utf-8") as f:
        html = f.read()
    v = _asset_version("trace-web.js")
    html = html.replace('src="trace-web.js"', f'src="trace-web.js?v={v}"')
    return html, 200, {"Content-Type": "text/html; charset=utf-8",
                       "Cache-Control": "no-cache"}


if __name__ == "__main__":
    # Loopback by default (P0-3): the dev server must not expose the LAN unless
    # BIND_HOST is deliberately set (prod runs under gunicorn, see the systemd
    # unit). Set BIND_HOST=0.0.0.0 or a tailnet IP only when you mean to.
    app.run(host=os.environ.get("BIND_HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", 8080)), debug=False)
