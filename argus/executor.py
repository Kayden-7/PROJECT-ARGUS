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
    from argus.kernel import MIN_EXECUTION_DELAY_SECONDS
    try:
        row = db.execute(
            "SELECT value FROM system_state WHERE key='UNDO_WINDOW_SECONDS'"
        ).fetchone()
        value = int(row["value"]) if row else MIN_EXECUTION_DELAY_SECONDS
    except Exception:
        value = MIN_EXECUTION_DELAY_SECONDS
    # Floor enforced on read too, not just on write — covers rows seeded by an
    # older default (30s) before the 1-minute minimum existed.
    return max(MIN_EXECUTION_DELAY_SECONDS, value)


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


def _audit_exec(row, event_type, outcome):
    """Best-effort execution audit. correlation = approval_id so it links to the
    decision + queue lifecycle. Idempotency keyed per execution + phase."""
    from argus.audit import safe_record
    suffix = "ATTEMPT" if event_type == "EXECUTION_ATTEMPT" else "RESOLVED"
    safe_record(event_type, correlation_id=row["approval_id"],
                idempotency_key=f"{row['execution_id']}:{suffix}",
                action_type=row["action_type"], outcome=outcome,
                payload={"execution_id": row["execution_id"]})


def _recipients_match(gmail_client, draft_id, ent):
    """
    Role-aware pre-send integrity: the live draft's recipients must equal the
    approved proposal's. Approved set = the single proposal recipient in To,
    no Cc, no Bcc. Any role change / extra / missing / parse failure -> mismatch.
    On a read error we fail closed (treat as mismatch -> MANUAL_REVIEW).
    """
    from email.utils import parseaddr
    try:
        roles = gmail_client.get_draft_recipients(draft_id)
    except Exception:
        return False
    def _addrs(values):
        out = []
        for v in values or []:
            _n, a = parseaddr(v)
            if a:
                out.append(a.strip().lower())
        return sorted(out)
    expected_to = sorted([parseaddr(ent.get("recipient", ""))[1].strip().lower()]) \
        if ent.get("recipient") else []
    if _addrs(roles.get("to")) != expected_to:
        return False
    if _addrs(roles.get("cc")) or _addrs(roles.get("bcc")):
        return False
    return True


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
            cls = gmail_client.classify_gmail_error("PRE_SEND", e)
            _to_manual_review(db, execution_id,
                              f"Draft creation failed [{cls['class']}:{cls['sub_reason']}]: {str(e)[:160]}")
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

        # Phase 7: audit the attempt BEFORE the Gmail call — FAIL CLOSED. If the
        # attempt can't be recorded, do not send (per the audit design contract).
        from argus.audit import record as _audit_record
        att = _audit_record("EXECUTION_ATTEMPT", correlation_id=row["approval_id"],
                            idempotency_key=f"{execution_id}:ATTEMPT",
                            action_type=row["action_type"], outcome="SENDING",
                            payload={"execution_id": execution_id})
        if not att.get("recorded") and att.get("reason") != "duplicate":
            _to_manual_review(db, execution_id,
                              "Execution attempt could not be audited — not sent (fail-closed).")
            return

        # Re-validate recipients immediately before send (role-aware). Shrinks the
        # check-to-send window; any mutation -> RECIPIENT_MISMATCH -> MANUAL_REVIEW.
        if not _recipients_match(gmail_client, row["draft_id"], ent):
            _to_manual_review(db, execution_id,
                              "RECIPIENT_MISMATCH: draft recipients differ from the approved proposal")
            _audit_exec(row, "EXECUTION_RESOLVED", "MANUAL_REVIEW")
            return

        try:
            result = gmail_client.send_draft(row["draft_id"])
        except Exception as e:
            # Any uncertainty after invoking drafts.send -> UNKNOWN_DELIVERY_STATE.
            cls = gmail_client.classify_gmail_error("SEND", e)
            _to_manual_review(db, execution_id,
                              f"Send outcome unknown [{cls['class']}:{cls['sub_reason']}] — "
                              f"not resent. Verify in Gmail Sent folder.")
            _audit_exec(row, "EXECUTION_RESOLVED", "MANUAL_REVIEW")
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
            _audit_exec(row, "EXECUTION_RESOLVED", "COMPLETED")
        return

    if status == "SENDING":
        # Recovery sees SENDING => the previous pass crashed (or was simply
        # interrupted) somewhere between calling drafts.send and writing
        # COMPLETED. We don't have to GUESS what happened — Gmail consumes a
        # draft the instant it sends, so a read-only drafts.get tells us for
        # certain: gone => it sent (mark COMPLETED, no false "crashed" alarm);
        # still there => it never sent (safe to resume from DRAFT_READY,
        # never a double-send since nothing went out). Only a genuinely
        # inconclusive check (the read itself fails) falls back to manual
        # review — never assumed, only confirmed.
        if not row["draft_id"]:
            _to_manual_review(db, execution_id,
                              "Crashed during send with no draft on record — outcome unknown. "
                              "Verify in Gmail Sent folder.")
            return
        from argus import gmail_client
        try:
            still_drafted = gmail_client.draft_exists(row["draft_id"])
        except Exception as e:
            _to_manual_review(db, execution_id,
                              f"Crashed during send — could not verify outcome ({str(e)[:120]}). "
                              f"Verify in Gmail Sent folder.")
            return
        if still_drafted:
            # Never sent — safe to retry. Release the claim so the atomic
            # claim in DRAFT_READY can pick it back up on the next pass.
            db.execute(
                "UPDATE pending_executions SET status='DRAFT_READY', owner_token=NULL, "
                "updated_at=? WHERE execution_id=? AND status='SENDING'",
                (now, execution_id),
            )
            db.commit()
            return
        # Draft is gone — Gmail only consumes a draft by sending it. Confirmed
        # sent, fenced by the owner_token recorded at claim time.
        affected = db.execute(
            "UPDATE pending_executions SET status='COMPLETED', updated_at=? "
            "WHERE execution_id=? AND status='SENDING' AND owner_token=?",
            (now, execution_id, row["owner_token"]),
        ).rowcount
        db.commit()
        if affected == 1:
            _mark_queue_executed(db, row["approval_id"], execution_id)
            _write_execution_trust(execution_id, row["action_type"], "SUCCESS")
            _audit_exec(row, "EXECUTION_RESOLVED", "COMPLETED")
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
        # Unlike a send, trash is idempotent (re-trashing an already-trashed
        # message is a no-op success) — safe to just retry rather than
        # parking in manual review for something that isn't actually unknown.
        try:
            gmail_client.trash_message(ent.get("message_id", ""))
        except Exception as e:
            _to_manual_review(db, execution_id, f"Trash retry failed: {str(e)[:200]}")
            return
        affected = db.execute(
            "UPDATE pending_executions SET status='COMPLETED', updated_at=? "
            "WHERE execution_id=? AND status='SENDING' AND owner_token=?",
            (now, execution_id, row["owner_token"]),
        ).rowcount
        db.commit()
        if affected == 1:
            _mark_queue_executed(db, row["approval_id"], execution_id)
            _write_execution_trust(execution_id, row["action_type"], "SUCCESS")
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
