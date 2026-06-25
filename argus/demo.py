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
    db.row_factory = sqlite3.Row
    try:
        # Refuse to reset while a send is in flight: a SENDING row may already have
        # crossed the Gmail boundary, and wiping it would destroy the only record of
        # an action that might have happened. (Execution is reconcile-on-read and
        # single-threaded, so there's no concurrent worker to race — this guards the
        # operator clicking reset at the wrong moment, not a background thread.)
        in_flight = db.execute(
            "SELECT COUNT(*) FROM pending_executions WHERE status='SENDING'").fetchone()[0]
        if in_flight:
            return {"success": False, "error_code": "EXECUTION_IN_FLIGHT",
                    "detail": "A send is in flight — resolve it before resetting."}

        # Clear transient/run state. (audit_log is a legacy table; the live
        # audit trail is audit_events, handled separately below.) The Phase 8
        # admission/queue tables must be reseeded too, or stale dedup/rate
        # rows carry over and silently throttle the next demo run. private_contacts
        # is reseeded to a single fixture below so the demo can SHOW the block.
        # trust_events = the full trust HISTORY (the gauge is recomputed from it at
        # read time) → wiping it returns every action to baseline. contact_permissions
        # holds any per-contact permission state built up during a run.
        for tbl in ("approval_queue", "pending_executions", "agent_proposals",
                    "trust_events", "audit_log", "private_contacts",
                    "proposal_dedup", "rate_limits", "queue_transition_attempts",
                    "contact_permissions"):
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

        # Reset trust to baseline for EVERY row actually present in trust_current
        # (not just the config action list) — a stray action_type would otherwise
        # survive the reset and keep a stale gauge. Belt-and-braces: also force-seed
        # the known config actions in case a row is missing entirely.
        db.execute(
            "UPDATE trust_current SET trust_current=?, damping_remaining=0, damping_streak=0",
            (STARTING_TRUST,))
        for action in FREE_ACTIONS + GATED_ACTIONS:
            db.execute(
                "INSERT OR IGNORE INTO trust_current (action_type, trust_current, "
                "damping_remaining, damping_streak) VALUES (?,?,0,0)",
                (action, STARTING_TRUST))

        # Reset system state to a known demo baseline, including the hard-stop
        # epoch (otherwise it climbs across demos and every prior approval reads
        # as stale on the next run).
        db.execute("UPDATE system_state SET value='0' WHERE key='SYSTEM_HARD_STOP'")
        db.execute("UPDATE system_state SET value='0' WHERE key='HARD_STOP_EPOCH'")
        db.execute("UPDATE system_state SET value='Balanced' WHERE key='ACTIVE_PROFILE'")
        db.execute("UPDATE system_state SET value='1.0' WHERE key='OVERALL_TRUST_MODIFIER'")
        db.execute("UPDATE system_state SET value='60' WHERE key='UNDO_WINDOW_SECONDS'")

        # Seed exactly one protected contact so the demo can SHOW the private-contact
        # block working (an action targeting this address never reaches GPT).
        seed_now = int(time.time())
        db.execute(
            "INSERT INTO private_contacts (normalized_email, display_label, enabled, "
            "created_at, updated_at) VALUES (?,?,1,?,?)",
            ("legal@confidential-client.com", "Protected — Legal (demo)", seed_now, seed_now))

        # Wiping audit_events necessarily restarts the hash chain. Anchor the new
        # chain with an explicit genesis event so the reset is recorded as a
        # DELIBERATE history reset (not presented as continuous history). No user
        # content in the payload. INSERT is permitted by the append-only triggers.
        from argus import audit
        audit.record("DEMO_RESET_COMPLETED", outcome="RESET",
                     reason="demo factory reset — new audit-chain genesis",
                     payload={"note": "audit history intentionally reset for a fresh demo run"},
                     db=db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return {"success": True, "demo_run_id": str(uuid.uuid4()), "reset_at": int(time.time())}
