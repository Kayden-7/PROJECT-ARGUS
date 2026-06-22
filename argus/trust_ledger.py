import sqlite3
import os
import time
import uuid

from config import (
    STARTING_TRUST, INERTIA_THRESHOLD, INERTIA_WEIGHT,
    OVERALL_MOD_START, OVERALL_MOD_SHIFT, OVERALL_MOD_MAX, OVERALL_MOD_MIN,
    PROFILE_TRUST_CEILINGS,
    DAMPING_N, DAMPING_MULTIPLIER, DAMPING_STABILITY,
    POLICY_GATE_BLOCK_PENALTY,
)
from argus.policy_engine import ACTION_SEVERITY, SEVERITY_DELTAS

DATABASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'argus.db')

# ── Recency weights (replaces 30-day decay — one time mechanic, not two) ──────
RECENCY_FULL     = 1.0   # 0–7 days
RECENCY_REDUCED  = 0.5   # 7–30 days
RECENCY_DISCOUNT = 0.1   # 30+ days

# ── Trust labels ───────────────────────────────────────────────────────────────
TRUST_LABELS = [
    (20,  "Untrusted",       "Requires Oversight"),
    (40,  "Low Trust",       "Learning Phase"),
    (60,  "Developing",      "Generally Reliable"),
    (80,  "Trusted",         "Safe to Delegate"),
    (101, "Highly Reliable", "Autonomous Range"),
]

VALID_OUTCOMES = {"SUCCESS", "FAILURE", "POLICY_GATE_BLOCK"}


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db


def _read_overall_modifier(db) -> float:
    try:
        row = db.execute(
            "SELECT value FROM system_state WHERE key='OVERALL_TRUST_MODIFIER'"
        ).fetchone()
        return float(row["value"]) if row else OVERALL_MOD_START
    except Exception:
        return OVERALL_MOD_START


def _write_overall_modifier(db, value: float) -> float:
    clamped = max(OVERALL_MOD_MIN, min(OVERALL_MOD_MAX, value))
    db.execute(
        "UPDATE system_state SET value=? WHERE key='OVERALL_TRUST_MODIFIER'",
        (str(round(clamped, 4)),)
    )
    return clamped


def _read_active_profile(db) -> str:
    try:
        row = db.execute(
            "SELECT value FROM system_state WHERE key='ACTIVE_PROFILE'"
        ).fetchone()
        return row["value"] if row else "Balanced"
    except Exception:
        return "Balanced"


def _read_trust_raw(db, action_type: str) -> float:
    try:
        row = db.execute(
            "SELECT trust_current FROM trust_current WHERE action_type=?", (action_type,)
        ).fetchone()
        return float(row["trust_current"]) if row else STARTING_TRUST
    except Exception:
        return STARTING_TRUST


def _read_damping(db, action_type: str) -> tuple:
    try:
        row = db.execute(
            "SELECT damping_remaining, damping_streak FROM trust_current WHERE action_type=?",
            (action_type,)
        ).fetchone()
        if row:
            return int(row["damping_remaining"]), int(row["damping_streak"])
        return 0, 0
    except Exception:
        return 0, 0


def _read_action_count(db, action_type: str) -> int:
    try:
        row = db.execute(
            "SELECT COUNT(*) AS cnt FROM trust_events WHERE action_type=?", (action_type,)
        ).fetchone()
        return row["cnt"] if row else 0
    except Exception:
        return 0


def _recency_weight(timestamp: int) -> float:
    age_days = (time.time() - timestamp) / 86400
    if age_days <= 7:
        return RECENCY_FULL
    elif age_days <= 30:
        return RECENCY_REDUCED
    else:
        return RECENCY_DISCOUNT


def _trust_label(score: float) -> dict:
    for threshold, label, description in TRUST_LABELS:
        if score <= threshold:
            return {"label": label, "description": description}
    return {"label": "Highly Reliable", "description": "Autonomous Range"}


# ── Public: read ───────────────────────────────────────────────────────────────

