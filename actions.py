"""The action registry: every write path is a named verb (CORE-DESIGN
invariant 1).

Contract per verb — validate (including submission criteria) → edit (one
SQLite transaction that also carries the verb's audit row, hardening
disposition 4) → side effects (after commit) → return. Routes, sync, and
MCP tools are thin callers; no caller writes SQL against mutating tables.

Verbs expect a connection with row_factory = sqlite3.Row and commit
themselves at the end of the edit; on any exception nothing commits, so a
partial settlement (row without links, edit without audit) cannot exist.

Extraction proceeds one route per session (CORE-DESIGN sequence step 5).
Extracted so far: settle_up, mark_bill_paid, unmark_bill_paid,
create_goal, delete_goal, contribute_to_goal, withdraw_from_goal,
edit_transaction, delete_transaction. record_transaction (the sync
insert path) is last. The write-side helpers below moved here from
app.py so the verbs own them; app.py imports them for the routes that
are still awaiting extraction.
"""
import json
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from derivations import compute_balance


class ActionError(ValueError):
    """Validation or submission-criteria failure; message is caller-safe."""


class NotFound(Exception):
    """Target row does not exist; routes map this to HTTP 404.
    Message is caller-safe and frozen (deployed API surface)."""


@contextmanager
def action_transaction(db):
    """Own one complete verb transaction, including rollback on failure.

    Actions commit themselves, so accepting a connection with pending work
    would also commit a caller's unrelated edits. Refuse that ambiguous state
    rather than silently widening the verb's transaction boundary.
    """
    if db.in_transaction:
        raise RuntimeError("action requires a connection with no open transaction")
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.rollback()
        raise
    else:
        db.commit()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_audit(db, actor, action, target, detail, at=None):
    """Write one consistently timestamped, canonical JSON audit row."""
    at = at or _now()
    db.execute(
        "INSERT INTO audit_log (at, actor, action, target, detail_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (at, actor, action, target, json.dumps(detail, sort_keys=True)))
    return at


def current_period():
    return date.today().strftime("%Y-%m")


def to_cents(value):
    """Parse a dollar amount (number or string) into integer cents, exactly."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("invalid amount")
    return int((d * 100).to_integral_value(rounding="ROUND_HALF_UP"))


def parse_share_bp(value):
    """Parse the legacy percentage API into exact integer basis points."""
    try:
        pct = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("payer_share_pct must be a number")
    if not pct.is_finite():
        raise ValueError("payer_share_pct must be a number")
    if not (Decimal(0) <= pct <= Decimal(100)):
        raise ValueError("payer_share_pct must be between 0 and 100")
    basis_points = pct * 100
    if basis_points != basis_points.to_integral_value():
        raise ValueError("payer_share_pct must use at most two decimal places")
    return int(basis_points)


def parse_iso_date(s, field="date"):
    try:
        return date.fromisoformat(s).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"invalid {field} (expected YYYY-MM-DD)")


def active_members(db):
    """The canonical active-member listing every write path validates against."""
    return db.execute(
        "SELECT id, username, display_name FROM members WHERE active = 1 ORDER BY id"
    ).fetchall()


def payer_share_pct(db, txn_id, paid_by):
    """The payer's share as a percentage, derived from split rows.

    Kept in API responses for byte-compatibility with v1.0: the payer's
    share_bp / 100 for shared rows, the old default of 50 for unshared rows
    (which have no split rows at all).
    """
    row = db.execute(
        "SELECT share_bp FROM splits WHERE transaction_id = ? AND member_id = ?",
        (txn_id, paid_by),
    ).fetchone()
    return row["share_bp"] / 100 if row else 50.0


def validate_txn_payload(db, data, partial=False):
    """Returns dict of column->value for insert/update. Raises ValueError.

    payer_share_pct is still the API's vocabulary but no longer a column —
    callers pop it from the result and hand it to the legacy two-member
    split adapter. Error messages are frozen: they are part of the deployed
    API surface.
    """
    out = {}
    if "date" in data or not partial:
        out["txn_date"] = parse_iso_date(data.get("date"), "date")
    if "amount" in data or not partial:
        cents = to_cents(data.get("amount"))
        if cents <= 0:
            raise ValueError("amount must be positive")
        out["amount_cents"] = cents
    if "description" in data or not partial:
        desc = (data.get("description") or "").strip()
        if not desc:
            raise ValueError("description is required")
        out["description"] = desc[:200]
    if "category" in data or not partial:
        out["category"] = (data.get("category") or "Other").strip()[:60] or "Other"
    if "paid_by" in data or not partial:
        uid = data.get("paid_by")
        ids = {m["id"] for m in active_members(db)}
        if uid not in ids:
            raise ValueError("paid_by must be one of the two users")
        out["paid_by"] = uid
    if "is_shared" in data or not partial:
        out["is_shared"] = 1 if data.get("is_shared", True) else 0
    if "payer_share_pct" in data or not partial:
        out["payer_share_pct"] = parse_share_bp(
            data.get("payer_share_pct", 50)) / 100
    return out


def write_legacy_two_member_splits(
        db, txn_id, paid_by, is_shared, pct, members=None):
    """Apply the deployed percentage API's explicitly two-member split."""
    if not is_shared:
        db.execute("DELETE FROM splits WHERE transaction_id = ?", (txn_id,))
        return
    members = list(active_members(db) if members is None else members)
    if len(members) != 2:
        raise ActionError("shared transactions require exactly two active members")
    member_ids = {m["id"] for m in members}
    if paid_by not in member_ids:
        raise ActionError("paid_by must be one of the two users")
    payer_bp = parse_share_bp(pct)
    other_id = next(member_id for member_id in member_ids if member_id != paid_by)
    db.execute("DELETE FROM splits WHERE transaction_id = ?", (txn_id,))
    db.execute(
        "INSERT INTO splits (transaction_id, member_id, share_bp) VALUES (?, ?, ?)",
        (txn_id, paid_by, payer_bp),
    )
    db.execute(
        "INSERT INTO splits (transaction_id, member_id, share_bp) VALUES (?, ?, ?)",
        (txn_id, other_id, 10000 - payer_bp),
    )


def delete_transaction_graph(db, txn_id):
    """Delete a transaction and its reversible metadata, without committing.

    Returns a JSON-ready before-image for destructive audit records, or None
    when the transaction does not exist. The eventual delete_transaction verb
    will own policy; this helper only centralizes referential cleanup.
    """
    txn = db.execute(
        "SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if txn is None:
        return None
    splits = [dict(row) for row in db.execute(
        "SELECT * FROM splits WHERE transaction_id = ? ORDER BY member_id",
        (txn_id,)).fetchall()]
    links = [dict(row) for row in db.execute(
        "SELECT * FROM links WHERE from_id = ? OR to_id = ? ORDER BY id",
        (txn_id, txn_id)).fetchall()]
    db.execute("DELETE FROM links WHERE from_id = ? OR to_id = ?", (txn_id, txn_id))
    db.execute("DELETE FROM splits WHERE transaction_id = ?", (txn_id,))
    db.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
    return {"transaction": dict(txn), "splits": splits, "links": links}


def edit_transaction(db, actor, txn_id, data):
    """Edit a transaction's fields in place (the deployed route's payload).

    validate — the row exists (NotFound otherwise); settlement rows are
    frozen per CORE-DESIGN's settlement-history edit policy (source=
    'settlement' is corrected by delete and recreate, never rewritten in
    place); the deployed route's partial-payload validation and frozen
    "nothing to update" rejection.
    edit — one transaction: the changed columns, the legacy two-member
    split rows (an untouched share carries forward exactly as the old
    column did), the removal of any incoming 'settles' link this edit
    reopens (the covered-row case from the same policy — editing a
    covered row severs its links so the next settlement can re-cover it),
    and the audit row recording the before-image, the changed columns,
    and any severed links.
    side effects — none yet.
    Returns the updated row.
    """
    with action_transaction(db):
        existing = db.execute(
            "SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if existing is None:
            raise NotFound("not found")
        if existing["source"] == "settlement":
            raise ActionError(
                "settlement rows cannot be edited; delete and recreate")

        cols = validate_txn_payload(db, data, partial=True)
        pct = cols.pop("payer_share_pct", None)
        if not cols and pct is None:
            raise ActionError("nothing to update")
        if pct is None:
            # Share not part of this edit: the current payer's share
            # travels, exactly as the old column did.
            pct = payer_share_pct(db, txn_id, existing["paid_by"])
        if cols:
            sets = ", ".join(f"{k} = ?" for k in cols)
            db.execute(
                f"UPDATE transactions SET {sets} WHERE id = ?",
                [*cols.values(), txn_id])
        row = db.execute(
            "SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        write_legacy_two_member_splits(
            db, txn_id, row["paid_by"], row["is_shared"], pct)

        severed = [dict(r) for r in db.execute(
            "SELECT * FROM links WHERE link_type = 'settles' AND to_id = ? "
            "ORDER BY id", (txn_id,)).fetchall()]
        if severed:
            db.execute(
                "DELETE FROM links WHERE link_type = 'settles' AND to_id = ?",
                (txn_id,))

        _write_audit(
            db, actor, "edit_transaction", f"transaction:{txn_id}",
            {"before": dict(existing), "changed": cols,
             "payer_share_pct": pct, "severed_settles_links": severed})
    return row


def delete_transaction(db, actor, txn_id):
    """Delete a transaction and its reversible metadata (splits, links).

    validate — the row exists (NotFound otherwise).
    edit — one transaction: delete_transaction_graph's referential cleanup
    (any 'settles' link referencing this row is removed too — deleting a
    settlement therefore reopens what it covered, per the same
    settlement-history edit policy), then the audit row carrying the full
    before-image, since the cascade erases the history and the audit row
    becomes its only witness.
    side effects — none yet.
    """
    with action_transaction(db):
        deleted = delete_transaction_graph(db, txn_id)
        if deleted is None:
            raise NotFound("not found")
        _write_audit(db, actor, "delete_transaction", f"transaction:{txn_id}",
                     {"deleted": deleted})


def settle_up(db, actor, data):
    """Record a settlement and link the shared rows it closes the books on.

    validate — the deployed payload validation, frozen messages; then the
    bounded-transition submission criterion from the CORE-DESIGN verbs
    table: exactly two active members. Amount and payer are stale-state
    assertions: the verb recomputes both from the ledger as of its date.
    edit — one transaction: the settlement row (source forced to
    'settlement'), its split rows, a 'settles' link to every shared
    transaction dated on or before the settlement and not already covered
    by a previous settlement (previous settlements included: this one
    closes them too; rows dated after the settlement stay uncovered for
    the next one; historic rows from before the links table simply start
    uncovered — forward-only), and the audit row.
    side effects — none yet.
    Returns the settlement transaction row.
    """
    with action_transaction(db):
        cols = validate_txn_payload(db, data)
        submitted_pct = cols.pop("payer_share_pct", 50)
        members = active_members(db)
        if len(members) != 2:
            raise ActionError("settle up requires exactly two active members")
        if not cols["is_shared"]:
            raise ActionError("settlement must be shared")
        if parse_share_bp(submitted_pct) != 0:
            raise ActionError("settlement payer share must be 0")

        balance = compute_balance(db, as_of=cols["txn_date"])
        if balance["state"] != "owing":
            raise ActionError("household is already settled as of this date")
        if (cols["amount_cents"] != balance["amount_cents"] or
                cols["paid_by"] != balance["ower"]["id"]):
            raise ActionError("balance changed; refresh and try again")

        cols.update({
            "amount_cents": balance["amount_cents"],
            "description": (f"Settlement — {balance['ower']['display_name']} → "
                            f"{balance['owed']['display_name']}"),
            "category": "Settlement",
            "paid_by": balance["ower"]["id"],
            "is_shared": 1,
            "source": "settlement",
        })
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        cur = db.execute(
            f"INSERT INTO transactions ({keys}) VALUES ({marks})", list(cols.values()))
        txn_id = cur.lastrowid
        write_legacy_two_member_splits(
            db, txn_id, cols["paid_by"], cols["is_shared"], 0, members)

        now = _now()
        covered = [r[0] for r in db.execute(
            """SELECT DISTINCT t.id FROM transactions t
               JOIN splits s ON s.transaction_id = t.id
               WHERE t.id != ? AND t.txn_date <= ? AND NOT EXISTS (
                   SELECT 1 FROM links l
                   WHERE l.link_type = 'settles' AND l.to_id = t.id)
               ORDER BY t.id""", (txn_id, cols["txn_date"])).fetchall()]
        db.executemany(
            "INSERT INTO links (link_type, from_id, to_id, created_by, created_at) "
            "VALUES ('settles', ?, ?, ?, ?)",
            [(txn_id, covered_id, actor, now) for covered_id in covered])
        _write_audit(
            db, actor, "settle_up", f"transaction:{txn_id}",
            {"amount_cents": cols["amount_cents"],
             "paid_by": cols["paid_by"], "owed": balance["owed"]["id"],
             "as_of": cols["txn_date"], "covers": covered}, at=now)
    return db.execute(
        "SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()


def mark_bill_paid(db, actor, bill_id, data, default_paid_by):
    """Mark a bill paid for a period and record it as a transaction.

    validate — active bill exists (NotFound otherwise), period shape,
    no duplicate payment for the period, payer is an active member,
    share fields well-formed. Messages and check order are the deployed
    route's, frozen.
    edit — one transaction: the bill's transaction row (source 'bill',
    dated today, amount and category from the bill), its split rows, the
    bill_payments row, and the audit row.
    side effects — none yet.
    Returns (bill_row, period) for the route's bill_to_json response.
    """
    with action_transaction(db):
        bill = db.execute(
            "SELECT * FROM bills WHERE id = ? AND active = 1", (bill_id,)).fetchone()
        if bill is None:
            raise NotFound("not found")
        period = data.get("period") or current_period()
        if len(period) != 7 or period[4] != "-":
            raise ActionError("period must be YYYY-MM")
        if db.execute("SELECT id FROM bill_payments WHERE bill_id = ? AND period = ?",
                      (bill_id, period)).fetchone():
            raise ActionError("already marked paid for this period")
        members = active_members(db)
        paid_by = data.get("paid_by", default_paid_by)
        if paid_by not in {m["id"] for m in members}:
            raise ActionError("paid_by must be one of the two users")
        is_shared = 1 if data.get("is_shared", True) else 0
        pct = parse_share_bp(data.get("payer_share_pct", 50)) / 100
        if is_shared and len(members) != 2:
            raise ActionError("shared transactions require exactly two active members")

        today = date.today().isoformat()
        cur = db.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source)
               VALUES (?, ?, ?, ?, ?, ?, 'bill')""",
            (today, bill["amount_cents"], f"{bill['name']} ({period})",
             bill["category"], paid_by, is_shared))
        txn_id = cur.lastrowid
        write_legacy_two_member_splits(
            db, txn_id, paid_by, is_shared, pct, members)
        db.execute(
            "INSERT INTO bill_payments (bill_id, period, paid_on, txn_id) "
            "VALUES (?, ?, ?, ?)",
            (bill_id, period, today, txn_id))
        _write_audit(
            db, actor, "mark_bill_paid", f"bill:{bill_id}",
            {"period": period, "transaction": txn_id,
             "amount_cents": bill["amount_cents"], "paid_by": paid_by})
    return bill, period


def unmark_bill_paid(db, actor, bill_id, period):
    """Undo a bill payment for a period.

    validate — a payment for (bill, period) exists (NotFound otherwise).
    edit — one transaction: remove any links referencing the payment's
    transaction (invariant 5: links are metadata and revert with it),
    its split rows, the transaction itself, the bill_payments row, and
    the audit row.
    side effects — none yet.
    """
    with action_transaction(db):
        payment = db.execute(
            "SELECT * FROM bill_payments WHERE bill_id = ? AND period = ?",
            (bill_id, period)).fetchone()
        if payment is None:
            raise NotFound("no payment for this period")
        txn_id = payment["txn_id"]
        bill = db.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
        deleted = delete_transaction_graph(db, txn_id) if txn_id else None
        db.execute("DELETE FROM bill_payments WHERE id = ?", (payment["id"],))
        _write_audit(
            db, actor, "unmark_bill_paid", f"bill:{bill_id}",
            {"period": period, "bill": dict(bill) if bill else None,
             "payment": dict(payment), "deleted": deleted})


def create_goal(db, actor, data):
    """Create a savings goal.

    validate — name required, target parses to positive cents, optional
    target date is ISO. Messages are the deployed route's, frozen (the
    route translated to_cents's error into 'invalid target amount';
    so does the verb).
    edit — one transaction: the goal row and the audit row carrying the
    created shape.
    side effects — none yet.
    Returns the goal row.
    """
    with action_transaction(db):
        name = (data.get("name") or "").strip()
        if not name:
            raise ActionError("name is required")
        try:
            target_cents = to_cents(data.get("target"))
        except ValueError:
            raise ActionError("invalid target amount")
        if target_cents <= 0:
            raise ActionError("target must be positive")
        target_date = None
        if data.get("target_date"):
            target_date = parse_iso_date(data["target_date"], "target date")
        cur = db.execute(
            "INSERT INTO goals (name, target_cents, target_date) VALUES (?, ?, ?)",
            (name[:100], target_cents, target_date))
        goal = db.execute(
            "SELECT * FROM goals WHERE id = ?", (cur.lastrowid,)).fetchone()
        _write_audit(db, actor, "create_goal", f"goal:{goal['id']}",
                     {"goal": dict(goal)})
    return goal


def delete_goal(db, actor, goal_id):
    """Hard-delete a goal (bounded transition; archive_goal arrives with
    its own migration increment).

    validate — the goal exists (NotFound otherwise).
    edit — one transaction: capture the before-image (goal summary,
    contribution count, saved total — the cascade erases the history, so
    the audit row becomes its only witness), delete the goal (contributions
    cascade), audit.
    side effects — none yet.
    """
    with action_transaction(db):
        goal = db.execute(
            "SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if goal is None:
            raise NotFound("not found")
        summary = db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS saved "
            "FROM goal_contributions WHERE goal_id = ?", (goal_id,)).fetchone()
        db.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
        _write_audit(db, actor, "delete_goal", f"goal:{goal_id}",
                     {"goal": dict(goal),
                      "contribution_count": summary["n"],
                      "saved_cents": summary["saved"]})


def _record_goal_flow(db, actor, verb, goal_id, member_id, amount_cents, note):
    """Shared body for contribute/withdraw: signed row, named intent."""
    with action_transaction(db):
        goal = db.execute(
            "SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if goal is None:
            raise NotFound("not found")
        if member_id not in {m["id"] for m in active_members(db)}:
            raise ActionError("member must be an active member")
        if not isinstance(amount_cents, int) or amount_cents <= 0:
            raise ActionError("amount must be positive")
        signed = amount_cents if verb == "contribute_to_goal" else -amount_cents
        note = (note or "").strip()[:200] or None
        cur = db.execute(
            "INSERT INTO goal_contributions "
            "(goal_id, user_id, amount_cents, c_date, note) VALUES (?, ?, ?, ?, ?)",
            (goal_id, member_id, signed, date.today().isoformat(), note))
        _write_audit(db, actor, verb, f"goal:{goal_id}",
                     {"contribution": cur.lastrowid, "amount_cents": amount_cents,
                      "member": member_id, "note": note})
    return goal


def contribute_to_goal(db, actor, goal_id, member_id, amount_cents, note=None):
    """Add money toward a goal. amount_cents is a positive magnitude."""
    return _record_goal_flow(
        db, actor, "contribute_to_goal", goal_id, member_id, amount_cents, note)


def withdraw_from_goal(db, actor, goal_id, member_id, amount_cents, note=None):
    """Take money back out of a goal. amount_cents is a positive magnitude;
    the stored row is negative, but the verb name — not the sign — is what
    records intent (correction-pass disposition)."""
    return _record_goal_flow(
        db, actor, "withdraw_from_goal", goal_id, member_id, amount_cents, note)
