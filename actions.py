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
edit_transaction, delete_transaction, record_transaction — the sequence's
last verb — plus create_bill, update_bill, delete_bill, extracted out of
sequence to close the last invariant-1 gap (bill definitions were the
only mutating table still taking raw SQL from a route). classify_inflow,
create_income_rule, set_rule_enabled, and apply_rules build the income
classification foundation (CORE-DESIGN sequence step 6). set_splits is
promoted separately, only once a standalone caller needs it.
"""
import hashlib

from werkzeug.security import check_password_hash, generate_password_hash
import json
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from derivations import compute_balance


class ActionError(ValueError):
    """Validation or submission-criteria failure; message is caller-safe."""


class NotFound(Exception):
    """Target row does not exist; routes map this to HTTP 404.
    Message is caller-safe and frozen (deployed API surface)."""


# The tables the action registry governs: every write to these must go
# through a verb here (CORE-DESIGN invariant 1). This lives with the
# registry it describes — tests/test_architecture.py imports it to enforce
# the no-raw-writes invariant, and ontology.py imports it to enumerate the
# object types. 'members' is included but carries a documented exception in
# the architecture test (account/auth management isn't a verb yet —
# CORE-DESIGN open question "Member auth mechanics beyond two").
GOVERNED_TABLES = frozenset({
    "transactions", "splits", "links", "goals", "goal_contributions",
    "bills", "bill_payments", "audit_log", "income_rules", "members",
    "api_tokens", "pending_actions", "items", "budgets",
})


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


# SQLite stores INTEGER as signed 64-bit; a cents value past this overflows at
# bind time. Verbs that accept a free-form amount cap against it so an absurd
# input is a clean validation error, not a 500 (MIRAGE B3 LOW-1).
_MAX_MONEY_CENTS = 2 ** 63 - 1


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


def payer_share_pct(db, txn_id, paid_by, shares=None):
    """The payer's share as a percentage, derived from split rows.

    Kept in API responses for byte-compatibility with v1.0: the payer's
    share_bp / 100 for shared rows, the old default of 50 for unshared rows
    (which have no split rows at all).

    `shares` — an optional {(txn_id, member_id): share_bp} dict from
    prefetch_payer_shares. When given, the value is read from it instead of a
    per-row SELECT, so a list endpoint avoids one query per transaction (the
    N+1 in CODE-REVIEW-2026-08-07 #16). The /100-else-50 rule is single-sourced
    here, so batched and unbatched callers can't diverge — byte-identical output.
    """
    if shares is not None:
        bp = shares.get((txn_id, paid_by))
        return bp / 100 if bp is not None else 50.0
    row = db.execute(
        "SELECT share_bp FROM splits WHERE transaction_id = ? AND member_id = ?",
        (txn_id, paid_by),
    ).fetchone()
    return row["share_bp"] / 100 if row else 50.0


def prefetch_payer_shares(db, txn_ids):
    """Batch the split lookup payer_share_pct does one-at-a-time: ONE query for
    a whole page of transactions instead of one per row (CODE-REVIEW #16).
    Returns {(transaction_id, member_id): share_bp}; pass it to
    payer_share_pct/txn_to_json as `shares`."""
    ids = list(txn_ids)
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = db.execute(
        f"SELECT transaction_id, member_id, share_bp FROM splits "
        f"WHERE transaction_id IN ({marks})", ids).fetchall()
    return {(r["transaction_id"], r["member_id"]): r["share_bp"] for r in rows}


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
        raw = data.get("description")
        if raw is not None and not isinstance(raw, str):
            raise ValueError("description must be text")
        desc = (raw or "").strip()
        if not desc:
            raise ValueError("description is required")
        out["description"] = desc[:200]
    if "category" in data or not partial:
        raw = data.get("category")
        if raw is not None and not isinstance(raw, str):
            raise ValueError("category must be text")
        out["category"] = (raw or "Other").strip()[:60] or "Other"
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


def record_transaction(db, actor, data, source, external_id=None, direction="out"):
    """Insert a new transaction — the UI's manual entry and the sync
    script's insert, unified (CORE-DESIGN: "sync's insert path becomes a
    call to this; dedupe stays inside it").

    validate — the deployed route's full-payload validation, frozen
    messages. source AND direction are verb decisions the caller passes
    explicitly, never read from `data` — the same discipline settle_up and
    mark_bill_paid use to force their own source tag. This is deliberate:
    it means the manual-entry route (which passes only source='manual')
    cannot be coaxed into creating an inflow by a client slipping
    "direction":"in" into the JSON body — a manual POST is always a spend
    until a designed manual-income feature exists. Only sync passes
    direction='in', for the money-in leg it now imports.
    edit — one transaction: the insert (a no-op on a duplicate
    external_id, via the same ON CONFLICT the deployed sync script used
    directly — sync's lookback window is expected to overlap between
    runs, so a duplicate is routine, not an error). An outflow gets its
    legacy two-member split rows exactly as before. An inflow gets none
    at all — "no share fields regardless of the SYNC defaults" per
    INCOME-DESIGN invariant 1, which is also what keeps compute_balance's
    splits join structurally blind to it — and is matched against
    enabled income_rules immediately, the same first-match-wins pass
    apply_rules runs later for backfill: a match sets income_type and,
    if the rule overrides the owner, paid_by, and bumps the rule's
    hit_count; no match lands 'unclassified'. The audit row records
    source, direction, and — for an inflow — what it was classified as
    and by which rule, if any; skipped entirely on the dedupe no-op,
    since nothing happened to audit.
    side effects — none yet.
    Returns the new row, or None when external_id deduped.
    """
    with action_transaction(db):
        if direction not in ("in", "out"):
            raise ActionError("direction must be 'in' or 'out'")

        cols = validate_txn_payload(db, data)
        pct = cols.pop("payer_share_pct", 50)
        cols["source"] = source
        cols["external_id"] = external_id
        cols["direction"] = direction

        matched_rule = None
        if direction == "in":
            cols["is_shared"] = 0
            matched_rule = _first_matching_rule(db, {
                "description": cols["description"],
                "external_id": external_id,
                "amount_cents": cols["amount_cents"]})
            if matched_rule is not None:
                cols["income_type"] = matched_rule["set_type"]
                if matched_rule["set_paid_by"] is not None:
                    cols["paid_by"] = matched_rule["set_paid_by"]
                # A transfer rule (set_transfer=1, migration #013) marks the
                # match a transfer — it drops out of income/spend/balance. 0 on
                # every ordinary rule, so this is inert until a transfer rule
                # is created (T3b).
                if matched_rule["set_transfer"]:
                    cols["is_transfer"] = 1
            else:
                cols["income_type"] = "unclassified"
        else:
            cols["income_type"] = None

        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        cur = db.execute(
            f"INSERT INTO transactions ({keys}) VALUES ({marks}) "
            "ON CONFLICT(external_id) DO NOTHING",
            list(cols.values()))
        if cur.rowcount == 0:
            return None
        txn_id = cur.lastrowid
        if direction == "out":
            write_legacy_two_member_splits(
                db, txn_id, cols["paid_by"], cols["is_shared"], pct)
        if matched_rule is not None:
            db.execute(
                "UPDATE income_rules SET hit_count = hit_count + 1 WHERE id = ?",
                (matched_rule["id"],))
        _write_audit(
            db, actor, "record_transaction", f"transaction:{txn_id}",
            {"source": source, "direction": direction,
             "amount_cents": cols["amount_cents"], "paid_by": cols["paid_by"],
             "is_shared": bool(cols["is_shared"]), "external_id": external_id,
             "income_type": cols["income_type"],
             "matched_rule_id": matched_rule["id"] if matched_rule else None})
    return db.execute(
        "SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()


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
        # An inflow never carries splits (INCOME-DESIGN invariant 1) — the money
        # derivations read direction='out' splits only, so a shared inflow is a
        # latent invariant breach. create_transaction forces is_shared=0 for a
        # money-in leg; edit must refuse the same here rather than writing split
        # rows onto an inflow (CODE-REVIEW 2026-08-08, Tier 2 #7).
        if existing["direction"] == "in" and cols.get("is_shared") == 1:
            raise ActionError("an inflow cannot be shared")
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


def recategorize_transaction(db, actor, txn_id, category):
    """Relabel ONE transaction's category — and nothing else.

    A constrained facade over edit_transaction (the single transaction-edit
    write path): it delegates a category-only payload, so it inherits that
    verb's validation, split-preserving behaviour, settlement freeze, and audit
    row (recorded as 'edit_transaction', changed={category}). It exists so the
    agent write tier can offer recategorize as its own narrow tool whose schema
    (PARAM_SPECS, additionalProperties:false) makes it structurally impossible
    to reach amount, splits, or description through it — the model can pass only
    a transaction id and a category. A category-only edit is proven not to move
    the balance or any month total (test_recategorize_leaves_balance_and_month_
    total_unchanged); only the per-category distribution shifts. Reversible
    (recategorize back). Returns the updated row.

    Two guards beyond edit_transaction, tightening the facade to its stated
    contract (ledger-mirage F1–F3):
    - a category is REQUIRED and must be text — a missing/blank one would
      otherwise default to 'Other' and silently clobber the label;
    - only SPENDING rows (direction='out') may be recategorized, so the
      "month total never moves" promise holds exactly: relabeling a refund
      inflow would shift refund-netting between two categories."""
    if category is None:
        raise ActionError("a category is required")
    if not isinstance(category, str):
        raise ActionError("category must be text")
    if not category.strip():
        raise ActionError("a category is required")
    existing = db.execute(
        "SELECT direction FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if existing is None:
        raise NotFound("not found")
    if existing["direction"] != "out":
        raise ActionError("only spending rows can be recategorized")
    return edit_transaction(db, actor, txn_id, {"category": category})


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
    outflow transaction dated on or before the settlement and not already
    covered by a previous settlement (previous settlements included: this
    one closes them too; rows dated after the settlement stay uncovered
    for the next one; historic rows from before the links table simply
    start uncovered — forward-only; direction='out' is explicit
    defense-in-depth alongside the splits join that already excludes
    inflows structurally, same reasoning as compute_balance), and the
    audit row.
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
            # Explicit, not by column default: a settlement *is* counted by
            # compute_balance (it's what zeroes the books), so its
            # direction='out' is load-bearing and stated here rather than
            # inherited from the schema default (same reasoning as
            # mark_bill_paid and record_transaction's outflow path).
            "direction": "out",
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
               WHERE t.id != ? AND t.txn_date <= ? AND t.direction = 'out'
                   AND NOT EXISTS (
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
        # direction='out' explicit, not by column default: compute_balance
        # and spending_summary filter on it, so a bill payment's inclusion
        # in the balance and spend totals is stated here, not inherited
        # from a schema default a future migration could change unseen.
        cur = db.execute(
            """INSERT INTO transactions
               (txn_date, amount_cents, description, category, paid_by,
                is_shared, source, direction)
               VALUES (?, ?, ?, ?, ?, ?, 'bill', 'out')""",
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


def create_bill(db, actor, data):
    """Create a recurring-bill definition (not a transaction — bills are
    what mark_bill_paid later turns into one).

    validate — name required; amount and due_day parsed together, frozen
    message on either failing to parse; then amount positive, then due_day
    in range — check order and messages are the deployed route's, frozen.
    edit — one transaction: the bill row and the audit row carrying the
    created shape.
    side effects — none yet.
    Returns the bill row.
    """
    with action_transaction(db):
        name = (data.get("name") or "").strip()
        if not name:
            raise ActionError("name is required")
        try:
            cents = to_cents(data.get("amount"))
            due_day = int(data.get("due_day"))
        except (ValueError, TypeError):
            raise ActionError("invalid amount or due day")
        if cents <= 0:
            raise ActionError("amount must be positive")
        if not (1 <= due_day <= 31):
            raise ActionError("due day must be between 1 and 31")
        category = (data.get("category") or "Bills").strip()[:60] or "Bills"
        cur = db.execute(
            "INSERT INTO bills (name, amount_cents, due_day, category) VALUES (?, ?, ?, ?)",
            (name[:100], cents, due_day, category),
        )
        bill = db.execute(
            "SELECT * FROM bills WHERE id = ?", (cur.lastrowid,)).fetchone()
        _write_audit(db, actor, "create_bill", f"bill:{bill['id']}",
                     {"bill": dict(bill)})
    return bill


def update_bill(db, actor, bill_id, data):
    """Edit a bill definition's fields (the deployed route's payload).

    validate — the bill exists (NotFound otherwise); name/category fall
    back to the existing row's when absent; amount/due_day parsed together
    (existing values used when absent from the payload), frozen message
    on either failing to parse; then one combined positive-amount-or-
    valid-range check — the deployed route uses a single message for both
    of those, distinct from create_bill's two separate messages, and that
    asymmetry is preserved rather than "fixed."
    edit — one transaction: the updated row and the audit row recording
    the before-image and the applied fields.
    side effects — none yet.
    Returns the updated bill row.
    """
    with action_transaction(db):
        existing = db.execute(
            "SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if existing is None:
            raise NotFound("not found")
        name = (data.get("name") or existing["name"]).strip()[:100]
        category = (data.get("category") or existing["category"]).strip()[:60]
        try:
            cents = to_cents(data["amount"]) if "amount" in data else existing["amount_cents"]
            due_day = int(data.get("due_day", existing["due_day"]))
        except (ValueError, TypeError):
            raise ActionError("invalid amount or due day")
        if cents <= 0 or not (1 <= due_day <= 31):
            raise ActionError("invalid amount or due day")
        db.execute(
            "UPDATE bills SET name = ?, amount_cents = ?, due_day = ?, category = ? "
            "WHERE id = ?",
            (name, cents, due_day, category, bill_id),
        )
        bill = db.execute(
            "SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
        _write_audit(db, actor, "update_bill", f"bill:{bill_id}",
                     {"before": dict(existing), "after": dict(bill)})
    return bill


def delete_bill(db, actor, bill_id):
    """Soft-delete a bill definition (bounded transition, matching
    delete_goal's posture — hard delete deferred to its own increment).

    validate — an active bill with this id exists (NotFound otherwise;
    already-inactive is indistinguishable from missing, matching the
    deployed route's rowcount check).
    edit — one transaction: active=0 and the audit row capturing the
    bill's state at the moment of deactivation. Past payments
    (bill_payments rows, their transactions) are untouched — deactivating
    a bill stops future occurrences, it does not erase history.
    side effects — none yet.
    """
    with action_transaction(db):
        bill = db.execute(
            "SELECT * FROM bills WHERE id = ? AND active = 1", (bill_id,)).fetchone()
        if bill is None:
            raise NotFound("not found")
        db.execute("UPDATE bills SET active = 0 WHERE id = ?", (bill_id,))
        _write_audit(db, actor, "delete_bill", f"bill:{bill_id}",
                     {"bill": dict(bill)})


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


# Ordered for the agent-tool enums (a presentation choice); the frozenset used for
# membership checks derives from it, so the offered vocabulary and the validated
# vocabulary can't drift. 'unclassified' is the ABSENCE of a tag — a valid stored
# value but never an assignable choice, so it stays out of the offered order.
REAL_INCOME_TYPE_ORDER = ("paycheck", "reimbursement", "refund",
                          "transfer", "gift", "other")
INCOME_TYPES = frozenset(REAL_INCOME_TYPE_ORDER) | {"unclassified"}


def classify_inflow(db, actor, txn_id, income_type):
    """Set an inflow's income_type — the tagging endpoint (INCOME-DESIGN's
    classification lifecycle: rules run first, this is how a human corrects
    or fills in what they left unclassified).

    validate — the row exists (NotFound otherwise); the row's direction is
    'in' (the registry's submission criterion — an outflow has nothing to
    classify); income_type is one of INCOME-DESIGN's vocabulary.
    edit — one transaction: the column update and the audit row recording
    the before/after value.
    side effects — none yet. The "make this a rule?" nudge is a UI-layer
    decision per the settled auto-rule-aggressiveness call (wait for a
    repeat match) — this verb only classifies the one row it's given.
    Returns the updated row.
    """
    with action_transaction(db):
        existing = db.execute(
            "SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if existing is None:
            raise NotFound("not found")
        if existing["direction"] != "in":
            raise ActionError("only inflows can be classified")
        if income_type not in INCOME_TYPES:
            raise ActionError(
                "income_type must be one of: " + ", ".join(sorted(INCOME_TYPES)))
        db.execute(
            "UPDATE transactions SET income_type = ? WHERE id = ?",
            (income_type, txn_id))
        row = db.execute(
            "SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        _write_audit(
            db, actor, "classify_inflow", f"transaction:{txn_id}",
            {"before": existing["income_type"], "after": income_type})
    return row


def set_transfer(db, actor, txn_id, is_transfer):
    """Mark a transaction as a transfer between the household's own accounts —
    or unmark it (transfer-neutral fix, increment T2).

    A transfer is NEITHER income NOR spend and is not part of who-owes-whom, so
    every money derivation excludes is_transfer=1 (added in T1). This is how a
    mis-signed row gets corrected: a credit-card 'Payment Thank You' posts as a
    POSITIVE amount, so sync files it as direction='in' and it reads as income —
    marking it a transfer takes it out of income; marking its funding leg (the
    checking-side debit) takes that out of spend.

    validate — the row exists (NotFound); it is NOT a settlement (a settlement
    IS the balance-zeroing mechanism, never a transfer). `is_transfer` is
    coerced to 0/1.
    edit — one transaction: the flag column + the audit row (before/after).
    Deliberately FLAG-ONLY — it does NOT touch split rows, so the action is
    fully reversible: unmark and the row returns to spend/income/balance
    exactly as before (the derivations simply stop ignoring it). A transfer
    keeping stale split rows is harmless because every money derivation filters
    is_transfer=1 out (T1).
    side effects — none.
    Returns the updated row."""
    flag = 1 if is_transfer else 0
    with action_transaction(db):
        existing = db.execute(
            "SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if existing is None:
            raise NotFound("not found")
        if existing["source"] == "settlement":
            raise ActionError("a settlement cannot be marked a transfer")
        db.execute(
            "UPDATE transactions SET is_transfer = ? WHERE id = ?", (flag, txn_id))
        row = db.execute(
            "SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        _write_audit(
            db, actor, "set_transfer", f"transaction:{txn_id}",
            {"before": existing["is_transfer"], "after": flag})
    return row


RULE_TYPES = INCOME_TYPES - {"unclassified"}
# A rule assigns a real type; "leave it unclassified" isn't something a
# rule needs to say — that's simply the state a non-match leaves a row in.


def _rule_account(external_id):
    """The SimpleFIN account id embedded in a sync row's external_id
    ('simplefin:<account>:<txn>') — the only place account identity lives
    today, since transactions carries no dedicated account column. None
    for a manual row or a malformed id."""
    if not external_id or not external_id.startswith("simplefin:"):
        return None
    parts = external_id.split(":", 2)
    return parts[1] if len(parts) >= 2 else None


def _rule_matches(rule, row):
    """A rule matches when every criterion it sets holds; unset criteria
    (NULL in the rule) pass freely. Criteria are ANDed, never ORed."""
    if rule["match_desc"] and rule["match_desc"].lower() not in row["description"].lower():
        return False
    if rule["match_account"] and _rule_account(row["external_id"]) != rule["match_account"]:
        return False
    if rule["min_cents"] is not None and row["amount_cents"] < rule["min_cents"]:
        return False
    if rule["max_cents"] is not None and row["amount_cents"] > rule["max_cents"]:
        return False
    return True


def _validate_income_rule(db, data):
    """Validate + normalize a rule payload, returning the canonical field
    dict (priority, match_desc, match_account, min_cents, max_cents,
    set_type, set_paid_by). Pure read (the conflict check queries but writes
    nothing), so both create_income_rule (inside its transaction) and
    propose_action (dry-run, before parking anything) share ONE validator —
    a proposed rule that would be rejected at create is rejected at propose,
    never a surprise at confirm.

    set_type is one of the real income types ('unclassified' excluded — a
    rule exists to assign a real type); at least one match criterion
    (description, account, or amount bounds) is required, else the rule would
    match every inflow; min_cents/max_cents, if both given, don't cross;
    set_paid_by, if given, is an active member; and no other ENABLED rule
    already has the exact same match criteria (the registry's submission
    criterion — a duplicate would make one of them permanently dead weight).
    """
    # A transfer rule (set_transfer=1) marks its matches as transfers
    # (is_transfer=1, migration #013). Since a transfer is neither income nor
    # spend, its income_type is irrelevant — so set_type defaults to 'transfer'
    # for a transfer rule, letting the caller create one with just a match
    # criterion + set_transfer. set_type still must be a real type either way.
    set_transfer = 1 if data.get("set_transfer") else 0
    set_type = data.get("set_type") or ("transfer" if set_transfer else None)
    if set_type not in RULE_TYPES:
        raise ActionError(
            "set_type must be one of: " + ", ".join(sorted(RULE_TYPES)))
    match_desc = (data.get("match_desc") or "").strip()[:200] or None
    # A 1-2 char match is too broad to be a safe rule (MIRAGE F3): applied
    # universally so a transfer rule (set_transfer=1) can't hide future
    # inflows from both income and spending behind a near-empty substring.
    if match_desc is not None and len(match_desc) < 3:
        raise ActionError("match_desc must be at least 3 characters")
    match_account = (data.get("match_account") or "").strip()[:100] or None
    min_cents = data.get("min_cents")
    max_cents = data.get("max_cents")
    for label, value in (("min_cents", min_cents), ("max_cents", max_cents)):
        if value is not None and not isinstance(value, int):
            raise ActionError(f"{label} must be an integer number of cents")
    if (match_desc is None and match_account is None
            and min_cents is None and max_cents is None):
        raise ActionError(
            "at least one match criterion (description, account, or "
            "amount bounds) is required")
    if min_cents is not None and max_cents is not None and min_cents > max_cents:
        raise ActionError("min_cents must not exceed max_cents")
    set_paid_by = data.get("set_paid_by")
    if (set_paid_by is not None
            and set_paid_by not in {m["id"] for m in active_members(db)}):
        raise ActionError("set_paid_by must be an active member")
    try:
        priority = int(data.get("priority", 0))
    except (TypeError, ValueError):
        raise ActionError("priority must be an integer")

    conflict = db.execute(
        """SELECT id FROM income_rules WHERE enabled = 1
           AND match_desc IS ? AND match_account IS ?
           AND min_cents IS ? AND max_cents IS ?""",
        (match_desc, match_account, min_cents, max_cents)).fetchone()
    if conflict:
        raise ActionError("a rule with these match criteria already exists")

    return {"priority": priority, "match_desc": match_desc,
            "match_account": match_account, "min_cents": min_cents,
            "max_cents": max_cents, "set_type": set_type,
            "set_paid_by": set_paid_by, "set_transfer": set_transfer}


def create_income_rule(db, actor, data):
    """Create an income-classification rule.

    validate — via _validate_income_rule (the shared validator; see there).
    edit — one transaction: the rule row and the audit row carrying the
    created shape.
    side effects — none directly. An agent caller reaches this two-phase
    through propose_action/confirm_action (the MCP write tier), never raw.
    Returns the rule row.
    """
    with action_transaction(db):
        f = _validate_income_rule(db, data)
        cur = db.execute(
            """INSERT INTO income_rules
               (priority, match_desc, match_account, min_cents, max_cents,
                set_type, set_paid_by, set_transfer, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f["priority"], f["match_desc"], f["match_account"], f["min_cents"],
             f["max_cents"], f["set_type"], f["set_paid_by"], f["set_transfer"],
             _now()))
        rule = db.execute(
            "SELECT * FROM income_rules WHERE id = ?", (cur.lastrowid,)).fetchone()
        _write_audit(db, actor, "create_income_rule", f"rule:{rule['id']}",
                     {"rule": dict(rule)})
    return rule


def set_rule_enabled(db, actor, rule_id, enabled):
    """Enable or disable a rule. No delete — disabled rules keep history,
    per the registry.

    validate — the rule exists (NotFound otherwise).
    edit — one transaction: the enabled flag and the audit row recording
    the before/after state.
    side effects — none yet.
    Returns the updated rule row.
    """
    with action_transaction(db):
        existing = db.execute(
            "SELECT * FROM income_rules WHERE id = ?", (rule_id,)).fetchone()
        if existing is None:
            raise NotFound("not found")
        db.execute(
            "UPDATE income_rules SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, rule_id))
        rule = db.execute(
            "SELECT * FROM income_rules WHERE id = ?", (rule_id,)).fetchone()
        _write_audit(db, actor, "set_rule_enabled", f"rule:{rule_id}",
                     {"before": bool(existing["enabled"]), "after": bool(rule["enabled"])})
    return rule


def _first_matching_rule(db, row, rules=None):
    """The first enabled rule (priority ascending, ties by id) that
    matches this row, or None. `row` needs only description, external_id,
    and amount_cents — a plain dict works as well as a real transactions
    Row, which is what lets record_transaction check a not-yet-inserted
    row before it decides the row's own income_type/paid_by. Pass a
    pre-fetched `rules` list to avoid re-querying per row in a batch."""
    if rules is None:
        rules = db.execute(
            "SELECT * FROM income_rules WHERE enabled = 1 "
            "ORDER BY priority ASC, id ASC").fetchall()
    for rule in rules:
        if _rule_matches(rule, row):
            return rule
    return None


def suggest_rule_after_classify(db, row):
    """The "make this a rule?" decision, deliberately kept out of
    classify_inflow so that verb stays a pure one-row edit (see its
    docstring). Read-only: returns a pre-filled rule suggestion for the UI,
    or None when no offer should be made.

    Offer exactly once per income_type — when tagging brings the count of
    inflows carrying a given real type to exactly two. That's the settled
    auto-rule-aggressiveness call: wait for a repeat match rather than fire
    on the first tag (robustness over fast convergence, per INCOME-DESIGN).
    Because the offer only fires at count == 2, a decline is a decline —
    the third same-type tag won't nag again.

    Suppressed when an enabled rule already matches this row: a rule already
    covers it, so a second would be permanent dead weight, and
    create_income_rule would reject the duplicate criteria anyway.

    match_desc is pre-filled from the description for the user to trim;
    set_type is the type just assigned. `row` is a classified inflow Row.
    """
    income_type = row["income_type"]
    if row["direction"] != "in" or income_type not in RULE_TYPES:
        return None
    count = db.execute(
        "SELECT COUNT(*) AS n FROM transactions "
        "WHERE direction = 'in' AND income_type = ?", (income_type,)).fetchone()["n"]
    if count != 2:
        return None
    if _first_matching_rule(db, row) is not None:
        return None
    return {"match_desc": (row["description"] or "").strip(),
            "set_type": income_type}


def suggest_transfer_rule_after_mark(db, row):
    """The "always treat this as a transfer?" nudge, the transfer-flag analog of
    suggest_rule_after_classify (T3b). Read-only: returns a pre-filled transfer-
    rule suggestion for the UI, or None when no offer should be made.

    Offer once, when marking brings the count of transfer INFLOWS to exactly two
    — the same wait-for-a-repeat aggressiveness the classify nudge uses (a
    recurring "Payment Thank You" earns a rule; a one-off transfer doesn't nag).
    Only inflows: rules run on the money-in path, so an outflow transfer can't
    be ruled. Suppressed when an enabled rule already matches the row (a rule
    covers it; a second would be dead weight and create_income_rule would reject
    the duplicate anyway). match_desc is pre-filled from the description for the
    user to trim to the recurring part; the suggestion carries set_transfer=1."""
    if row["direction"] != "in" or not row["is_transfer"]:
        return None
    count = db.execute(
        "SELECT COUNT(*) AS n FROM transactions "
        "WHERE direction = 'in' AND is_transfer = 1").fetchone()["n"]
    if count != 2:
        return None
    if _first_matching_rule(db, row) is not None:
        return None
    return {"match_desc": (row["description"] or "").strip(),
            "set_type": "transfer", "set_transfer": True}


def _matching_pass(db, rules=None):
    """Read-only: each rule (priority ascending, ties by id) tried in order
    against every unclassified inflow; first match wins. Defaults to all
    enabled rules; pass a single-element `rules` list to scope the pass to
    one rule (the new-rule-only application confirm_action needs)."""
    if rules is None:
        rules = db.execute(
            "SELECT * FROM income_rules WHERE enabled = 1 "
            "ORDER BY priority ASC, id ASC").fetchall()
    rows = db.execute(
        "SELECT * FROM transactions WHERE direction = 'in' "
        "AND income_type = 'unclassified' ORDER BY id").fetchall()
    matches = []
    for row in rows:
        rule = _first_matching_rule(db, row, rules)
        if rule is not None:
            matches.append({
                "transaction_id": row["id"], "rule_id": rule["id"],
                "set_type": rule["set_type"], "set_paid_by": rule["set_paid_by"],
                "set_transfer": rule["set_transfer"]})
    return matches


def _write_matches(db, actor, matches, action="apply_rules"):
    """Apply a computed match list: each matched row's income_type (and
    paid_by, when the rule sets an owner override), each winning rule's
    hit_count, and one audit row listing every reclassification — skipped if
    nothing matched, the same "a no-op isn't audited" discipline
    record_transaction's dedupe uses. Assumes an OPEN transaction (the
    caller owns the boundary): it writes, it does not begin or commit."""
    for match in matches:
        # A transfer rule (set_transfer=1) also flags the match is_transfer=1;
        # 0 on ordinary rules leaves it untouched. Column defaults to 0 in the
        # match dict's absence (pre-T3b rules), so this is a no-op until a
        # transfer rule exists.
        xfer = 1 if match.get("set_transfer") else 0
        if match["set_paid_by"] is not None:
            db.execute(
                "UPDATE transactions SET income_type = ?, paid_by = ?, "
                "is_transfer = ? WHERE id = ?",
                (match["set_type"], match["set_paid_by"], xfer, match["transaction_id"]))
        else:
            db.execute(
                "UPDATE transactions SET income_type = ?, is_transfer = ? WHERE id = ?",
                (match["set_type"], xfer, match["transaction_id"]))
        db.execute(
            "UPDATE income_rules SET hit_count = hit_count + 1 WHERE id = ?",
            (match["rule_id"],))
    if matches:
        _write_audit(db, actor, action, None, {"applied": matches})


def apply_rules(db, actor, dry_run=False):
    """Re-run enabled rules over every unclassified inflow, in priority
    order; first match wins; no match leaves a row unclassified.

    validate — none.
    edit — skipped entirely when dry_run (a pure preview, no transaction
    opened at all). Otherwise, one transaction via _write_matches.
    side effects — none yet.
    Returns the list of {transaction_id, rule_id, set_type, set_paid_by}
    — a preview under dry_run, the applied changes otherwise; empty if
    nothing matched.
    """
    if dry_run:
        return _matching_pass(db)

    with action_transaction(db):
        matches = _matching_pass(db)
        _write_matches(db, actor, matches)
    return matches


def _apply_single_rule(db, actor, rule):
    """Apply exactly one already-created, enabled rule to the current
    unclassified inflows — the new-rule-only scope a confirmed rule proposal
    uses (see confirm_action). Filtering the pass to this one rule is what
    makes the confirmed effect equal the previewed would_match_now count,
    never a wider full-queue apply_rules that could sweep in rows an older
    enabled rule happens to match. Returns the applied matches."""
    with action_transaction(db):
        matches = _matching_pass(db, rules=[rule])
        _write_matches(db, actor, matches)
    return matches


# ──────────────── two-phase agent writes (AGENT-DESIGN step 4) ───────────

PROPOSABLE_ACTIONS = frozenset({"create_rule", "apply_rules"})
PENDING_ACTION_TTL_SECONDS = 86400   # 24h. Was 10 min, sized for the old flow
# where the proposer confirmed in the same conversation. MIRAGE F-1 made approval
# a separate human-in-the-app step, and the change-notification digest/alert
# (NOTIFICATIONS-DESIGN) tells a person out-of-band — so a proposal must live
# long enough to notice a notification and act. A stale approval still dies.

# A rule is "broad" when the ONLY thing narrowing it is a short description
# substring — no amount bounds, no account. Such a rule can read "0 matches
# now" yet silently classify FUTURE inflows the human never pictured. These
# floors drive an ADVISORY warning in the preview; they are NOT a hard block
# (that is the >= 3 min-length guard in _validate_income_rule). A transfer
# rule uses the stricter floor: its matches leave BOTH income and spending, so
# a future paycheck swept in by a broad transfer rule vanishes from every total
# — worth warning on longer-but-still-broad matches. Mirror in
# static/render.js (ruleBreadthWarning) for the in-app suggested-rule dialog.
BROAD_MATCH_MIN_LEN = 5           # income rule: a lone desc shorter than this
BROAD_TRANSFER_MATCH_MIN_LEN = 8  # transfer rule: hides money both ways


def rule_breadth_warning(fields):
    """Advisory: return a human-readable caution when `fields` describes a
    BROAD rule (a short description substring with nothing else narrowing it),
    else None. Advisory only — never rejects; fires even when nothing matches
    today, because the current-queue count says nothing about future inflows.
    Shared by the preview and any caller that wants the same nudge."""
    match_desc = (fields.get("match_desc") or "").strip()
    has_bounds = (fields.get("min_cents") is not None
                  or fields.get("max_cents") is not None)
    has_account = bool(fields.get("match_account"))
    is_transfer = bool(fields.get("set_transfer"))
    # An account or amount bounds genuinely constrain the rule; and a rule with
    # no description at all matches on those instead — none of these is the
    # short-substring hazard this warns about.
    if has_bounds or has_account or not match_desc:
        return None
    floor = BROAD_TRANSFER_MATCH_MIN_LEN if is_transfer else BROAD_MATCH_MIN_LEN
    if len(match_desc) >= floor:
        return None
    if is_transfer:
        return (
            f"Broad transfer rule: '{match_desc}' is short and nothing else "
            "narrows it, so it may also mark FUTURE deposits — including a "
            "paycheck next cycle — as transfers, hiding them from both income "
            "and spending. Consider a longer, more specific phrase.")
    return (
        f"Broad rule: '{match_desc}' is short and nothing else narrows it, so "
        "it will also classify FUTURE inflows whose description happens to "
        "contain that text. Consider a longer, more specific phrase.")


def _preview_create_rule(db, fields):
    """The dry-run a rule proposal shows the human: how many unclassified
    inflows this rule would match NOW (`would_match_now` + up to 5
    `sample_rows`), which existing enabled rules ALSO match those rows
    (`conflicts` — overlap to surface, distinct from the hard duplicate-
    criteria conflict _validate_income_rule already rejects), and a plain
    sentence of the forward effect. Money is integer cents here; the
    presenting layer (route / MCP tool) adds display strings at the edge.
    Read-only."""
    unclassified = db.execute(
        "SELECT * FROM transactions WHERE direction = 'in' "
        "AND income_type = 'unclassified' ORDER BY id").fetchall()
    matched = [row for row in unclassified if _rule_matches(fields, row)]
    enabled = db.execute(
        "SELECT * FROM income_rules WHERE enabled = 1 "
        "ORDER BY priority ASC, id ASC").fetchall()
    conflicts = [
        {"transaction_id": row["id"], "rule_id": er["id"],
         "set_type": er["set_type"]}
        for row in matched for er in enabled if _rule_matches(er, row)]
    sample_rows = [
        {"id": row["id"], "date": row["txn_date"],
         "description": row["description"], "amount_cents": row["amount_cents"]}
        for row in matched[:5]]
    warning = rule_breadth_warning(fields)
    return {
        "kind": "create_rule",
        "would_match_now": len(matched),
        "sample_rows": sample_rows,
        "conflicts": conflicts,
        "set_type": fields["set_type"],
        "future_effect": (
            "future inflows matching this rule are classified as "
            f"'{fields['set_type']}'"),
        # Advisory breadth nudge (F3): flags a short-substring rule that will
        # sweep in future inflows even when would_match_now is 0. Non-blocking.
        "broad": warning is not None,
        "warning": warning,
    }


def propose_action(db, actor, created_by, action_type, payload):
    """PHASE 1 of the agent write tier's two-phase choreography. Does NOT
    execute anything: it validates + dry-runs the action, then parks a
    pending_actions row with the FROZEN payload and the computed preview,
    and returns a single-use confirmation token. No audit row — nothing has
    been written to the financial tables (audit_log is executed writes only).

    validate — action_type is proposable; for 'create_rule', the payload
    passes _validate_income_rule (same validator create runs, so a rule that
    would be rejected at create is rejected here, not at confirm).
    edit — one transaction inserting the pending_actions row.
    Returns {confirmation_token, action_type, preview, expires_at}.
    """
    if action_type not in PROPOSABLE_ACTIONS:
        raise ActionError(
            "action_type must be one of: " + ", ".join(sorted(PROPOSABLE_ACTIONS)))
    payload = payload or {}

    if action_type == "create_rule":
        fields = _validate_income_rule(db, payload)
        also_apply = bool(payload.get("also_apply_to_existing", True))
        frozen = dict(fields)
        frozen["also_apply_to_existing"] = also_apply
        preview = _preview_create_rule(db, fields)
        preview["also_apply_to_existing"] = also_apply
    else:  # apply_rules
        frozen = {}
        matches = _matching_pass(db)
        by_rule = {}
        for match in matches:
            by_rule[match["rule_id"]] = by_rule.get(match["rule_id"], 0) + 1
        preview = {
            "kind": "apply_rules",
            "rows_affected": len(matches),
            "by_rule": [{"rule_id": rid, "count": count}
                        for rid, count in sorted(by_rule.items())],
        }

    token = secrets.token_urlsafe(24)
    now_dt = datetime.now(timezone.utc)
    created = now_dt.isoformat(timespec="seconds")
    expires = (now_dt + timedelta(seconds=PENDING_ACTION_TTL_SECONDS)
               ).isoformat(timespec="seconds")
    with action_transaction(db):
        db.execute(
            """INSERT INTO pending_actions
               (token, action_type, payload_json, preview_json, created_by,
                created_at, expires_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (token, action_type, json.dumps(frozen, sort_keys=True),
             json.dumps(preview, sort_keys=True), created_by, created, expires))
    return {"confirmation_token": token, "action_type": action_type,
            "preview": preview, "expires_at": expires}


def confirm_action(db, actor, token):
    """PHASE 2. Executes exactly the frozen payload the token points to — the
    parked args, never the caller's. Single-use; refused if already
    consumed, or past its expiry.

    Ordering (see AGENT-DESIGN's flagged friction): the pending row is
    claimed — flipped 'pending'→'confirmed' — in its OWN committed
    transaction FIRST, because the underlying verbs open their own
    BEGIN IMMEDIATE and refuse a pre-open one. The claim is conditional on
    status still being 'pending' (rowcount 0 → someone else won the race, or
    it was already consumed), so a re-confirm can never double-execute. A
    crash between the claim and the dispatch drops the write rather than
    repeating it — the safe direction; the human re-proposes.

    For 'create_rule' the frozen payload carries also_apply_to_existing: on
    True, after creating the rule the verb classifies the CURRENT unclassified
    inflows that this new rule matches (new-rule-only, via _apply_single_rule),
    so the effect equals the previewed count. Each underlying verb writes its
    own audit row — that is the executed-write record.
    Returns what was executed + the pending action id.
    """
    # MIRAGE F-1 backstop (defense-in-depth beneath the route's session gate):
    # confirming a two-phase proposal is a HUMAN act. actor is 'mcp:<label>' for
    # a bearer/MCP caller, 'ui:<name>' for a session — refuse an mcp: actor in
    # the money layer, so no automation can self-approve even if it reaches the
    # verb directly.
    if str(actor).startswith("mcp:"):
        raise ActionError(
            "a proposed action must be confirmed by a signed-in person in the "
            "app, not by an automation")
    row = db.execute(
        "SELECT * FROM pending_actions WHERE token = ?", (token,)).fetchone()
    if row is None:
        raise NotFound("not found")
    if row["status"] != "pending":
        raise ActionError(f"this proposal is already {row['status']}")
    if _now() > row["expires_at"]:
        with action_transaction(db):
            db.execute(
                "UPDATE pending_actions SET status = 'expired' "
                "WHERE id = ? AND status = 'pending'", (row["id"],))
        raise ActionError("this proposal has expired; propose again")

    with action_transaction(db):
        claimed = db.execute(
            "UPDATE pending_actions SET status = 'confirmed' "
            "WHERE id = ? AND status = 'pending'", (row["id"],)).rowcount
    if not claimed:
        raise ActionError("this proposal is already confirmed")

    payload = json.loads(row["payload_json"])
    if row["action_type"] == "create_rule":
        also_apply = payload.pop("also_apply_to_existing", True)
        rule = create_income_rule(db, actor, payload)
        applied = _apply_single_rule(db, actor, rule) if also_apply else []
        return {"action_type": "create_rule", "rule": dict(rule),
                "applied": applied, "pending_action_id": row["id"]}
    if row["action_type"] == "apply_rules":
        applied = apply_rules(db, actor, dry_run=False)
        return {"action_type": "apply_rules", "applied": applied,
                "pending_action_id": row["id"]}
    raise ActionError(f"unknown action_type: {row['action_type']}")


# ─────────────────────────── api tokens (AGENT-DESIGN step 1) ────────────

API_TOKEN_SCOPES = frozenset({"read", "read,write"})


def _hash_token(token):
    """The stored form of a bearer token: its SHA-256 hex. The token is
    high-entropy, so a fast hash is right — this is not a password."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_api_token(db, actor, data):
    """Issue a revocable bearer token for a non-browser client (the tailnet
    MCP server, any future API client).

    validate — label non-empty; user_id is an active member (per-person
    tokens); scopes is 'read' or 'read,write'.
    edit — one transaction: the token row (storing ONLY the SHA-256 hash,
    never the token) + an audit row recording label/user/scopes (never the
    token or its hash).
    Returns a dict of the row PLUS 'token' — the plaintext, returned exactly
    once; it can never be recovered after this call.
    """
    with action_transaction(db):
        label = (data.get("label") or "").strip()[:100]
        if not label:
            raise ActionError("label is required")
        user_id = data.get("user_id")
        if user_id not in {m["id"] for m in active_members(db)}:
            raise ActionError("user_id must be an active member")
        scopes = data.get("scopes", "read")
        if scopes not in API_TOKEN_SCOPES:
            raise ActionError("scopes must be 'read' or 'read,write'")
        token = secrets.token_urlsafe(32)
        cur = db.execute(
            "INSERT INTO api_tokens (token_hash, label, user_id, scopes, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (_hash_token(token), label, user_id, scopes, _now()))
        row = db.execute(
            "SELECT * FROM api_tokens WHERE id = ?", (cur.lastrowid,)).fetchone()
        _write_audit(db, actor, "create_api_token", f"api_token:{row['id']}",
                     {"label": label, "user_id": user_id, "scopes": scopes})
    result = dict(row)
    result["token"] = token   # plaintext — shown once, never stored
    return result


def revoke_api_token(db, actor, token_id):
    """Revoke a token. No delete — a revoked row keeps its audit trail and
    stops matching immediately.

    validate — the token exists (NotFound otherwise).
    edit — one transaction: set revoked=1 + an audit row. Idempotent.
    Returns the updated row.
    """
    with action_transaction(db):
        existing = db.execute(
            "SELECT * FROM api_tokens WHERE id = ?", (token_id,)).fetchone()
        if existing is None:
            raise NotFound("not found")
        db.execute("UPDATE api_tokens SET revoked = 1 WHERE id = ?", (token_id,))
        _write_audit(db, actor, "revoke_api_token", f"api_token:{token_id}",
                     {"label": existing["label"],
                      "was_revoked": bool(existing["revoked"])})
    return db.execute("SELECT * FROM api_tokens WHERE id = ?", (token_id,)).fetchone()


def find_active_api_token(db, token):
    """Resolve a bearer token to its row, or None if unknown or revoked, and
    bump last_used_at. Not a verb — it's an auth-layer read with an
    observability touch, not a financial mutation; it lives here only so the
    raw write to api_tokens stays inside actions.py."""
    row = db.execute(
        "SELECT * FROM api_tokens WHERE token_hash = ? AND revoked = 0",
        (_hash_token(token),)).fetchone()
    if row is None:
        return None
    db.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (_now(), row["id"]))
    db.commit()
    return row


# ─────────────────────── inventory (INVENTORY-DESIGN, the pantry) ─────────

# Ordered for the agent-tool enums; the frozensets (membership) derive from the
# order, so the offered and validated vocabularies stay one source.
ITEM_KIND_ORDER = ("staple", "oneoff")
ITEM_KINDS = frozenset(ITEM_KIND_ORDER)
ITEM_STATUS_ORDER = ("stocked", "low", "out", "ordered")   # severity order, then
# 'ordered' = bought (online), not yet arrived — off the list, not yet stocked (#015)
ITEM_STATUSES = frozenset(ITEM_STATUS_ORDER)


def add_item(db, actor, data):
    """Add a household item — a tracked staple or a one-off shopping need.

    validate — name non-empty; kind ∈ {staple, oneoff}; status ∈ the 3-state
    vocab; category/note optional. A one-off defaults to 'out' (it IS a need);
    a staple defaults to 'stocked'.
    edit — one transaction: the row + an audit row carrying the created shape.
    Never touches money. Returns the row.
    """
    with action_transaction(db):
        # H-4: a non-string name (e.g. a number from an API/MCP caller) hit
        # .strip() and raised AttributeError → a 500. Reject it as a clean 400,
        # mirroring the B3 "category must be text" hardening in set_budget.
        raw_name = data.get("name")
        if raw_name is not None and not isinstance(raw_name, str):
            raise ActionError("name must be text")
        name = (raw_name or "").strip()[:100]
        if not name:
            raise ActionError("name is required")
        kind = data.get("kind") or "staple"
        if kind not in ITEM_KINDS:
            raise ActionError("kind must be 'staple' or 'oneoff'")
        status = data.get("status") or ("out" if kind == "oneoff" else "stocked")
        if status not in ITEM_STATUSES:
            raise ActionError(
                "status must be one of: " + ", ".join(sorted(ITEM_STATUSES)))
        category = (data.get("category") or "").strip()[:60] or None
        note = (data.get("note") or "").strip()[:200] or None
        # Optional restock-match override (INVENTORY-DESIGN step 5); NULL = the
        # restock_suggestions derivation falls back to the item name.
        restock_match = (data.get("restock_match") or "").strip()[:200] or None
        now = _now()
        # A stocked add IS a "just full" moment, so it anchors the manual restock
        # cadence (restock_forecast counts N days from last_stocked_at); a low/out
        # add leaves the anchor NULL until the item is first marked stocked.
        last_stocked_at = now if status == "stocked" else None
        cur = db.execute(
            "INSERT INTO items (name, category, kind, status, note, restock_match, "
            "last_stocked_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, category, kind, status, note, restock_match, last_stocked_at, now, now))
        row = db.execute("SELECT * FROM items WHERE id = ?", (cur.lastrowid,)).fetchone()
        _write_audit(db, actor, "add_item", f"item:{row['id']}", {"item": dict(row)})
    return row


def set_item_status(db, actor, item_id, status):
    """Set an item's status — the routine interaction (mark low/out/stocked).
    A direct write in the agent tiers (logged, reversible), like classify_inflow.

    validate — the item exists and is active (NotFound otherwise); status is in
    the vocab.
    edit — one transaction: the status + updated_at. A ONE-OFF set to 'stocked'
    (i.e. bought) archives itself (active=0) so it leaves the shopping list.
    'ordered' (#015) is the in-between: bought but not arrived — off the list,
    not stocked; set 'stocked' on arrival (a one-off then archives as bought)
    or 'out' if it never came. updated_at doubles as its ordered-at for the
    view's "still waiting?" nudge.
    side effects — none. Returns the updated row.
    """
    with action_transaction(db):
        existing = db.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if existing is None or not existing["active"]:
            raise NotFound("not found")
        if status not in ITEM_STATUSES:
            raise ActionError(
                "status must be one of: " + ", ".join(sorted(ITEM_STATUSES)))
        row = _apply_item_status(db, actor, "set_item_status", existing, status)
    return row


def _apply_item_status(db, actor, verb, existing, status):
    """The one status edit, shared by set_item_status and restock_items: the
    UPDATE plus its per-item audit row (attributed to `verb`), inside the
    CALLER's transaction. A one-off set 'stocked' (bought) archives itself;
    'stocked' re-anchors last_stocked_at (the manual-cadence anchor), low/out
    leaves it where it was. Validation stays with each caller."""
    archived = existing["kind"] == "oneoff" and status == "stocked"
    now = _now()
    last_stocked_at = now if status == "stocked" else existing["last_stocked_at"]
    db.execute(
        "UPDATE items SET status = ?, updated_at = ?, active = ?, "
        "last_stocked_at = ? WHERE id = ?",
        (status, now, 0 if archived else 1, last_stocked_at, existing["id"]))
    _write_audit(db, actor, verb, f"item:{existing['id']}",
                 {"before": existing["status"], "after": status,
                  "archived": bool(archived)})
    return db.execute(
        "SELECT * FROM items WHERE id = ?", (existing["id"],)).fetchone()


def restock_items(db, actor, item_ids):
    """Mark a bought set of items stocked in ONE action — the trip verb
    (INVENTORY-DESIGN, Pantry v2 amendment increment 1): one grocery run home,
    one call clears the shopping list instead of a tap per item. Batch pantry
    convenience only — never money.

    validate — item_ids is a non-empty list of item ids (a bounded batch, ≤ 100);
    EVERY item must exist and be active before anything changes (all-or-nothing,
    so a stale id can't half-apply a trip). Duplicates collapse.
    edit — one transaction: each item gets the same edit set_item_status gives
    a 'stocked' mark (shared helper) — a one-off archives itself (bought → off
    the list), last_stocked_at re-anchors — and its OWN audit row under this
    verb, so the log stays per-item attributable.
    Returns the updated rows, in the order given.
    """
    with action_transaction(db):
        if not isinstance(item_ids, (list, tuple)) or not item_ids:
            raise ActionError("item_ids must be a non-empty list of item ids")
        try:
            ids = list(dict.fromkeys(int(i) for i in item_ids))
        except (TypeError, ValueError):
            raise ActionError("item_ids must be a non-empty list of item ids")
        if len(ids) > 100:
            raise ActionError("too many items in one restock (max 100)")
        existing = {}
        for item_id in ids:                       # validate ALL before ANY edit
            row = db.execute(
                "SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None or not row["active"]:
                raise NotFound(f"item {item_id} not found")
            existing[item_id] = row
        rows = [_apply_item_status(db, actor, "restock_items",
                                   existing[item_id], "stocked")
                for item_id in ids]
    return rows


def archive_item(db, actor, item_id):
    """Remove an item from the pantry — soft delete (active=0), matching
    delete_goal/delete_bill's bounded posture; history stays attributable.

    validate — the item exists (NotFound otherwise).
    edit — one transaction: active=0 + an audit row. Idempotent.
    Returns the updated row.
    """
    with action_transaction(db):
        existing = db.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if existing is None:
            raise NotFound("not found")
        db.execute(
            "UPDATE items SET active = 0, updated_at = ? WHERE id = ?",
            (_now(), item_id))
        _write_audit(db, actor, "archive_item", f"item:{item_id}",
                     {"name": existing["name"], "was_active": bool(existing["active"])})
    return db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def set_budget(db, actor, data):
    """Set (or change) a category's monthly spending limit — Analytics Tier C
    (BUDGETS-DESIGN). Upserts ONE row per category: a new category inserts, an
    existing one has its amount replaced, and a previously removed one
    reactivates (ON CONFLICT(category) DO UPDATE, active=1). A budget is a
    category limit, not a transaction — it never touches money.

    validate — category non-empty; amount positive (integer cents via to_cents).
    edit — one transaction: the upsert + an audit row carrying before/after.
    Returns the budget row.
    """
    with action_transaction(db):
        raw_category = data.get("category")
        # A non-string category (e.g. a JSON number) would blow up .strip()
        # with an AttributeError → a generic 500 (MIRAGE B3 LOW-2). Reject it
        # as a clean validation error instead. None is allowed through — it
        # becomes the "category is required" message below.
        if raw_category is not None and not isinstance(raw_category, str):
            raise ActionError("category must be text")
        category = (raw_category or "").strip()[:60]
        if not category:
            raise ActionError("category is required")
        try:
            cents = to_cents(data.get("amount"))
        except OverflowError:
            # Infinity → OverflowError inside to_cents' int() conversion.
            raise ActionError("amount is too large")
        except (InvalidOperation, ValueError, TypeError):
            raise ActionError("invalid amount")
        if cents <= 0:
            raise ActionError("amount must be positive")
        # A huge finite amount (e.g. 1e17) parses fine but overflows SQLite's
        # signed-64-bit integer column at bind time → a generic 500 (MIRAGE
        # B3 LOW-1). Cap it as a clean validation error; the ceiling is the
        # storage limit, not a business judgment.
        if cents > _MAX_MONEY_CENTS:
            raise ActionError("amount is too large")
        before = db.execute(
            "SELECT * FROM budgets WHERE category = ?", (category,)).fetchone()
        now = _now()
        db.execute(
            "INSERT INTO budgets (category, amount_cents, created_at, updated_at, active) "
            "VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(category) DO UPDATE SET "
            "amount_cents = excluded.amount_cents, active = 1, updated_at = excluded.updated_at",
            (category, cents, now, now))
        row = db.execute(
            "SELECT * FROM budgets WHERE category = ?", (category,)).fetchone()
        _write_audit(db, actor, "set_budget", f"budget:{row['id']}",
                     {"before": (dict(before) if before else None), "after": dict(row)})
    return row


def remove_budget(db, actor, budget_id):
    """Remove a category's budget — a soft delete (active=0), matching
    delete_bill/archive_item's bounded posture; the audit history is untouched.
    Never touches money.

    validate — the budget exists and is active (NotFound otherwise).
    edit — one transaction: active=0 + updated_at + an audit row (before-image).
    Returns the updated row.
    """
    with action_transaction(db):
        existing = db.execute(
            "SELECT * FROM budgets WHERE id = ?", (budget_id,)).fetchone()
        if existing is None or not existing["active"]:
            raise NotFound("not found")
        db.execute("UPDATE budgets SET active = 0, updated_at = ? WHERE id = ?",
                   (_now(), budget_id))
        _write_audit(db, actor, "remove_budget", f"budget:{budget_id}",
                     {"before": dict(existing)})
    return db.execute("SELECT * FROM budgets WHERE id = ?", (budget_id,)).fetchone()


PASSWORD_MIN_LEN = 8   # the same floor the first-run setup enforces


def change_password(db, actor, member_id, current_password, new_password):
    """Let a member change their OWN password — the first write verb over
    `members` (until now the table had no write path past first-run setup).

    validate — the member exists and is active (NotFound otherwise); the
    current password verifies against the stored hash (a wrong one is an
    ActionError the route rate-limits, so this can't become a guessing
    oracle); the new password is >= PASSWORD_MIN_LEN chars and differs from
    the current one.
    edit — one transaction: the new werkzeug hash replaces the old; an audit
    row records THAT it changed and nothing else — never a password, never
    a hash (the log is readable by both members and the agents).
    side effects — none. Sessions are signed cookies with no server-side
    store, so other devices stay signed in until their cookie expires;
    revoking sessions on change is a later increment if wanted.
    Returns {"member_id": id}.
    """
    with action_transaction(db):
        row = db.execute(
            "SELECT id, password_hash, active FROM members WHERE id = ?",
            (member_id,)).fetchone()
        if row is None or not row["active"]:
            raise NotFound("not found")
        if not check_password_hash(row["password_hash"], current_password or ""):
            raise ActionError("current password is wrong")
        new_password = new_password or ""
        if len(new_password) < PASSWORD_MIN_LEN:
            raise ActionError(f"new password must be at least {PASSWORD_MIN_LEN} characters")
        if check_password_hash(row["password_hash"], new_password):
            raise ActionError("new password must differ from the current one")
        db.execute("UPDATE members SET password_hash = ? WHERE id = ?",
                   (generate_password_hash(new_password), member_id))
        _write_audit(db, actor, "change_password", f"member:{member_id}",
                     {"member_id": member_id})   # deliberately no secret material
    return {"member_id": member_id}


# The system category settle_up stamps on settlement rows; merge_category
# refuses it on either side so settlements stay separately identifiable.
PROTECTED_CATEGORIES = frozenset({"Settlement"})


def merge_category(db, actor, data):
    """Delete/merge/rename a category by moving EVERYTHING that carries it —
    categories are emergent transaction tags (no categories table), so "delete"
    can only safely mean "relabel every reference, atomically". A non-empty
    category cannot simply vanish: the caller must name where its contents go,
    which is exactly what prevents orphans (a budget, bill, or pantry item
    pointing at a category no transaction carries).

    validate — from_category and into_category non-empty (into normalized like
    every category: strip, ≤ 60 chars), different from each other, neither
    'Settlement' (the system category settlements are found by); from_category
    must actually be referenced somewhere (else there is nothing to delete).
    edit — ONE transaction, all-or-nothing, across every month and direction:
      * transactions.category from → into (amounts, splits, dates untouched —
        a relabel changes no money number);
      * bills.category and items.category from → into (items.updated_at is
        deliberately NOT bumped: restock_suggestions uses it as its
        since-the-item-last-changed bound, and a relabel is not a stock event);
      * budgets: if `from` has a budget and `into` has none, the budget row
        follows the merge (its category is renamed — the limit survives); if
        `into` already has its own row, `from`'s is retired (active=0,
        reversible via set_budget) so the UNIQUE(category) invariant and
        `into`'s own limit both hold.
    One audit row records the into-target and every count. Renaming a category
    is the same move with a fresh name. Never touches money.
    Returns {"into", "transactions", "bills", "items", "budget"}.
    """
    with action_transaction(db):
        src = (data.get("from_category") or "").strip()[:60]
        dst = (data.get("into_category") or "").strip()[:60]
        if not src or not dst:
            raise ActionError("from_category and into_category are required")
        if src == dst:
            raise ActionError("that's already this category")
        if src in PROTECTED_CATEGORIES or dst in PROTECTED_CATEGORIES:
            raise ActionError("the Settlement category is system-managed")
        counts = {
            "transactions": db.execute(
                "SELECT COUNT(*) FROM transactions WHERE category = ?",
                (src,)).fetchone()[0],
            "bills": db.execute(
                "SELECT COUNT(*) FROM bills WHERE category = ?",
                (src,)).fetchone()[0],
            "items": db.execute(
                "SELECT COUNT(*) FROM items WHERE category = ?",
                (src,)).fetchone()[0],
        }
        src_budget = db.execute(
            "SELECT * FROM budgets WHERE category = ?", (src,)).fetchone()
        if not any(counts.values()) and src_budget is None:
            raise ActionError(f"nothing is tagged '{src}'")
        db.execute("UPDATE transactions SET category = ? WHERE category = ?",
                   (dst, src))
        db.execute("UPDATE bills SET category = ? WHERE category = ?", (dst, src))
        db.execute("UPDATE items SET category = ? WHERE category = ?", (dst, src))
        budget_outcome = "none"
        if src_budget is not None:
            dst_budget = db.execute(
                "SELECT * FROM budgets WHERE category = ?", (dst,)).fetchone()
            if dst_budget is None:
                db.execute("UPDATE budgets SET category = ?, updated_at = ? "
                           "WHERE id = ?", (dst, _now(), src_budget["id"]))
                budget_outcome = "moved"
            else:
                db.execute("UPDATE budgets SET active = 0, updated_at = ? "
                           "WHERE id = ?", (_now(), src_budget["id"]))
                budget_outcome = "retired"
        result = {"into": dst, "budget": budget_outcome, **counts}
        _write_audit(db, actor, "merge_category", f"category:{src}", result)
    return result


RESET_CONFIRM_PHRASE = "reset all money rows"

# The money-movement tables reset_money clears, in FK-safe order. Everything
# else — members, splits config (none: splits IS money-derived so it clears),
# bills, goals, budgets, income_rules (rows kept, hit_count zeroed), items,
# api_tokens, and audit_log (the system of record) — survives untouched.
RESET_TABLES = ("links", "splits", "bill_payments", "goal_contributions",
                "pending_actions", "transactions")


def reset_money(db, actor, confirm):
    """The fresh-start wipe (Aug 2026): clear every money-movement row while
    keeping the household's configured structure — members, bills, goals,
    budgets, income rules, pantry items, api tokens — byte-untouched, so the
    ledger reads $0/empty and fills forward from the next bank sync.

    Deliberately has NO route: unreachable from the UI, MCP, or Ask. Run by
    hand (CLI) on the Pi, against a fresh backup, per deploy/reset-money.md.

    validate — `confirm` must equal RESET_CONFIRM_PHRASE exactly (a wrong or
    missing phrase deletes nothing).
    edit — one transaction: DELETE the RESET_TABLES in FK-safe order, zero
    income_rules.hit_count (rules themselves survive), and ONE audit row
    carrying each table's before-count — the wipe is itself part of the
    record it preserves.
    Returns {table: rows_deleted} plus rules_hit_count_zeroed.
    """
    if confirm != RESET_CONFIRM_PHRASE:
        raise ActionError(
            f"refusing: confirm must be the exact phrase {RESET_CONFIRM_PHRASE!r}")
    with action_transaction(db):
        before = {t: db.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
                  for t in RESET_TABLES}
        rules_hit = db.execute(
            "SELECT COUNT(*) AS c FROM income_rules WHERE hit_count != 0"
        ).fetchone()["c"]
        for t in RESET_TABLES:
            db.execute(f"DELETE FROM {t}")
        db.execute("UPDATE income_rules SET hit_count = 0")
        _write_audit(db, actor, "reset_money", "ledger:money",
                     {"cleared": before, "rules_hit_count_zeroed": rules_hit})
    return {**before, "rules_hit_count_zeroed": rules_hit}


def set_item_match(db, actor, item_id, restock_match):
    """Set or clear a staple's restock-match phrase — the OPTIONAL override that
    ties a purchase's description to this item for restock suggestions
    (INVENTORY-DESIGN step 5). Blank/whitespace clears it (NULL), so detection
    falls back to the item's own name (the auto-guess). Never touches money.

    validate — the item exists and is active (NotFound otherwise).
    edit — one transaction: restock_match + updated_at + an audit row carrying
    before/after. Returns the updated row.
    """
    with action_transaction(db):
        existing = db.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if existing is None or not existing["active"]:
            raise NotFound("not found")
        phrase = (restock_match or "").strip()[:200] or None
        db.execute(
            "UPDATE items SET restock_match = ?, updated_at = ? WHERE id = ?",
            (phrase, _now(), item_id))
        _write_audit(db, actor, "set_item_match", f"item:{item_id}",
                     {"before": existing["restock_match"], "after": phrase})
    return db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def set_item_interval(db, actor, item_id, days):
    """Set or clear a staple's manual restock cadence — the "remind me every N
    days" a person sets directly (INVENTORY-DESIGN step 5). When set,
    restock_forecast counts N days from last_stocked_at instead of inferring the
    interval from purchase history; None/blank clears it, so the forecast falls
    back to the inferred median cadence. Never touches money.

    validate — the item exists, is active, and is a staple (a one-off archives
    itself on purchase, so a cadence is meaningless there — NotFound / ActionError
    otherwise); days is None (clear) or a positive integer within a sane bound.
    edit — one transaction: restock_interval_days + updated_at + an audit row
    carrying before/after. Returns the updated row.
    """
    with action_transaction(db):
        existing = db.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if existing is None or not existing["active"]:
            raise NotFound("not found")
        if existing["kind"] != "staple":
            raise ActionError("a restock cadence only applies to a staple")
        interval = _clean_interval_days(days)
        db.execute(
            "UPDATE items SET restock_interval_days = ?, updated_at = ? WHERE id = ?",
            (interval, _now(), item_id))
        _write_audit(db, actor, "set_item_interval", f"item:{item_id}",
                     {"before": existing["restock_interval_days"], "after": interval})
    return db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def _clean_interval_days(days):
    """Validate a manual restock cadence: None/empty clears it; otherwise a
    positive integer within [1, 365] (a year is the longest cadence worth a
    reminder). Rejects zero, negatives, and non-integers."""
    if days is None or days == "":
        return None
    try:
        n = int(days)
    except (TypeError, ValueError):
        raise ActionError("interval must be a whole number of days")
    if not 1 <= n <= 365:
        raise ActionError("interval must be between 1 and 365 days")
    return n


def _clean_iso_date(value, label):
    """Validate an optional 'YYYY-MM-DD': None/blank clears (returns None);
    otherwise the exact 10-char ISO form, returned as given."""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        if len(text) != 10:
            raise ValueError
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise ActionError(f"{label} must be a date like 2026-08-22")
    return text


def _set_item_meta(db, actor, verb, item_id, column, value):
    """The shared edit for the #014 metadata setters (store / need_by /
    snoozed_until): one transaction, the column + an audit row with
    before/after. Deliberately does NOT bump updated_at — these are metadata,
    not stock events, and updated_at is restock_suggestions' since-the-item-
    changed bound (the merge_category rationale). Validation stays with each
    verb; the item must exist and be active."""
    with action_transaction(db):
        existing = db.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if existing is None or not existing["active"]:
            raise NotFound("not found")
        db.execute(f"UPDATE items SET {column} = ? WHERE id = ?",
                   (value, item_id))
        _write_audit(db, actor, verb, f"item:{item_id}",
                     {"before": existing[column], "after": value})
    return db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def set_item_store(db, actor, item_id, store):
    """Set or clear where an item is bought (Pantry v2 inc 2) — powers the
    store-grouped shopping list ("at Costco — what do we need here?"). Set
    once per item, zero upkeep; blank clears (NULL = ungrouped, the pre-#014
    list). Never touches money.

    validate — the item exists and is active (NotFound otherwise); store
    trimmed, ≤ 60 chars, blank clears.
    edit — the shared metadata edit (audited before/after; updated_at
    untouched). Returns the updated row.
    """
    value = (store or "").strip()[:60] or None
    return _set_item_meta(db, actor, "set_item_store", item_id, "store", value)


def set_item_need_by(db, actor, item_id, need_by):
    """Set or clear an item's deadline (Pantry v2 inc 2) — "candles before
    Saturday". The shopping list sorts needed-by-soonest first; the view
    frames overdue against the client's date (derivations stay clock-free).
    Blank clears (NULL = whenever). Never touches money.

    validate — the item exists and is active (NotFound otherwise); need_by is
    a 'YYYY-MM-DD' or blank-to-clear.
    edit — the shared metadata edit (audited before/after; updated_at
    untouched). Returns the updated row.
    """
    value = _clean_iso_date(need_by, "need_by")
    return _set_item_meta(db, actor, "set_item_need_by", item_id, "need_by",
                          value)


def set_item_snooze(db, actor, item_id, until):
    """Snooze or wake an item (Pantry v2 inc 2) — pause its nudges until a
    date ("we're traveling; stop nagging about milk"). The row keeps its
    status; the VIEW hides snoozed rows from the active list and their
    restock nudges against the client's today (derivations stay clock-free).
    Blank clears (NULL = live now). Never touches money.

    validate — the item exists and is active (NotFound otherwise); until is a
    'YYYY-MM-DD' or blank-to-wake.
    edit — the shared metadata edit (audited before/after; updated_at
    untouched — snoozing must not shift restock inference). Returns the row.
    """
    value = _clean_iso_date(until, "snoozed_until")
    return _set_item_meta(db, actor, "set_item_snooze", item_id,
                          "snoozed_until", value)


# ─────────────────── declarative action parameters (ACTION-SCHEMA-DESIGN) ──────
# The single source for each agent-exposed write verb's PARAMETER schema. Enums
# reference the ordered vocabulary tuples the frozensets also derive from, so the
# offered choices, the verb's validation, and the agent tool schema are one
# source — a new item status or income type can't leave the tools' enums stale.
# STRUCTURAL shape only (name/type/required/enum); semantic validation stays in
# the verb. agent_write_tools + ledger_mcp GENERATE their tool schemas from this
# (build order steps 2–3); a test pins param_schema() byte-equal to today's
# hand-written schemas so those steps are zero-change.

@dataclass(frozen=True)
class Param:
    """One agent-facing parameter of a write action: JSON type, whether it's
    required, an optional ordered enum (a verb vocabulary constant, never a copy),
    and the prose the agent reads."""
    name: str
    type: str                 # "string" | "integer" | "number" | "boolean" | "array"
    description: str
    required: bool = True
    enum: tuple = None        # ordered choices; references a verb constant
    items: str = None         # for type="array": the element JSON type


PARAM_SPECS = {
    "classify_inflow": [
        Param("transaction_id", "integer",
              "The money-in row's id (from a search or the unclassified-inflows "
              "queue)."),
        Param("income_type", "string",
              "What kind of income it is, per the person's own words.",
              enum=REAL_INCOME_TYPE_ORDER),
    ],
    "recategorize_transaction": [
        Param("transaction_id", "integer",
              "The SPENDING row's id, from ledger_search_transactions — confirm "
              "with the person which transaction before changing it."),
        Param("category", "string",
              "The destination category, e.g. 'Groceries'. Any label works — "
              "categories are just tags, so a new name creates that category."),
    ],
    "create_bill": [
        Param("name", "string", "What the bill is, e.g. 'Electric' or 'Rent'."),
        Param("amount", "number", "The bill amount in dollars, e.g. 85.50."),
        Param("due_day", "integer",
              "The day of the month it's due, a whole number 1–31."),
        Param("category", "string",
              "Optional category label; defaults to 'Bills'.", required=False),
    ],
    "create_goal": [
        Param("name", "string", "What they're saving for, e.g. 'Vacation'."),
        Param("target", "number", "The savings target in dollars, e.g. 2000."),
        Param("target_date", "string",
              "Optional target date as YYYY-MM-DD (work it out from what they "
              "said), or omit for no deadline.", required=False),
    ],
    "add_item": [
        Param("name", "string", "The item, e.g. 'Coffee'."),
        Param("kind", "string",
              "'staple' to track ongoing, 'oneoff' for a one-time buy. Defaults "
              "to 'staple'.", required=False, enum=ITEM_KIND_ORDER),
        Param("status", "string",
              "Only if they said so. Defaults: a staple starts 'stocked', a "
              "one-off starts 'out'.", required=False, enum=ITEM_STATUS_ORDER),
        Param("note", "string",
              "Optional detail, e.g. a brand or which store.", required=False),
    ],
    "set_item_status": [
        Param("item_id", "integer", "The item's id, from ledger_inventory."),
        Param("status", "string",
              "The new status. 'ordered' = they bought it (online) and it "
              "hasn't arrived yet — it leaves the shopping list without being "
              "counted as stocked; when it arrives, set 'stocked'.",
              enum=ITEM_STATUS_ORDER),
    ],
    "restock_items": [
        Param("item_ids", "array",
              "The bought items' ids, from ledger_inventory.", items="integer"),
    ],
    "archive_item": [
        Param("item_id", "integer", "The item's id, from ledger_inventory."),
    ],
    "set_item_match": [
        Param("item_id", "integer", "The item's id, from ledger_inventory."),
        Param("restock_match", "string",
              "The purchase description to match this staple against (e.g. "
              "'chewy' for dog food), or an empty string to clear the match."),
    ],
    "set_item_interval": [
        Param("item_id", "integer", "The staple's id, from ledger_inventory."),
        Param("days", "integer",
              "How many days between restocks — e.g. 14 for every two weeks, 30 "
              "for monthly. A whole number from 1 to 365."),
    ],
    "set_item_store": [
        Param("item_id", "integer", "The item's id, from ledger_inventory."),
        Param("store", "string",
              "Where it's bought (e.g. 'Costco'), or an empty string to "
              "clear it."),
    ],
    "set_item_need_by": [
        Param("item_id", "integer", "The item's id, from ledger_inventory."),
        Param("need_by", "string",
              "The deadline as YYYY-MM-DD (work it out from what they said, "
              "e.g. 'before Saturday'), or an empty string to clear it."),
    ],
    "set_item_snooze": [
        Param("item_id", "integer", "The item's id, from ledger_inventory."),
        Param("until", "string",
              "Snooze nudges until this YYYY-MM-DD, or an empty string to "
              "wake it now."),
    ],
    # The Ask facade for rules is deliberately NARROW (B2, like B1's
    # category-only recategorize): description-match + type only. Amount
    # bounds, account matches, priorities, and set_paid_by stay app/MCP
    # territory — a chat-created rule should be simple enough to read back
    # in one sentence.
    "create_income_rule": [
        Param("set_type", "string",
              "What matched deposits get tagged as, per the person's own "
              "words. 'transfer' makes it a transfer rule — matches are "
              "kept out of income AND out of spending (a credit-card "
              "payment, money moved between accounts).",
              enum=REAL_INCOME_TYPE_ORDER),
        Param("match_desc", "string",
              "The description phrase to match, case-insensitive substring "
              "— e.g. 'ADP PAYROLL' or 'Payment Thank You'. Use the stable "
              "part; trailing numbers vary per deposit."),
        Param("also_apply_to_existing", "boolean",
              "Whether confirming also tags the already-landed deposits the "
              "preview counted. Defaults to true.", required=False),
    ],
    "confirm_action": [
        Param("confirmation_token", "string",
              "The single-use token returned by ledger_propose_rule. Only "
              "ever pass a token you received this conversation — never "
              "invent or reuse one."),
    ],
    # B3: set a category's monthly spending limit from Ask. Like add_bill/
    # add_goal it's a DEFINITION — it moves no money and never touches the
    # balance — so it's a direct confirm-first tool, no two-phase needed.
    # An upsert: naming an existing category changes its limit.
    "set_budget": [
        Param("category", "string",
              "The spending category to budget, e.g. 'Groceries'. If a budget "
              "for it already exists, this changes the limit."),
        Param("amount", "number",
              "The monthly limit in dollars, e.g. 400. Must be positive."),
    ],
}


def param_schema(verb):
    """The JSON-Schema input object for a write verb, generated from PARAM_SPECS —
    the single source both agent tool surfaces consume. Structural shape only; the
    verb still owns semantic validation."""
    props, required = {}, []
    for p in PARAM_SPECS[verb]:
        prop = {"type": p.type}
        if p.items is not None:
            prop["items"] = {"type": p.items}
        if p.enum is not None:
            prop["enum"] = list(p.enum)
        prop["description"] = p.description
        props[p.name] = prop
        if p.required:
            required.append(p.name)
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}
