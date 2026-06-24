import sqlite3
import os
import time

DATABASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'argus.db')

REASON_MAX = 500


def _hard_stop_snapshot(conn=None):
    """Consistent single-read snapshot of the hard-stop control state.

    Reads SYSTEM_HARD_STOP and HARD_STOP_EPOCH in ONE statement so a concurrent
    toggle can never be observed half-applied. Returns
    {ok:True, engaged, epoch(int>=0), updated_at, updated_by, reason} or
    {ok:False, error}. Fails closed: missing row, a flag value other than
    '0'/'1', or a malformed/negative epoch all yield ok:False.
    """
    own = conn is None
    try:
        if own:
            conn = sqlite3.connect(DATABASE)
            conn.row_factory = sqlite3.Row
        rows = {r["key"]: r for r in conn.execute(
            "SELECT key, value, updated_at, updated_by, reason FROM system_state "
            "WHERE key IN ('SYSTEM_HARD_STOP','HARD_STOP_EPOCH')")}
        hs = rows.get("SYSTEM_HARD_STOP")
        ep = rows.get("HARD_STOP_EPOCH")
        if hs is None or ep is None:
            return {"ok": False, "error": "missing_state_row"}
        if hs["value"] not in ("0", "1"):
            return {"ok": False, "error": "malformed_hard_stop"}
        try:
            epoch = int(ep["value"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "malformed_epoch"}
        if epoch < 0:
            return {"ok": False, "error": "negative_epoch"}
        return {"ok": True, "engaged": hs["value"] == "1", "epoch": epoch,
                "updated_at": hs["updated_at"], "updated_by": hs["updated_by"],
                "reason": hs["reason"]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    finally:
        if own and conn is not None:
            conn.close()


def is_hard_stop() -> bool:
    """True if execution is halted. Fails CLOSED (True) on any read error."""
    snap = _hard_stop_snapshot()
    return True if not snap["ok"] else snap["engaged"]


def hard_stop_status() -> dict:
    """Status for the control endpoint. A degraded read reports engaged:True."""
    snap = _hard_stop_snapshot()
    if not snap["ok"]:
        return {"ok": False, "engaged": True, "epoch": None,
                "degraded": True, "error": snap["error"]}
    return {"ok": True, "engaged": snap["engaged"], "epoch": snap["epoch"],
            "updated_at": snap["updated_at"], "updated_by": snap["updated_by"],
            "reason": snap["reason"]}


def is_execution_stale(approval_epoch) -> bool:
    """True (block) if an approved execution must NOT proceed: hard stop engaged,
    epoch mismatch, or any uncertainty. Fails closed. (For the Part 6 executor
    preflight.) bool is a subclass of int in Python — reject it explicitly."""
    if (not isinstance(approval_epoch, int) or isinstance(approval_epoch, bool)
            or approval_epoch < 0):
        return True
    snap = _hard_stop_snapshot()
    if not snap["ok"] or snap["engaged"]:
        return True
    return approval_epoch != snap["epoch"]


def set_hard_stop(engaged: bool, updated_by: str = "control", reason=None) -> dict:
    """Toggle the emergency hard stop atomically. On a real off->on transition,
    the flag update + HARD_STOP_EPOCH bump + audit event commit together or roll
    back. Idempotent: a no-op toggle makes NO mutation and writes NO audit event.
    Rejects (never truncates) reasons over 500 chars (C8)."""
    if not isinstance(engaged, bool):
        return {"success": False, "error_code": "INVALID_ENGAGED"}
    if reason is not None:
        if not isinstance(reason, str):
            return {"success": False, "error_code": "INVALID_REASON"}
        if len(reason) > REASON_MAX:
            return {"success": False, "error_code": "REJECTION_REASON_TOO_LONG"}

    conn = sqlite3.connect(DATABASE, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return {"success": False, "error_code": "BUSY", "retryable": True}

        snap = _hard_stop_snapshot(conn)
        if not snap["ok"]:
            conn.execute("ROLLBACK")
            return {"success": False, "error_code": "STATE_UNAVAILABLE", "detail": snap["error"]}

        # Idempotent: already in the requested state -> no write, no audit event.
        if snap["engaged"] == engaged:
            conn.execute("ROLLBACK")
            return {"success": True, "engaged": engaged, "epoch": snap["epoch"], "transitioned": False}

        now = int(time.time())
        cur = conn.execute(
            "UPDATE system_state SET value=?, updated_at=?, updated_by=?, reason=? "
            "WHERE key='SYSTEM_HARD_STOP'",
            ("1" if engaged else "0", now, updated_by, reason))
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            return {"success": False, "error_code": "STATE_WRITE_FAILED"}

        new_epoch = snap["epoch"]
        if engaged:  # guaranteed real off->on transition here -> bump epoch once
            up = conn.execute(
                "UPDATE system_state SET value=?, updated_at=? WHERE key='HARD_STOP_EPOCH'",
                (str(snap["epoch"] + 1), now))
            if up.rowcount != 1:
                conn.execute("ROLLBACK")
                return {"success": False, "error_code": "EPOCH_WRITE_FAILED"}
            new_epoch = snap["epoch"] + 1

        from argus import audit
        event = "SYSTEM_HARD_STOP_ENABLED" if engaged else "SYSTEM_HARD_STOP_DISABLED"
        rec = audit.record(
            event, action_type="system.emergency_stop",
            outcome="ENGAGED" if engaged else "RELEASED", reason=reason,
            payload={"engaged": engaged, "epoch": new_epoch,
                     "updated_by": updated_by, "transitioned": True},
            db=conn)
        if not rec.get("recorded"):
            conn.execute("ROLLBACK")
            return {"success": False, "error_code": "AUDIT_WRITE_FAILED", "detail": rec.get("error")}

        conn.execute("COMMIT")
        return {"success": True, "engaged": engaged, "epoch": new_epoch, "transitioned": True}
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return {"success": False, "error_code": "HARD_STOP_FAILED", "detail": str(e)[:120]}
    finally:
        conn.close()


# ── Execution delay (Settings > Execution Delay) ────────────────────────────
# Reuses UNDO_WINDOW_SECONDS: the same window both lets a user cancel an
# approved action AND gates when the executor is allowed to promote it to a
# real send (see executor.promote_approved). One number, two effects.
MIN_EXECUTION_DELAY_SECONDS = 60  # hard floor — never send sooner than this


def get_execution_delay() -> dict:
    try:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT value, updated_at FROM system_state WHERE key='UNDO_WINDOW_SECONDS'"
        ).fetchone()
        conn.close()
        raw = int(row["value"]) if row else MIN_EXECUTION_DELAY_SECONDS
        # Floor enforced on read too — must match what executor/queue actually
        # use, or this endpoint would report a number nothing else honors
        # (e.g. a pre-existing row seeded at the old 30s default).
        seconds = max(MIN_EXECUTION_DELAY_SECONDS, raw)
        return {"success": True, "seconds": seconds, "updated_at": row["updated_at"] if row else None}
    except Exception as e:
        return {"success": False, "error_code": "STATE_READ_FAILED", "detail": str(e)[:120]}


def set_execution_delay(seconds, updated_by: str = "control") -> dict:
    """Clamp to the floor server-side — the frontend slider already won't go
    below 1 minute, but a direct API call must not be able to bypass that."""
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        return {"success": False, "error_code": "INVALID_SECONDS"}
    clamped = max(MIN_EXECUTION_DELAY_SECONDS, seconds)
    now = int(time.time())
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute(
            "UPDATE system_state SET value=?, updated_at=?, updated_by=? "
            "WHERE key='UNDO_WINDOW_SECONDS'",
            (str(clamped), now, updated_by))
        if cur.rowcount != 1:
            conn.execute(
                "INSERT INTO system_state (key, value, updated_at, updated_by) "
                "VALUES ('UNDO_WINDOW_SECONDS', ?, ?, ?)",
                (str(clamped), now, updated_by))
        conn.commit()
        conn.close()
        return {"success": True, "seconds": clamped, "requested": seconds,
                "clamped": clamped != seconds}
    except Exception as e:
        return {"success": False, "error_code": "STATE_WRITE_FAILED", "detail": str(e)[:120]}


VALID_PROFILES = ("Strict", "Balanced", "Autonomous")


def get_active_profile() -> dict:
    """Active policy profile + its trust ceiling. Fails closed to Balanced (the
    safe middle) if the row is missing or unreadable."""
    from config import PROFILE_TRUST_CEILINGS
    try:
        conn = sqlite3.connect(DATABASE)
        row = conn.execute("SELECT value FROM system_state WHERE key='ACTIVE_PROFILE'").fetchone()
        conn.close()
        profile = row[0] if row and row[0] in VALID_PROFILES else "Balanced"
    except Exception:
        profile = "Balanced"
    return {"profile": profile, "ceiling": PROFILE_TRUST_CEILINGS.get(profile, 85.0)}


def set_active_profile(profile, updated_by="control", reason=None) -> dict:
    """Switch the active policy profile atomically with its audit event. The
    policy engine (threshold) and trust ledger (ceiling) both read ACTIVE_PROFILE
    live, so the switch takes effect on the next evaluation. Idempotent: a no-op
    switch makes no write and no audit event."""
    from config import PROFILE_TRUST_CEILINGS
    if profile not in VALID_PROFILES:
        return {"success": False, "error_code": "INVALID_PROFILE"}
    if reason is not None:
        if not isinstance(reason, str):
            return {"success": False, "error_code": "INVALID_REASON"}
        if len(reason) > REASON_MAX:
            return {"success": False, "error_code": "REJECTION_REASON_TOO_LONG"}

    conn = sqlite3.connect(DATABASE, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return {"success": False, "error_code": "BUSY", "retryable": True}

        row = conn.execute("SELECT value FROM system_state WHERE key='ACTIVE_PROFILE'").fetchone()
        current = row["value"] if row else None
        ceiling = PROFILE_TRUST_CEILINGS.get(profile, 85.0)

        if current == profile:  # idempotent: no write, no audit event
            conn.execute("ROLLBACK")
            return {"success": True, "profile": profile, "ceiling": ceiling, "transitioned": False}

        now = int(time.time())
        cur = conn.execute(
            "UPDATE system_state SET value=?, updated_at=?, updated_by=?, reason=? "
            "WHERE key='ACTIVE_PROFILE'", (profile, now, updated_by, reason))
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            return {"success": False, "error_code": "STATE_WRITE_FAILED"}

        from argus import audit
        rec = audit.record(
            "POLICY_PROFILE_CHANGED", action_type="system.policy_profile",
            outcome="CHANGED", reason=reason,
            payload={"old": current, "new": profile, "ceiling": ceiling,
                     "updated_by": updated_by}, db=conn)
        if not rec.get("recorded"):
            conn.execute("ROLLBACK")
            return {"success": False, "error_code": "AUDIT_WRITE_FAILED", "detail": rec.get("error")}

        conn.execute("COMMIT")
        return {"success": True, "profile": profile, "ceiling": ceiling, "transitioned": True}
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return {"success": False, "error_code": "PROFILE_FAILED", "detail": str(e)[:120]}
    finally:
        conn.close()


def kernel_entry(proposal: dict) -> dict:
    if is_hard_stop():
        return {
            "decision": "BLOCK",
            "failure_type": "EMERGENCY",
            "failure_reason_code": "SYSTEM_HARD_STOP",
            "trace": ["SYSTEM_HARD_STOP=1 — all execution blocked"],
            "conflict_resolution_path": ["SYSTEM_HARD_STOP"],
            "trust_impact": "none",
            "action_expiry": 0
        }

    from argus.validation import validate_proposal
    from argus.policy_engine import evaluate

    result = validate_proposal(proposal)
    if not result["valid"]:
        return {
            "decision":            "BLOCK",
            "decision_source":     "VALIDATION",
            "failure_type":        "VALIDATION",
            "failure_reason_code": result["errors"][0]["code"] if result["errors"] else "VALIDATION_FAILED",
            "terminated_at":       "VALIDATION",
            "trace":               [{"step": "VALIDATION", "result": "FAIL",
                                     "reason": str(result["errors"]), "before": None, "after": None}],
            "trust_at_evaluation": None,
            "effective_threshold": None,
            "trust_impact":        "none",
            "trust_delta_preview": None,
            "action_expiry":       0,
            "narrative":           "Proposal failed validation and was rejected before policy evaluation.",
            "modifier_breakdown":  {},
        }

    sanitized = result["sanitized_proposal"]
    decision = evaluate(sanitized)

    # ── Layer 7: Safety Downgrade Filter (one-way ALLOW→GATED) ────────────────
    # Runs after the hierarchy, before execution. Certain high-impact actions
    # always require human approval regardless of trust. Never grants.
    from argus.safety_filter import apply_safety_filter
    return apply_safety_filter(sanitized, decision)
