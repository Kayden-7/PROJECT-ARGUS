"""
ARGUS — Gmail execution layer (Phase 5 Part 2)

Implements the locked, stress-tested state machine:

    DRAFT_PENDING -> DRAFT_READY -> SENDING -> COMPLETED
                          |             |
                          +-------------+--> MANUAL_REVIEW (any uncertainty)

Core principle: ARGUS never silently double-sends and never silently classifies
an uncertain outcome. On ANY doubt, the execution stops in MANUAL_REVIEW for a
human. Driven by reconcile-on-read (runs on API calls) — no background worker.
"""
import sqlite3
import os
import time
import uuid
import json

DATABASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'argus.db')

# Actions that emit an irreversible message -> draft-based crash-safe flow.
DRAFT_ACTIONS = {
    "email.send.external", "email.send.internal", "email.reply", "email.forward",
}
# Actions that are a single idempotent Gmail call (safe to replay).
DIRECT_ACTIONS = {
    "email.delete",   # -> trash, never permanent delete
}
# Everything this executor handles (Part 2 = email only; calendar is Phase 6).
EXECUTABLE_ACTIONS = DRAFT_ACTIONS | DIRECT_ACTIONS


def _db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db


def _read_undo_window(db):
    try:
        row = db.execute(
            "SELECT value FROM system_state WHERE key='UNDO_WINDOW_SECONDS'"
        ).fetchone()
        return int(row["value"]) if row else 30
    except Exception:
        return 30


def _to_manual_review(db, execution_id, reason):
    now = int(time.time())
    db.execute(
        "UPDATE pending_executions SET status='MANUAL_REVIEW', status_reason=?, updated_at=? "
        "WHERE execution_id=?",
        (reason[:300], now, execution_id),
    )
    db.commit()


# ── Trust (idempotent per execution) ─────────────────────────────────────────

def _trust_written(db, execution_id):
    row = db.execute(
        "SELECT 1 FROM trust_events WHERE reason LIKE ? LIMIT 1",
        (f"EXECUTED:{execution_id}%",),
    ).fetchone()
    return row is not None


def _write_execution_trust(execution_id, action_type, outcome):
    """Write the execution trust event once, keyed by execution_id (idempotent)."""
    db = _db()
    already = _trust_written(db, execution_id)
    db.close()
    if already:
        return
    from argus.trust_ledger import record_event
    record_event(action_type, outcome, reason=f"EXECUTED:{execution_id}:{outcome}")


# ── Promotion: APPROVED queue item -> pending_execution ──────────────────────

def promote_approved():
    """
    Turn APPROVED queue items whose undo window has elapsed into exactly one
    pending_execution each (UNIQUE(approval_id) guarantees no duplicates even
    under concurrent reconciles or a double-click).
    """
    db = _db()
    now = int(time.time())
    undo = _read_undo_window(db)
    rows = db.execute(
        "SELECT id, proposal_json, approved_at FROM approval_queue "
        "WHERE status='APPROVED' AND approved_at IS NOT NULL AND (approved_at + ?) <= ?",
        (undo, now),
    ).fetchall()

    for r in rows:
        try:
            proposal = json.loads(r["proposal_json"]) if r["proposal_json"] else {}
        except Exception:
            proposal = {}
        action_type = proposal.get("action_type", "")
        if action_type not in EXECUTABLE_ACTIONS:
            continue  # calendar / non-Gmail handled elsewhere
        execution_id = str(uuid.uuid4())
        try:
            db.execute(
                "INSERT OR IGNORE INTO pending_executions "
                "(execution_id, approval_id, action_type, payload_json, status, "
                " attempt_count, approved_at, execute_after, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'DRAFT_PENDING', 0, ?, ?, ?, ?)",
                (execution_id, r["id"], action_type, r["proposal_json"],
                 r["approved_at"], now, now, now),
            )
            db.commit()
        except Exception:
            db.rollback()
    db.close()


# ── Per-action advancement ───────────────────────────────────────────────────

def _entities(row):
    try:
        proposal = json.loads(row["payload_json"]) if row["payload_json"] else {}
    except Exception:
        proposal = {}
    return proposal.get("entities", {}) or {}


