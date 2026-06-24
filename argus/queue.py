import sqlite3
import os
import time
import uuid
import json

DATABASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'argus.db')

VALID_TRANSITIONS = {
    "PENDING":       ["APPROVED", "REJECTED", "EXPIRED", "MANUAL_REVIEW", "CANCELLED"],
    "MANUAL_REVIEW": ["APPROVED", "REJECTED", "CANCELLED"],
    "APPROVED":      ["EXECUTED", "CANCELLED"],
    # HELD, MANUAL_REVIEW_TIMEOUT, TRANSITION_LOCKED have NO valid outward
    # transition here — recovery is owner-only via reopen (Phase 8 Part 6).
}

DEFAULT_UNDO_WINDOW = 30

# Control 4 — MANUAL_REVIEW timeout (lazy, on read/action).
MANUAL_REVIEW_TIMEOUT_SECONDS = 600

# Control 7 — invalid-transition rate limiting.
INVALID_TRANSITION_WINDOW = 60   # rolling window (seconds)
INVALID_TRANSITION_LIMIT  = 5    # invalid attempts in window before auto-lock
# An item may only be auto-locked while still actionable. Locking is NEVER
# reachable from APPROVED/EXECUTED/REJECTED/EXPIRED/CANCELLED/HELD — that would
# resurrect a terminal/claimed item.
LOCKABLE_STATES = ("PENDING", "MANUAL_REVIEW", "MANUAL_REVIEW_TIMEOUT")

# R-REOPEN (Part 6): the only queue states an owner may recover back to PENDING.
# CANCELLED is deliberately absent — cancellation is TERMINAL; to act again the
# user issues a fresh command (a new proposal, a new queue item). This closes the
# ambiguous-delivery double-send path that a cancel→reopen route would reopen.
REOPENABLE_QUEUE_STATES = ("HELD", "MANUAL_REVIEW_TIMEOUT", "TRANSITION_LOCKED")


def _db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db


def _now():
    return int(time.time())


def _record_attempt(db, queue_id, frm, to, valid):
    """C7 evidence trail: one row per transition attempt on a queue item."""
    db.execute(
        "INSERT INTO queue_transition_attempts "
        "(queue_id, attempted_from, attempted_to, valid, created_at) VALUES (?,?,?,?,?)",
        (queue_id, frm, to, 1 if valid else 0, _now()),
    )


def _invalid_attempts_in_window(db, queue_id, now):
    row = db.execute(
        "SELECT COUNT(*) AS n FROM queue_transition_attempts "
        "WHERE queue_id=? AND valid=0 AND created_at > ?",
        (queue_id, now - INVALID_TRANSITION_WINDOW),
    ).fetchone()
    return row["n"] if row else 0


def _handle_invalid(db, item_id, requested, current):
    """C7: record the refused attempt, and if this item has tripped the threshold
    AND is still in a lockable state, CAS it to TRANSITION_LOCKED. Commits its own
    work so the attempt persists even though the transition was refused.

    `status='TRANSITION_LOCKED'` is the SOLE lock authority — no separate flag.
    """
    now = _now()
    _record_attempt(db, item_id, current, requested, valid=False)
    locked = False
    if (current in LOCKABLE_STATES
            and _invalid_attempts_in_window(db, item_id, now) >= INVALID_TRANSITION_LIMIT):
        reason = (f"Auto-locked after {INVALID_TRANSITION_LIMIT} invalid transition "
                  f"attempts within {INVALID_TRANSITION_WINDOW}s")
        locked = db.execute(
            "UPDATE approval_queue SET status='TRANSITION_LOCKED', transition_lock_reason=?, "
            "transition_locked_at=?, updated_at=?, version=version+1 "
            "WHERE id=? AND status=?",
            (reason, now, now, item_id, current),
        ).rowcount > 0
    db.commit()
    if locked:
        from argus.audit import safe_record
        safe_record("QUEUE_TRANSITION_LOCKED", correlation_id=item_id,
                    idempotency_key=f"{item_id}:LOCKED:{now}", outcome="TRANSITION_LOCKED",
                    reason="INVALID_TRANSITION_RATE_LIMITED",
                    payload={"from": current, "attempted": requested})
        return {"success": False, "error_code": "INVALID_TRANSITION_RATE_LIMITED",
                "status": "TRANSITION_LOCKED", "item_id": item_id}
    return _invalid_transition(requested, current)


