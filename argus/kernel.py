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

    is_valid, cleaned, error_code = validate_proposal(proposal)
    if not is_valid:
        return {
            "decision": "BLOCK",
            "failure_type": "VALIDATION",
            "failure_reason_code": error_code,
            "trace": [f"Validation failed: {error_code}"],
            "conflict_resolution_path": ["VALIDATION"],
            "trust_impact": "none",
            "action_expiry": 0
        }

    return evaluate(cleaned)
