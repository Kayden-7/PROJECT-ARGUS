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
}

DEFAULT_UNDO_WINDOW = 30


def _db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db


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
            "SELECT status, proposal_json FROM approval_queue WHERE id=?", (item_id,)
        ).fetchone()

        if not row:
            db.close()
            return _not_found(item_id)

        current = row["status"]
        if "APPROVED" not in VALID_TRANSITIONS.get(current, []):
            db.close()
            return _invalid_transition("APPROVE", current)

        now = int(time.time())
        affected = db.execute(
            """UPDATE approval_queue
               SET status='APPROVED', approved_at=?, updated_at=?, status_reason=NULL
               WHERE id=? AND status=?""",
            (now, now, item_id, current)
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
            "SELECT status, proposal_json FROM approval_queue WHERE id=?", (item_id,)
        ).fetchone()

        if not row:
            db.close()
            return _not_found(item_id)

        current = row["status"]
        if "REJECTED" not in VALID_TRANSITIONS.get(current, []):
            db.close()
            return _invalid_transition("REJECT", current)

        now = int(time.time())
        affected = db.execute(
            """UPDATE approval_queue
               SET status='REJECTED', updated_at=?, status_reason=?
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
            "SELECT status, approved_at FROM approval_queue WHERE id=?", (item_id,)
        ).fetchone()

        if not row:
            db.close()
            return _not_found(item_id)

        current = row["status"]
        if "CANCELLED" not in VALID_TRANSITIONS.get(current, []):
            db.close()
            return _invalid_transition("CANCEL", current)

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
        affected = db.execute(
            """UPDATE approval_queue
               SET status='CANCELLED', updated_at=?, status_reason='Cancelled by user'
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