def _materialize_mr_timeout(db, item_id, current, started_at, mr_gen):
    """C4 lazy escalation: if a MANUAL_REVIEW item has exceeded the window, CAS it
    to MANUAL_REVIEW_TIMEOUT. Idempotent audit keyed on (item, mr_generation) so a
    re-read never double-records. Returns the (possibly updated) status."""
    if current != "MANUAL_REVIEW" or not started_at:
        return current
    now = _now()
    if now - started_at <= MANUAL_REVIEW_TIMEOUT_SECONDS:
        return current
    affected = db.execute(
        "UPDATE approval_queue SET status='MANUAL_REVIEW_TIMEOUT', updated_at=?, "
        "version=version+1, status_reason='Manual review timed out — reopen to act' "
        "WHERE id=? AND status='MANUAL_REVIEW' AND manual_review_generation=?",
        (now, item_id, mr_gen),
    ).rowcount
    db.commit()
    if affected:
        from argus.audit import safe_record
        safe_record("MANUAL_REVIEW_TIMEOUT", correlation_id=item_id,
                    idempotency_key=f"{item_id}:MR_TIMEOUT:{mr_gen}",
                    outcome="MANUAL_REVIEW_TIMEOUT",
                    payload={"manual_review_generation": mr_gen})
        return "MANUAL_REVIEW_TIMEOUT"
    return current


def _read_undo_window(db):
    try:
        row = db.execute(
            "SELECT value FROM system_state WHERE key='UNDO_WINDOW_SECONDS'"
        ).fetchone()
        return int(row["value"]) if row else DEFAULT_UNDO_WINDOW
    except Exception:
        return DEFAULT_UNDO_WINDOW


def _invalid_transition(requested, current):
    return {
        "success": False,
        "error_code": "INVALID_STATE_TRANSITION",
        "requested_transition": requested,
        "current_state": current,
    }


def _not_found(item_id):
    return {
        "success": False,
        "error_code": "ITEM_NOT_FOUND",
        "item_id": item_id,
    }


def enqueue(proposal: dict, decision: dict) -> dict:
    now = int(time.time())
    expiry = decision.get("action_expiry", 300)
    item_id = str(uuid.uuid4())
    try:
        db = _db()
        db.execute(
            """INSERT INTO approval_queue
               (id, proposal_json, decision_json, status,
                created_at, expires_at, approved_at, updated_at, status_reason, execution_id)
               VALUES (?, ?, ?, 'PENDING', ?, ?, NULL, ?, NULL, NULL)""",
            (item_id, json.dumps(proposal), json.dumps(decision), now, now + expiry, now)
        )
        db.commit()
        db.close()
        return {"success": True, "id": item_id, "expires_at": now + expiry}
    except Exception as e:
        return {"success": False, "error_code": "DB_ERROR", "detail": str(e)}


