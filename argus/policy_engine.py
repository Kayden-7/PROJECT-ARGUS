import sqlite3
import os
from config import FREE_ACTIONS, APPROVAL_EXPIRY_SECONDS, PROFILE_THRESHOLDS

DATABASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'argus.db')

# Severity tier per action type
ACTION_SEVERITY = {
    "email.compose":       "TRIVIAL",
    "email.archive":       "TRIVIAL",
    "email.mark_read":     "TRIVIAL",
    "email.star":          "TRIVIAL",
    "email.move":          "TRIVIAL",
    "calendar.accept":     "TRIVIAL",
    "calendar.label":      "TRIVIAL",
    "calendar.color":      "TRIVIAL",
    "label.apply":         "TRIVIAL",
    "email.reply":         "LOW",
    "email.forward":       "LOW",
    "email.send.internal": "LOW",
    "calendar.decline":    "LOW",
    "email.send.external": "MEDIUM",
    "calendar.create":     "MEDIUM",
    "calendar.modify":     "MEDIUM",
    "calendar.invite":     "MEDIUM",
    "calendar.reschedule": "MEDIUM",
    "email.delete":        "HIGH",
    "calendar.delete":     "HIGH",
}

# (success_delta, failure_delta) per severity tier
SEVERITY_DELTAS = {
    "TRIVIAL": (0.5,  -5),
    "LOW":     (3,   -12),
    "MEDIUM":  (7,   -18),
    "HIGH":    (10,  -20),
}

SEVERITY_ORDER    = ["TRIVIAL", "LOW", "MEDIUM", "HIGH"]
STARTING_TRUST    = 40.0
INERTIA_THRESHOLD = 5    # first N executions are dampened
INERTIA_WEIGHT    = 0.5
OVERALL_MOD_START = 1.0


# ── DB helpers (each returns None / False to signal failure) ──────────────────

def _db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db


def _read_prime_rules(db):
    try:
        return [r["action_type"] for r in db.execute("SELECT action_type FROM prime_rules").fetchall()]
    except Exception:
        return None


def _read_policy_gate(db, action_type):
    try:
        return db.execute(
            "SELECT min_threshold, base_threshold FROM policy_gates WHERE action_type=?",
            (action_type,)
        ).fetchone()
    except Exception:
        return False   # False = DB error (None = no record found)


def _read_contact(db, recipient, action_type):
    try:
        return db.execute(
            "SELECT relax_amount FROM contact_permissions WHERE contact=? AND action_type=?",
            (recipient, action_type)
        ).fetchone()
    except Exception:
        return False


def _read_trust(db, action_type):
    try:
        row = db.execute(
            "SELECT trust_current FROM trust_current WHERE action_type=?", (action_type,)
        ).fetchone()
        return float(row["trust_current"]) if row else STARTING_TRUST
    except Exception:
        return None


def _read_active_profile_threshold(db):
    try:
        row = db.execute(
            "SELECT value FROM system_state WHERE key='ACTIVE_PROFILE'"
        ).fetchone()
        profile = row["value"] if row else "Balanced"
        return PROFILE_THRESHOLDS.get(profile, 70)
    except Exception:
        return None


def _read_action_count(db, action_type):
    try:
        row = db.execute(
            "SELECT COUNT(*) AS cnt FROM trust_events WHERE action_type=?", (action_type,)
        ).fetchone()
        return row["cnt"] if row else 0
    except Exception:
        return 0


def _read_overall_modifier(db):
    try:
        row = db.execute(
            "SELECT value FROM system_state WHERE key='OVERALL_TRUST_MODIFIER'"
        ).fetchone()
        return float(row["value"]) if row else OVERALL_MOD_START
    except Exception:
        return OVERALL_MOD_START


# ── Response builders ─────────────────────────────────────────────────────────

def _step(name, result, reason, before=None, after=None):
    return {"step": name, "result": result, "reason": reason, "before": before, "after": after}


