"""Pi Finance — a household finance app.

Flask + SQLite, no build step. See README.md for setup.
"""
import functools
import os
import secrets
import sqlite3
from datetime import date, datetime

from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

import actions
from actions import active_members, current_period, payer_share_pct, to_cents
from derivations import (compute_balance as derive_balance, income_summary,
                         spending_summary)
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


def bad_request(msg):
    return jsonify({"error": msg}), 400


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "authentication required"}), 401
        return fn(*args, **kwargs)
    return wrapper


# v1.0 name kept for its many read-side call sites; the canonical query
# lives with the verbs now (actions.active_members).
get_users = active_members


def ui_actor(db):
    """Actor string for the logged-in member: 'ui:<username>'."""
    member = db.execute(
        "SELECT username FROM members WHERE id = ?", (session["user_id"],)
    ).fetchone()
    return f"ui:{member['username']}" if member else f"ui:{session['user_id']}"


def txn_to_json(db, r):
    return {
        "id": r["id"],
        "date": r["txn_date"],
        "amount": dollars(r["amount_cents"]),
        "description": r["description"],
        "category": r["category"],
        "paid_by": r["paid_by"],
        "is_shared": bool(r["is_shared"]),
        "payer_share_pct": payer_share_pct(db, r["id"], r["paid_by"]),
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
    db = get_db()
    row = db.execute(
        "SELECT * FROM members WHERE username = ? AND active = 1", (username,)
    ).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "wrong username or password"}), 401
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
    return jsonify({"user_id": session["user_id"], "users": users})


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
    return jsonify([txn_to_json(db, r) for r in rows])


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
                     "direction": row["direction"], "income_type": row["income_type"]})


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
            default_paid_by=session["user_id"])
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
        goal = verb(db, ui_actor(db), goal_id, session["user_id"],
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
    recent = [txn_to_json(db, r) for r in db.execute(
        "SELECT * FROM transactions ORDER BY txn_date DESC, id DESC LIMIT 6").fetchall()]
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

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