def fetch_pending() -> list:
    try:
        db = _db()
        # C4: lazily time out any MANUAL_REVIEW items past the window before listing.
        stale = db.execute(
            "SELECT id, manual_review_started_at, manual_review_generation "
            "FROM approval_queue WHERE status='MANUAL_REVIEW' "
            "AND manual_review_started_at IS NOT NULL AND manual_review_started_at < ?",
            (_now() - MANUAL_REVIEW_TIMEOUT_SECONDS,),
        ).fetchall()
        for s in stale:
            _materialize_mr_timeout(db, s["id"], "MANUAL_REVIEW",
                                    s["manual_review_started_at"], s["manual_review_generation"])
        rows = db.execute(
            """SELECT * FROM approval_queue
               WHERE status IN ('PENDING', 'MANUAL_REVIEW')
               ORDER BY created_at ASC"""
        ).fetchall()
        db.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def approve(item_id: str) -> dict:
    try:
        db = _db()
        row = db.execute(
            "SELECT status, proposal_json, manual_review_started_at, "
            "manual_review_generation FROM approval_queue WHERE id=?", (item_id,)
        ).fetchone()

        if not row:
            db.close()
            return _not_found(item_id)

        # C4: an item sitting in MANUAL_REVIEW past its window times out HERE, on
        # action — a timed-out item can never be approved directly (must reopen).
        current = _materialize_mr_timeout(db, item_id, row["status"],
                                          row["manual_review_started_at"],
                                          row["manual_review_generation"])
        if "APPROVED" not in VALID_TRANSITIONS.get(current, []):
            res = _handle_invalid(db, item_id, "APPROVE", current)
            db.close()
            return res

        # Part 6 atomic-approval guard: never approve while the emergency stop is
        # engaged, and stamp the approval with the live epoch + a fresh generation.
        # The epoch lets the executor preflight reject a stale APPROVED item; the
        # generation gives this approval cycle a unique execution identity so a
        # superseded execution can never collide with a re-approved one.
        from argus import kernel
        snap = kernel._hard_stop_snapshot(conn=db)
        if not snap.get("ok") or snap.get("engaged"):
            db.close()
            return {"success": False, "error_code": "HARD_STOP_ACTIVE",
                    "detail": "Emergency stop is engaged — approvals are blocked."}
        epoch = snap["epoch"]

        now = int(time.time())
        _record_attempt(db, item_id, current, "APPROVED", valid=True)
        affected = db.execute(
            """UPDATE approval_queue
               SET status='APPROVED', approved_at=?, updated_at=?,
                   version=version+1, approval_generation=approval_generation+1,
                   approval_epoch=?, status_reason=NULL
               WHERE id=? AND status=?""",
            (now, now, epoch, item_id, current)
        ).rowcount
        db.commit()
        db.close()

        if affected == 0:
            return _invalid_transition("APPROVE", current)

        from argus.audit import safe_record
        safe_record("QUEUE_TRANSITIONED", correlation_id=item_id,
                    idempotency_key=f"{item_id}:APPROVED", outcome="APPROVED",
                    payload={"from": current, "to": "APPROVED"})
        return {
            "success":       True,
            "id":            item_id,
            "status":        "APPROVED",
            "approved_at":   now,
            "proposal_json": row["proposal_json"],
        }
    except Exception as e:
        return {"success": False, "error_code": "DB_ERROR", "detail": str(e)}


def reject(item_id: str, reason: str = "Rejected by user") -> dict:
    try:
        db = _db()
        row = db.execute(
            "SELECT status, proposal_json, manual_review_started_at, "
            "manual_review_generation FROM approval_queue WHERE id=?", (item_id,)
        ).fetchone()

        if not row:
            db.close()
            return _not_found(item_id)

        current = _materialize_mr_timeout(db, item_id, row["status"],
                                          row["manual_review_started_at"],
                                          row["manual_review_generation"])
        if "REJECTED" not in VALID_TRANSITIONS.get(current, []):
            res = _handle_invalid(db, item_id, "REJECT", current)
            db.close()
            return res

        now = int(time.time())
        _record_attempt(db, item_id, current, "REJECTED", valid=True)
        affected = db.execute(
            """UPDATE approval_queue
               SET status='REJECTED', updated_at=?, version=version+1, status_reason=?
               WHERE id=? AND status=?""",
            (now, reason, item_id, current)
        ).rowcount
        db.commit()
        db.close()

        if affected == 0:
            return _invalid_transition("REJECT", current)

        from argus.audit import safe_record
        safe_record("QUEUE_TRANSITIONED", correlation_id=item_id,
                    idempotency_key=f"{item_id}:REJECTED", outcome="REJECTED",
                    payload={"from": current, "to": "REJECTED"})
        return {
            "success":       True,
            "id":            item_id,
            "status":        "REJECTED",
            "proposal_json": row["proposal_json"],
        }
    except Exception as e:
        return {"success": False, "error_code": "DB_ERROR", "detail": str(e)}