def _block(source, code, narrative, trace):
    return {
        "decision":           "BLOCK",
        "decision_source":    source,
        "failure_type":       "EMERGENCY" if source == "SYSTEM_HARD_STOP" else "POLICY",
        "failure_reason_code": code,
        "terminated_at":      source,
        "trace":              trace,
        "trust_at_evaluation": None,
        "effective_threshold": None,
        "trust_impact":       "none",
        "trust_delta_preview": None,
        "action_expiry":      0,
        "narrative":          narrative,
        "modifier_breakdown": {},
    }


def _allow(source, trust, threshold, expiry, trace, breakdown, narrative):
    return {
        "decision":            "ALLOW",
        "decision_source":     source,
        "failure_type":        None,
        "failure_reason_code": None,
        "terminated_at":       source,
        "trace":               trace,
        "trust_at_evaluation": trust,
        "effective_threshold": threshold,
        "trust_impact":        "pending_positive",
        "trust_delta_preview": None,
        "action_expiry":       expiry,
        "narrative":           narrative,
        "modifier_breakdown":  breakdown,
    }


def _gated(trust, threshold, expiry, trace, breakdown, narrative):
    return {
        "decision":            "GATED",
        "decision_source":     "TRUST_CHECK",
        "failure_type":        "TRUST",
        "failure_reason_code": "TRUST_BELOW_THRESHOLD",
        "terminated_at":       "TRUST_CHECK",
        "trace":               trace,
        "trust_at_evaluation": trust,
        "effective_threshold": threshold,
        "trust_impact":        "pending_negative",
        "trust_delta_preview": None,
        "action_expiry":       expiry,
        "narrative":           narrative,
        "modifier_breakdown":  breakdown,
    }


def _db_fail_gated(trace):
    return _gated(0, 0, 0, trace, {}, "Database unavailable — action queued for safety.")


# ── Main evaluation function ──────────────────────────────────────────────────