def get_trust(action_type: str) -> dict:
    """
    Returns effective trust score with recency weighting applied.
    Raw accumulated score is stored in trust_current.
    Recency-weighted score is computed at read time from trust_events.
    Policy display and GET /api/trust use this value.
    """
    try:
        db = _db()
        events = db.execute(
            "SELECT delta, timestamp FROM trust_events WHERE action_type=? ORDER BY timestamp ASC",
            (action_type,)
        ).fetchall()

        action_count    = len(events)
        damping_remaining, _ = _read_damping(db, action_type)
        profile         = _read_active_profile(db)
        ceiling         = PROFILE_TRUST_CEILINGS.get(profile, 85.0)
        raw_trust       = _read_trust_raw(db, action_type)
        overall_modifier = _read_overall_modifier(db)
        db.close()

        # Recency-weighted recomputation from trust_events
        if not events:
            effective_trust = STARTING_TRUST
        else:
            weighted_delta_sum = sum(
                ev["delta"] * _recency_weight(ev["timestamp"]) for ev in events
            )
            effective_trust = max(0.0, min(ceiling, STARTING_TRUST + weighted_delta_sum))

        label_info = _trust_label(effective_trust)

        return {
            "action_type":       action_type,
            "trust":             round(effective_trust, 2),
            "raw_trust":         round(raw_trust, 2),
            "label":             label_info["label"],
            "description":       label_info["description"],
            "event_count":       action_count,
            "inertia_active":    action_count < INERTIA_THRESHOLD,
            "damping_active":    damping_remaining > 0,
            "damping_remaining": damping_remaining,
            "overall_modifier":  round(overall_modifier, 4),
            "profile":           profile,
            "ceiling":           ceiling,
        }

    except Exception as e:
        return {
            "action_type":       action_type,
            "trust":             STARTING_TRUST,
            "raw_trust":         STARTING_TRUST,
            "label":             "Low Trust",
            "description":       "Learning Phase",
            "event_count":       0,
            "inertia_active":    True,
            "damping_active":    False,
            "damping_remaining": 0,
            "overall_modifier":  OVERALL_MOD_START,
            "profile":           "Balanced",
            "ceiling":           85.0,
            "error":             str(e),
        }


# ── Public: write ──────────────────────────────────────────────────────────────