def _advance_draft_action(db, row):
    from argus import gmail_client
    execution_id = row["execution_id"]
    status = row["status"]
    now = int(time.time())
    ent = _entities(row)

    if status == "DRAFT_PENDING":
        # Orphan-draft guard: if we already attempted once and still have no
        # draft_id, a draft may have been created but not recorded -> fail closed.
        if row["attempt_count"] and row["attempt_count"] >= 1 and not row["draft_id"]:
            _to_manual_review(db, execution_id,
                              "Ambiguous draft creation (possible orphan draft) — verify in Gmail.")
            return
        # Mark the attempt BEFORE calling Gmail, so a crash is detectable.
        db.execute(
            "UPDATE pending_executions SET attempt_count=attempt_count+1, updated_at=? "
            "WHERE execution_id=?",
            (now, execution_id),
        )
        db.commit()
        try:
            history_id = gmail_client.get_history_id()
            draft_id = gmail_client.create_draft(
                to=ent.get("recipient", ""),
                subject=ent.get("subject", ""),
                body=ent.get("body", ""),
                thread_id=ent.get("thread_id"),
                in_reply_to=ent.get("in_reply_to"),
            )
        except Exception as e:
            _to_manual_review(db, execution_id, f"Draft creation failed: {str(e)[:200]}")
            return
        db.execute(
            "UPDATE pending_executions SET draft_id=?, history_id=?, status='DRAFT_READY', "
            "updated_at=? WHERE execution_id=?",
            (draft_id, str(history_id), now, execution_id),
        )
        db.commit()
        return

    if status == "DRAFT_READY":
        # Atomic claim: only the winner (rowcount==1) proceeds to send.
        token = str(uuid.uuid4())
        affected = db.execute(
            "UPDATE pending_executions SET status='SENDING', owner_token=?, updated_at=? "
            "WHERE execution_id=? AND status='DRAFT_READY'",
            (token, now, execution_id),
        ).rowcount
        db.commit()
        if affected != 1:
            return  # someone else claimed it
        try:
            result = gmail_client.send_draft(row["draft_id"])
        except Exception as e:
            # We don't know if the send crossed the Gmail boundary -> fail closed.
            _to_manual_review(db, execution_id, f"Send ambiguous/failed: {str(e)[:200]}")
            return
        # Fence the completion write by owner_token so a zombie can't overwrite review.
        affected = db.execute(
            "UPDATE pending_executions SET status='COMPLETED', message_id=?, updated_at=? "
            "WHERE execution_id=? AND status='SENDING' AND owner_token=?",
            (result.get("message_id"), now, execution_id, token),
        ).rowcount
        db.commit()
        if affected == 1:
            _mark_queue_executed(db, row["approval_id"], execution_id)
            _write_execution_trust(execution_id, row["action_type"], "SUCCESS")
        return

    if status == "SENDING":
        # Recovery sees SENDING => we crashed mid-send. Never auto-resume.
        _to_manual_review(db, execution_id,
                          "Crashed during send — outcome unknown. Verify in Gmail Sent folder.")
        return


def _advance_direct_action(db, row):
    """Single idempotent Gmail call (currently email.delete -> trash)."""
    from argus import gmail_client
    execution_id = row["execution_id"]
    status = row["status"]
    now = int(time.time())
    ent = _entities(row)

    if status == "DRAFT_PENDING":
        token = str(uuid.uuid4())
        affected = db.execute(
            "UPDATE pending_executions SET status='SENDING', owner_token=?, updated_at=? "
            "WHERE execution_id=? AND status='DRAFT_PENDING'",
            (token, now, execution_id),
        ).rowcount
        db.commit()
        if affected != 1:
            return
        try:
            gmail_client.trash_message(ent.get("message_id", ""))
        except Exception as e:
            _to_manual_review(db, execution_id, f"Trash failed: {str(e)[:200]}")
            return
        affected = db.execute(
            "UPDATE pending_executions SET status='COMPLETED', updated_at=? "
            "WHERE execution_id=? AND status='SENDING' AND owner_token=?",
            (now, execution_id, token),
        ).rowcount
        db.commit()
        if affected == 1:
            _mark_queue_executed(db, row["approval_id"], execution_id)
            _write_execution_trust(execution_id, row["action_type"], "SUCCESS")
        return

    if status == "SENDING":
        _to_manual_review(db, execution_id,
                          "Crashed during delete — outcome unknown. Verify in Gmail.")
        return


def _mark_queue_executed(db, approval_id, execution_id):
    if not approval_id:
        return
    now = int(time.time())
    db.execute(
        "UPDATE approval_queue SET status='EXECUTED', execution_id=?, updated_at=? "
        "WHERE id=? AND status='APPROVED'",
        (execution_id, now, approval_id),
    )
    db.commit()


# ── Reconcile (runs on API reads) ────────────────────────────────────────────

def advance_executions():
    db = _db()
    rows = db.execute(
        "SELECT * FROM pending_executions "
        "WHERE status IN ('DRAFT_PENDING','DRAFT_READY','SENDING')"
    ).fetchall()
    for row in rows:
        try:
            if row["action_type"] in DRAFT_ACTIONS:
                _advance_draft_action(db, row)
            elif row["action_type"] in DIRECT_ACTIONS:
                _advance_direct_action(db, row)
            else:
                # Unrecognised combo -> fail closed.
                _to_manual_review(db, row["execution_id"],
                                  f"Unhandled action_type: {row['action_type']}")
        except Exception as e:
            # Any unexpected error in advancement -> fail closed, never silent.
            try:
                _to_manual_review(db, row["execution_id"], f"Executor error: {str(e)[:200]}")
            except Exception:
                pass
    db.close()


def reconcile():
    """Promote ready approvals, then advance every in-flight execution one step."""
    promote_approved()
    advance_executions()