def evaluate(proposal: dict) -> dict:
    action_type = proposal.get("action_type")
    recipient   = proposal.get("entities", {}).get("recipient", "")
    importance  = proposal.get("importance", "normal")
    expiry      = proposal.get("action_expiry", APPROVAL_EXPIRY_SECONDS)
    trace       = []
    breakdown   = {}

    try:
        db = _db()
    except Exception:
        return _db_fail_gated([_step("DB_CONNECT", "FAIL", "Cannot open database")])

    try:
        # ── Layer 1: SYSTEM_HARD_STOP (already checked in kernel — log as passed) ──
        trace.append(_step("SYSTEM_HARD_STOP", "PASS", "Hard stop not active"))

        # ── Layer 2: Prime Rules ──────────────────────────────────────────────────
        prime_rules = _read_prime_rules(db)
        if prime_rules is None:
            return _db_fail_gated(trace + [_step("PRIME_RULES", "DB_FAIL", "Cannot read prime rules")])

        if action_type in prime_rules:
            trace.append(_step("PRIME_RULE", "BLOCK", f"{action_type} is a Prime Rule"))
            return _block("PRIME_RULE", "PRIME_RULE_MATCH",
                          "This decision belongs to you. ARGUS will not act on this.", trace)

        trace.append(_step("PRIME_RULE_CHECK", "PASS", "No prime rule match"))

        # ── Layer 3: FREE action ──────────────────────────────────────────────────
        if action_type in FREE_ACTIONS:
            trace.append(_step("FREE_ACTION_CHECK", "ALLOW", f"{action_type} is FREE"))
            return _allow("FREE_ACTION", None, None, expiry, trace, breakdown,
                          f"{action_type} is a free action — no approval required.")

        trace.append(_step("FREE_ACTION_CHECK", "PASS", f"{action_type} is GATED — continuing"))

        # ── Layer 4: Policy gate ──────────────────────────────────────────────────
        gate = _read_policy_gate(db, action_type)
        if gate is False:
            return _db_fail_gated(trace + [_step("POLICY_GATE", "DB_FAIL", "Cannot read policy gate")])
        if gate is None:
            trace.append(_step("POLICY_GATE", "BLOCK", f"No gate record for {action_type}"))
            return _block("POLICY_GATE", "NO_GATE_RECORD",
                          f"No policy gate defined for {action_type}. Blocked for safety.", trace)

        min_threshold     = float(gate["min_threshold"])
        profile_threshold = _read_active_profile_threshold(db)
        if profile_threshold is None:
            return _db_fail_gated(trace + [_step("POLICY_GATE", "DB_FAIL", "Cannot read active profile")])
        trace.append(_step("POLICY_GATE", "PASS", f"min={min_threshold} profile_threshold={profile_threshold}"))

        # ── Layer 5: Contact permission ───────────────────────────────────────────
        trust_modifier = 0.0
        contact = _read_contact(db, recipient, action_type)
        if contact is False:
            return _db_fail_gated(trace + [_step("CONTACT_PERMISSION", "DB_FAIL", "Cannot read contact permissions")])

        if contact is not None:
            trust_modifier = max(0.0, float(contact["relax_amount"]))
            trace.append(_step("CONTACT_PERMISSION", "RELAXED",
                               f"Threshold relaxed by {trust_modifier}", 0.0, trust_modifier))
        else:
            trace.append(_step("CONTACT_PERMISSION", "PASS", "No contact record — threshold unchanged",
                               trust_modifier, trust_modifier))

        breakdown["contact_relax"] = trust_modifier

        # ── Layer 6: Trust check ──────────────────────────────────────────────────

        # Step 1 — importance → severity bump
        base_severity = ACTION_SEVERITY.get(action_type, "MEDIUM")
        severity = base_severity
        if importance == "high":
            idx = SEVERITY_ORDER.index(severity)
            severity = SEVERITY_ORDER[min(idx + 1, len(SEVERITY_ORDER) - 1)]
        trace.append(_step("IMPORTANCE_CHECK",
                           "BUMPED" if severity != base_severity else "SKIPPED",
                           f"importance={importance} → severity {base_severity}→{severity}",
                           base_severity, severity))

        # Step 2 — severity → base deltas
        success_delta, failure_delta = SEVERITY_DELTAS.get(severity, (7, -18))
        breakdown["severity"]      = severity
        breakdown["success_delta"] = success_delta
        breakdown["failure_delta"] = failure_delta

        # Step 3 — read trust + inertia
        trust_score = _read_trust(db, action_type)
        if trust_score is None:
            return _db_fail_gated(trace + [_step("TRUST_READ", "DB_FAIL", "Cannot read trust score")])

        action_count   = _read_action_count(db, action_type)
        inertia_weight = INERTIA_WEIGHT if action_count < INERTIA_THRESHOLD else 1.0
        breakdown["trust_at_evaluation"] = trust_score
        breakdown["action_count"]        = action_count
        breakdown["inertia_weight"]      = inertia_weight
        trace.append(_step("TRUST_READ", "OK",
                           f"trust={trust_score:.1f} count={action_count} inertia={inertia_weight}",
                           None, trust_score))

        # Step 4 — effective threshold
        effective_threshold = max(min_threshold, profile_threshold - trust_modifier)
        breakdown["effective_threshold"] = effective_threshold
        breakdown["trust_modifier"]      = trust_modifier

        # Step 5 — overall modifier (for trust ledger to use, not the decision)
        overall_modifier         = _read_overall_modifier(db)
        breakdown["overall_modifier"] = overall_modifier

        # Decision
        if trust_score >= effective_threshold:
            trace.append(_step("TRUST_CHECK", "ALLOW",
                               f"trust {trust_score:.1f} >= threshold {effective_threshold:.1f}",
                               trust_score, trust_score))
            return _allow("TRUST_CHECK", trust_score, effective_threshold, expiry, trace, breakdown,
                          f"Trust {trust_score:.1f} meets threshold {effective_threshold:.1f}. Auto-executing.")
        else:
            trace.append(_step("TRUST_CHECK", "GATED",
                               f"trust {trust_score:.1f} < threshold {effective_threshold:.1f}",
                               trust_score, trust_score))
            return _gated(trust_score, effective_threshold, expiry, trace, breakdown,
                          f"Trust {trust_score:.1f} is below threshold {effective_threshold:.1f}. Queued for approval.")

    except Exception as e:
        return _db_fail_gated([_step("EVALUATE", "EXCEPTION", str(e))])
    finally:
        db.close()