def record_event(action_type: str, outcome: str, severity: str = None, reason: str = "") -> dict:
    """
    Records a trust event and updates trust_current + damping state + overall modifier.

    outcome:  "SUCCESS" | "FAILURE" | "POLICY_GATE_BLOCK"
    severity: "TRIVIAL" | "LOW" | "MEDIUM" | "HIGH"
              Inferred from ACTION_SEVERITY if not provided.
              Ignored for POLICY_GATE_BLOCK (fixed penalty applies).
    """
    if outcome not in VALID_OUTCOMES:
        return {"success": False, "error_code": "INVALID_OUTCOME", "detail": outcome}

    from config import ALL_ACTIONS
    if action_type not in ALL_ACTIONS:
        return {"success": False, "error_code": "UNKNOWN_ACTION_TYPE", "detail": action_type}

    trust_before = STARTING_TRUST  # captured early for reconciliation fallback

    try:
        db = _db()
        now = int(time.time())

        # ── Read current state ─────────────────────────────────────────────────
        trust_before      = _read_trust_raw(db, action_type)
        action_count      = _read_action_count(db, action_type)
        overall_modifier  = _read_overall_modifier(db)
        profile           = _read_active_profile(db)
        ceiling           = PROFILE_TRUST_CEILINGS.get(profile, 85.0)
        damping_remaining, damping_streak = _read_damping(db, action_type)

        # ── Step 1: Base delta ─────────────────────────────────────────────────
        if outcome == "POLICY_GATE_BLOCK":
            base_delta      = POLICY_GATE_BLOCK_PENALTY
            is_success      = False
            is_high_failure = False
            severity        = "N/A"
        else:
            if severity is None:
                severity = ACTION_SEVERITY.get(action_type, "MEDIUM")
            success_delta, failure_delta = SEVERITY_DELTAS.get(severity, (7, -18))
            is_success      = (outcome == "SUCCESS")
            is_high_failure = (not is_success and severity == "HIGH")
            base_delta      = success_delta if is_success else failure_delta

        # ── Step 2: Inertia ────────────────────────────────────────────────────
        inertia_active = action_count < INERTIA_THRESHOLD
        inertia_weight = INERTIA_WEIGHT if inertia_active else 1.0

        # ── Step 3: Damping ────────────────────────────────────────────────────
        # Damping only reduces SUCCESS gains.
        # Inertia + damping: use the more restrictive (lower) multiplier, never multiply them.
        if damping_remaining > 0 and is_success:
            effective_weight = min(inertia_weight, DAMPING_MULTIPLIER)
        elif is_success:
            effective_weight = inertia_weight
        else:
            effective_weight = inertia_weight  # failure penalties are not dampened

        # ── Step 4: Overall modifier (not applied to policy gate blocks) ───────
        if outcome != "POLICY_GATE_BLOCK":
            weighted_delta = base_delta * effective_weight * overall_modifier
        else:
            weighted_delta = base_delta

        # ── Step 5: New trust + ceiling ────────────────────────────────────────
        trust_after  = max(0.0, min(ceiling, trust_before + weighted_delta))
        actual_delta = round(trust_after - trust_before, 4)

        # ── Step 6: Damping state update ───────────────────────────────────────
        new_remaining = damping_remaining
        new_streak    = damping_streak

        if is_high_failure:
            # Activate or extend damping (second HIGH failure resets counter to N)
            new_remaining = DAMPING_N
            new_streak    = 0
        elif damping_remaining > 0:
            new_remaining -= 1
            if is_success:
                new_streak += 1
                if new_streak >= DAMPING_STABILITY:
                    # Stability achieved — exit damping early
                    new_remaining = 0
                    new_streak    = 0
            else:
                # Non-HIGH failure resets streak but does not extend window
                new_streak = 0

        # ── Step 7: Write trust event ──────────────────────────────────────────
        event_id     = str(uuid.uuid4())
        trust_reason = reason or f"{outcome}:{severity}"

        db.execute(
            """INSERT INTO trust_events
               (event_id, timestamp, action_type, delta, reason, resulting_trust)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, now, action_type, actual_delta, trust_reason, round(trust_after, 4))
        )

        # ── Step 8: Update trust_current + damping columns ────────────────────
        db.execute(
            """UPDATE trust_current
               SET trust_current=?, damping_remaining=?, damping_streak=?
               WHERE action_type=?""",
            (round(trust_after, 4), new_remaining, new_streak, action_type)
        )

        # ── Step 9: Shift overall modifier ────────────────────────────────────
        if outcome != "POLICY_GATE_BLOCK":
            shift        = OVERALL_MOD_SHIFT if is_success else -OVERALL_MOD_SHIFT
            new_modifier = _write_overall_modifier(db, overall_modifier + shift)
        else:
            new_modifier = overall_modifier

        # Phase 7: audit the trust change in the SAME transaction (atomic).
        try:
            from argus.audit import record as _audit_record
            _audit_record("TRUST_CHANGED", correlation_id=event_id,
                          idempotency_key=f"{event_id}:TRUST", action_type=action_type,
                          outcome=outcome,
                          payload={"delta": actual_delta, "resulting_trust": round(trust_after, 4),
                                   "severity": severity}, db=db)
        except Exception:
            pass

        db.commit()
        db.close()

        return {
            "success":           True,
            "event_id":          event_id,
            "action_type":       action_type,
            "outcome":           outcome,
            "severity":          severity,
            "base_delta":        base_delta,
            "actual_delta":      actual_delta,
            "trust_before":      round(trust_before, 4),
            "trust_after":       round(trust_after, 4),
            "inertia_active":    inertia_active,
            "damping_active":    new_remaining > 0,
            "damping_remaining": new_remaining,
            "damping_streak":    new_streak,
            "overall_modifier":  round(new_modifier, 4),
            "profile_ceiling":   ceiling,
        }

    except Exception as e:
        # Write a reconciliation event so the ledger knows a write was attempted
        try:
            recon_db = _db()
            recon_db.execute(
                """INSERT INTO trust_events
                   (event_id, timestamp, action_type, delta, reason, resulting_trust)
                   VALUES (?, ?, ?, 0, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    int(time.time()),
                    action_type,
                    f"RECONCILIATION:write_failed:{str(e)[:120]}",
                    trust_before,
                )
            )
            recon_db.commit()
            recon_db.close()
        except Exception:
            pass

        return {
            "success":     False,
            "error_code":  "TRUST_WRITE_FAILED",
            "detail":      str(e),
            "action_type": action_type,
        }
