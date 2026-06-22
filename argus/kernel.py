import sqlite3
import os

DATABASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'argus.db')


def is_hard_stop() -> bool:
    try:
        db = sqlite3.connect(DATABASE)
        row = db.execute("SELECT value FROM system_state WHERE key='SYSTEM_HARD_STOP'").fetchone()
        db.close()
        return row is not None and row[0] == '1'
    except Exception:
        return True


def set_hard_stop(value: bool) -> None:
    db = sqlite3.connect(DATABASE)
    db.execute("UPDATE system_state SET value=? WHERE key='SYSTEM_HARD_STOP'", ('1' if value else '0',))
    db.commit()
    db.close()


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