def cancel(item_id: str) -> dict:
    try:
        db = _db()
        row = db.execute(
            "SELECT status, approved_at, manual_review_started_at, "
            "manual_review_generation FROM approval_queue WHERE id=?", (item_id,)
        ).fetchone()

        if not row:
            db.close()
            return _not_found(item_id)

        current = _materialize_mr_timeout(db, item_id, row["status"],
                                          row["manual_review_started_at"],
                                          row["manual_review_generation"])
        if "CANCELLED" not in VALID_TRANSITIONS.get(current, []):
            res = _handle_invalid(db, item_id, "CANCEL", current)
            db.close()
            return res

        if current == "APPROVED":
            undo_window = _read_undo_window(db)
            approved_at = row["approved_at"]
            if approved_at is None or int(time.time()) > approved_at + undo_window:
                db.close()
                return {
                    "success": False,
                    "error_code": "UNDO_WINDOW_CLOSED",
                    "detail": f"Safety hold period of {undo_window}s has elapsed.",
                }

        now = int(time.time())
        _record_attempt(db, item_id, current, "CANCELLED", valid=True)
        affected = db.execute(
            """UPDATE approval_queue
               SET status='CANCELLED', updated_at=?, version=version+1,
                   status_reason='Cancelled by user'
               WHERE id=? AND status=?""",
            (now, item_id, current)
        ).rowcount
        db.commit()
        db.close()

        if affected == 0:
            return _invalid_transition("CANCEL", current)

        from argus.audit import safe_record
        safe_record("QUEUE_TRANSITIONED", correlation_id=item_id,
                    idempotency_key=f"{item_id}:CANCELLED", outcome="CANCELLED",
                    payload={"from": current, "to": "CANCELLED"})
        return {"success": True, "id": item_id, "status": "CANCELLED"}
    except Exception as e:
        return {"success": False, "error_code": "DB_ERROR", "detail": str(e)}


def to_manual_review(item_id: str, reason: str = "Routed to manual review") -> dict:
    """C4 entry point: CAS PENDING -> MANUAL_REVIEW, bump manual_review_generation
    and stamp manual_review_started_at so the lazy timeout has a clock to measure
    against. The generation bump makes each review window's timeout audit unique."""
    try:
        db = _db()
        row = db.execute("SELECT status FROM approval_queue WHERE id=?", (item_id,)).fetchone()
        if not row:
            db.close()
            return _not_found(item_id)

        current = row["status"]
        if "MANUAL_REVIEW" not in VALID_TRANSITIONS.get(current, []):
            res = _handle_invalid(db, item_id, "MANUAL_REVIEW", current)
            db.close()
            return res

        now = int(time.time())
        _record_attempt(db, item_id, current, "MANUAL_REVIEW", valid=True)
        affected = db.execute(
            """UPDATE approval_queue
               SET status='MANUAL_REVIEW',
                   manual_review_generation=manual_review_generation+1,
                   manual_review_started_at=?, updated_at=?, version=version+1,
                   status_reason=?
               WHERE id=? AND status=?""",
            (now, now, (reason or "")[:500], item_id, current)
        ).rowcount
        db.commit()
        db.close()

        if affected == 0:
            return _invalid_transition("MANUAL_REVIEW", current)

        from argus.audit import safe_record
        safe_record("QUEUE_TRANSITIONED", correlation_id=item_id,
                    idempotency_key=f"{item_id}:MANUAL_REVIEW:{now}", outcome="MANUAL_REVIEW",
                    payload={"from": current, "to": "MANUAL_REVIEW"})
        return {"success": True, "id": item_id, "status": "MANUAL_REVIEW"}
    except Exception as e:
        return {"success": False, "error_code": "DB_ERROR", "detail": str(e)}


