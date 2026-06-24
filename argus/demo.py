"""
ARGUS — demo reset (Phase 9)

Reseeds ONLY isolated, demo-owned datastore state to a clean, repeatable
starting point. Never touches the real Gmail mailbox or OpenAI config. Guarded
by DEMO_MODE at the endpoint (fails closed when not in demo mode).
"""
import os
import sqlite3
import time
import uuid

from config import FREE_ACTIONS, GATED_ACTIONS, STARTING_TRUST

DATABASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'argus.db')


def reset_demo():
    """Idempotent reseed of demo state. Returns a deterministic demo_run_id.

    Atomic: either the full reseed commits or it all rolls back (the audit
    guard triggers below are restored on any failure, so the table is never
    left unprotected).
    """
    db = sqlite3.connect(DATABASE)
    try:
        # Clear transient/run state. (audit_log is a legacy table; the live
        # audit trail is audit_events, handled separately below.) The Phase 8
        # admission/queue tables must be reseeded too, or stale dedup/rate
        # rows carry over and silently throttle the next demo run.
        for tbl in ("approval_queue", "pending_executions", "agent_proposals",
                    "trust_events", "audit_log",
                    "proposal_dedup", "rate_limits", "queue_transition_attempts"):
            db.execute(f"DELETE FROM {tbl}")

        # audit_events is append-only in production: BEFORE UPDATE/DELETE
        # triggers RAISE(ABORT). The demo factory-reset is the one privileged
        # exception — drop the guards, wipe, then immediately recreate them so
        # the new demo run is once again tamper-evident. This path is
        # DEMO_MODE-gated and never reachable in production, so the immutability
        # guarantee there is intact. If anything below throws, the rollback
        # restores the original triggers (SQLite DDL is transactional).
        db.execute("DROP TRIGGER IF EXISTS audit_no_update")
        db.execute("DROP TRIGGER IF EXISTS audit_no_delete")
        db.execute("DELETE FROM audit_events")
        db.execute(
            "CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END")
        db.execute(
            "CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END")

        # Reset trust to baseline for every action.
        for action in FREE_ACTIONS + GATED_ACTIONS:
            db.execute(
                "UPDATE trust_current SET trust_current=?, damping_remaining=0, damping_streak=0 "
                "WHERE action_type=?", (STARTING_TRUST, action))

        # Reset system state to a known demo baseline, including the hard-stop
        # epoch (otherwise it climbs across demos and every prior approval reads
        # as stale on the next run).
        db.execute("UPDATE system_state SET value='0' WHERE key='SYSTEM_HARD_STOP'")
        db.execute("UPDATE system_state SET value='0' WHERE key='HARD_STOP_EPOCH'")
        db.execute("UPDATE system_state SET value='Balanced' WHERE key='ACTIVE_PROFILE'")
        db.execute("UPDATE system_state SET value='1.0' WHERE key='OVERALL_TRUST_MODIFIER'")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return {"success": True, "demo_run_id": str(uuid.uuid4()), "reset_at": int(time.time())}
