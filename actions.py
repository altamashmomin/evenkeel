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
Extracted so far: settle_up, mark_bill_paid, unmark_bill_paid. The
write-side helpers below moved here from app.py so the verbs own them;
app.py imports them for the routes that are still awaiting extraction.
"""
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation


class ActionError(ValueError):
    """Validation or submission-criteria failure; message is caller-safe."""


class NotFound(Exception):
    """Target row does not exist; routes map this to HTTP 404.
    Message is caller-safe and frozen (deployed API surface)."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_period():
    return date.today().strftime("%Y-%m")


def to_cents(value):
    """Parse a dollar amount (number or string) into integer cents, exactly."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("invalid amount")
    return int((d * 100).to_integral_value(rounding="ROUND_HALF_UP"))


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


def validate_txn_payload(db, data, partial=False):
    """Returns dict of column->value for insert/update. Raises ValueError.

    payer_share_pct is still the API's vocabulary but no longer a column —
    callers pop it from the result and hand it to write_splits. Error
    messages are frozen: they are part of the deployed API surface.
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
        try:
            pct = float(data.get("payer_share_pct", 50))
        except (TypeError, ValueError):
            raise ValueError("payer_share_pct must be a number")
        if not (0 <= pct <= 100):
            raise ValueError("payer_share_pct must be between 0 and 100")
        out["payer_share_pct"] = pct
    return out


def write_splits(db, txn_id, paid_by, is_shared, pct):
    """Replace a transaction's split rows from a payer-share percentage.

    The kernel of the future set_splits verb: unshared rows get no split
    rows; shared rows get one row per member, basis points summing to
    10000. Two-person closed form per the bounded transition (hardening
    disposition 1); proper N-member gating arrives with set_splits.
    """
    db.execute("DELETE FROM splits WHERE transaction_id = ?", (txn_id,))
    if not is_shared:
        return
    payer_bp = round(pct * 100)
    others = [m["id"] for m in active_members(db) if m["id"] != paid_by]
    db.execute(
        "INSERT INTO splits (transaction_id, member_id, share_bp) VALUES (?, ?, ?)",
        (txn_id, paid_by, payer_bp),
    )
    db.execute(
        "INSERT INTO splits (transaction_id, member_id, share_bp) VALUES (?, ?, ?)",
        (txn_id, others[0], 10000 - payer_bp),
    )


def settle_up(db, actor, data):
    """Record a settlement and link the shared rows it closes the books on.

    validate — the deployed payload validation, frozen messages; then the
    submission criterion from the CORE-DESIGN verbs table: at least two
    active members.
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
    cols = validate_txn_payload(db, data)
    pct = cols.pop("payer_share_pct", 50)
    if len(active_members(db)) < 2:
        raise ActionError("settle up requires at least two active members")

    cols["source"] = "settlement"
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    cur = db.execute(
        f"INSERT INTO transactions ({keys}) VALUES ({marks})", list(cols.values()))
    txn_id = cur.lastrowid
    write_splits(db, txn_id, cols["paid_by"], cols["is_shared"], pct)

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
    db.execute(
        "INSERT INTO audit_log (at, actor, action, target, detail_json) "
        "VALUES (?, ?, 'settle_up', ?, ?)",
        (now, actor, f"transaction:{txn_id}",
         json.dumps({"amount_cents": cols["amount_cents"],
                     "paid_by": cols["paid_by"],
                     "covers": covered}, sort_keys=True)))
    db.commit()
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
    paid_by = data.get("paid_by", default_paid_by)
    if paid_by not in {m["id"] for m in active_members(db)}:
        raise ActionError("paid_by must be one of the two users")
    is_shared = 1 if data.get("is_shared", True) else 0
    try:
        pct = float(data.get("payer_share_pct", 50))
    except (TypeError, ValueError):
        raise ActionError("payer_share_pct must be a number")
    if not (0 <= pct <= 100):
        raise ActionError("payer_share_pct must be between 0 and 100")

    today = date.today().isoformat()
    cur = db.execute(
        """INSERT INTO transactions
           (txn_date, amount_cents, description, category, paid_by,
            is_shared, source)
           VALUES (?, ?, ?, ?, ?, ?, 'bill')""",
        (today, bill["amount_cents"], f"{bill['name']} ({period})",
         bill["category"], paid_by, is_shared))
    txn_id = cur.lastrowid
    write_splits(db, txn_id, paid_by, is_shared, pct)
    db.execute(
        "INSERT INTO bill_payments (bill_id, period, paid_on, txn_id) "
        "VALUES (?, ?, ?, ?)",
        (bill_id, period, today, txn_id))
    db.execute(
        "INSERT INTO audit_log (at, actor, action, target, detail_json) "
        "VALUES (?, ?, 'mark_bill_paid', ?, ?)",
        (_now(), actor, f"bill:{bill_id}",
         json.dumps({"period": period, "transaction": txn_id,
                     "amount_cents": bill["amount_cents"], "paid_by": paid_by},
                    sort_keys=True)))
    db.commit()
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
    payment = db.execute(
        "SELECT * FROM bill_payments WHERE bill_id = ? AND period = ?",
        (bill_id, period)).fetchone()
    if payment is None:
        raise NotFound("no payment for this period")
    txn_id = payment["txn_id"]
    if txn_id:
        db.execute("DELETE FROM links WHERE from_id = ? OR to_id = ?",
                   (txn_id, txn_id))
        db.execute("DELETE FROM splits WHERE transaction_id = ?", (txn_id,))
        db.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
    db.execute("DELETE FROM bill_payments WHERE id = ?", (payment["id"],))
    db.execute(
        "INSERT INTO audit_log (at, actor, action, target, detail_json) "
        "VALUES (?, ?, 'unmark_bill_paid', ?, ?)",
        (_now(), actor, f"bill:{bill_id}",
         json.dumps({"period": period, "transaction": txn_id}, sort_keys=True)))
    db.commit()