def reopen(item_id: str, reason: str, actor: str = "owner") -> dict:
    """R-REOPEN (Part 6): owner-only recovery from HELD / MANUAL_REVIEW_TIMEOUT /
    TRANSITION_LOCKED back to PENDING. Branches by the linked execution's state so
    a reopen can NEVER resurrect an ambiguous-delivery send.

    INVARIANT: at most one Gmail send per queue item, ever. Reopen alone can never
    cause a second send — it only proceeds when the linked execution is provably
    pre-send (superseded), proven-unsent, or absent.
    """
    if not isinstance(reason, str) or not reason.strip():
        return {"success": False, "error_code": "REASON_REQUIRED"}
    if len(reason) > 500:
        return {"success": False, "error_code": "REJECTION_REASON_TOO_LONG"}

    try:
        db = _db()
        row = db.execute(
            "SELECT status, approval_generation FROM approval_queue WHERE id=?", (item_id,)
        ).fetchone()
        if not row:
            db.close()
            return _not_found(item_id)

        qstatus = row["status"]
        gen = row["approval_generation"]
        if qstatus not in REOPENABLE_QUEUE_STATES:
            db.close()
            return {"success": False, "error_code": "INVALID_REOPEN_STATE",
                    "current_state": qstatus}

        now = int(time.time())

        # Inspect the linked execution for THIS approval generation (at most one,
        # via UNIQUE(approval_id, approval_generation)).
        ex = db.execute(
            "SELECT execution_id, status FROM pending_executions "
            "WHERE approval_id=? AND approval_generation=? ORDER BY rowid DESC LIMIT 1",
            (item_id, gen)).fetchone()
        if ex is not None:
            es = ex["status"]
            if es in ("SENDING", "MANUAL_REVIEW"):
                # Delivery may have crossed the Gmail boundary — never supersede.
                db.close()
                return {"success": False, "error_code": "EXECUTION_OUTCOME_UNRESOLVED",
                        "detail": "Delivery outcome unresolved — resolve before reopening."}
            if es == "COMPLETED":
                # Already sent: reconcile the queue forward, do not reopen.
                db.execute(
                    "UPDATE approval_queue SET status='EXECUTED', updated_at=?, version=version+1 "
                    "WHERE id=? AND status=?", (now, item_id, qstatus))
                db.commit(); db.close()
                return {"success": False, "error_code": "ALREADY_EXECUTED"}
            if es in ("DRAFT_PENDING", "DRAFT_READY", "HELD"):
                # Fence A — claim-conditional supersede: only an UNCLAIMED pre-send
                # row may be superseded. If the executor claimed it mid-reopen
                # (owner_token set, or status moved on), this affects 0 rows -> refuse.
                superseded = db.execute(
                    "UPDATE pending_executions SET status='SUPERSEDED', updated_at=? "
                    "WHERE execution_id=? AND approval_generation=? AND owner_token IS NULL "
                    "AND status IN ('DRAFT_PENDING','DRAFT_READY','HELD')",
                    (now, ex["execution_id"], gen)).rowcount
                if superseded != 1:
                    db.rollback(); db.close()
                    return {"success": False, "error_code": "EXECUTION_OUTCOME_UNRESOLVED",
                            "detail": "Execution was claimed concurrently — cannot reopen."}
            # es in ('FAILED','SUPERSEDED'): proven-unsent / already-superseded —
            # nothing to supersede, reopen proceeds.

        affected = db.execute(
            "UPDATE approval_queue SET status='PENDING', updated_at=?, version=version+1, "
            "transition_lock_reason=NULL, transition_locked_at=NULL, "
            "manual_review_started_at=NULL, status_reason=? "
            "WHERE id=? AND status=?",
            (now, reason[:500], item_id, qstatus)).rowcount
        if affected != 1:
            db.rollback(); db.close()
            return _invalid_transition("REOPEN", qstatus)

        from argus import audit
        rec = audit.record("QUEUE_REOPENED", correlation_id=item_id, outcome="PENDING",
                           reason=reason[:500],
                           payload={"actor": actor, "from": qstatus, "to": "PENDING"}, db=db)
        if not rec.get("recorded"):
            db.rollback(); db.close()
            return {"success": False, "error_code": "AUDIT_WRITE_FAILED"}
        db.commit(); db.close()
        return {"success": True, "id": item_id, "status": "PENDING"}
    except Exception as e:
        return {"success": False, "error_code": "DB_ERROR", "detail": str(e)}


def expire_stale() -> dict:
    try:
        now = int(time.time())
        db = _db()
        affected = db.execute(
            """UPDATE approval_queue
               SET status='EXPIRED', updated_at=?, status_reason='Request timed out — no action taken'
               WHERE status='PENDING' AND expires_at < ?""",
            (now, now)
        ).rowcount
        db.commit()
        db.close()
        return {"success": True, "expired": affected}
    except Exception as e:
        return {"success": False, "error_code": "DB_ERROR", "detail": str(e)}
