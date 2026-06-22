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
    """Idempotent reseed of demo state. Returns a deterministic demo_run_id."""
    db = sqlite3.connect(DATABASE)
    try:
        # Clear transient/run state.
        for tbl in ("approval_queue", "pending_executions", "agent_proposals",
                    "trust_events", "audit_log"):
            db.execute(f"DELETE FROM {tbl}")

        # Reset trust to baseline for every action.
        for action in FREE_ACTIONS + GATED_ACTIONS:
            db.execute(
                "UPDATE trust_current SET trust_current=?, damping_remaining=0, damping_streak=0 "
                "WHERE action_type=?", (STARTING_TRUST, action))

        # Reset system state to a known demo baseline.
        db.execute("UPDATE system_state SET value='0' WHERE key='SYSTEM_HARD_STOP'")
        db.execute("UPDATE system_state SET value='Balanced' WHERE key='ACTIVE_PROFILE'")
        db.execute("UPDATE system_state SET value='1.0' WHERE key='OVERALL_TRUST_MODIFIER'")
        db.commit()
    finally:
        db.close()

    return {"success": True, "demo_run_id": str(uuid.uuid4()), "reset_at": int(time.time())}
