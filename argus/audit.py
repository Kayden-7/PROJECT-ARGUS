"""
ARGUS — Audit trail (Phase 7)

Append-only, tamper-evident event log. Every material decision, queue
transition, execution, trust change, and agent proposal is recorded with a
SHA-256 hash chain. Locked after 2 stress-test passes (✖ → ✔).

Honest scope:
- The chain proves the RETAINED log is internally consistent. It does NOT prove
  completeness or external non-tampering (a host admin can replace the DB).
- Payloads are CODE-OWNED only — no email bodies, subjects, tokens, prompts, or
  model text. Recipient is stored as a coarse scope, never the address/body.
- Idempotency is keyed on a per-source key so reconcile-on-read can't duplicate
  an event, while legitimate repeated event types stay representable.
"""
import os
import json
import time
import uuid
import hashlib
import sqlite3

DATABASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'argus.db')


def _db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db


# Defense in depth: the audit writer is the LAST line — even if a caller is
# careless, these keys never get persisted. Audit payloads must be code-owned
# metadata only (no email content, recipients, drafts, or model text).
_SENSITIVE_KEYS = {
    "body", "subject", "recipient", "draft", "content", "raw", "html", "text",
    "message", "to", "cc", "bcc", "entities", "command", "prompt",
}


def _scrub(value):
    if isinstance(value, dict):
        return {k: ("[redacted]" if str(k).lower() in _SENSITIVE_KEYS else _scrub(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _canonical(payload):
    # Scrub first, then fixed encoding so hashes are reproducible.
    return json.dumps(_scrub(payload) or {}, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, default=str)


def _hash(canon, prev_hash, ts, event_type, idem):
    return hashlib.sha256(
        (canon + (prev_hash or "") + str(ts) + event_type + (idem or "")).encode("utf-8")
    ).hexdigest()


def record(event_type, correlation_id=None, idempotency_key=None,
           action_type=None, outcome=None, reason=None, payload=None, db=None):
    """
    Append one audit event (hash-chained). If `db` is given, writes within the
    caller's transaction (caller commits) so the event commits atomically with
    its source mutation. Duplicate idempotency_key → no-op.
    """
    own = db is None
    if own:
        # Own connection: serialize the read-prev + insert with BEGIN IMMEDIATE so
        # two concurrent writers can't both chain off the same hash (no fork).
        conn = sqlite3.connect(DATABASE, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
        except Exception:
            pass
    else:
        conn = db  # caller's transaction already holds the write lock → serialized
    try:
        if idempotency_key and conn.execute(
                "SELECT 1 FROM audit_events WHERE idempotency_key=?", (idempotency_key,)).fetchone():
            if own:
                conn.execute("COMMIT")
            return {"recorded": False, "reason": "duplicate"}
        if idempotency_key is None:
            idempotency_key = str(uuid.uuid4())

        now = int(time.time())
        prev = conn.execute("SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = prev["entry_hash"] if prev else ""
        canon = _canonical(payload)
        entry_hash = _hash(canon, prev_hash, now, event_type, idempotency_key)

        conn.execute(
            "INSERT OR IGNORE INTO audit_events (timestamp, event_type, correlation_id, "
            "action_type, outcome, reason, idempotency_key, payload_json, prev_entry_hash, entry_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now, event_type, correlation_id, action_type, outcome, reason,
             idempotency_key, canon, prev_hash, entry_hash))
        if own:
            conn.execute("COMMIT")
        return {"recorded": True, "entry_hash": entry_hash}
    except Exception as e:
        if own:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        return {"recorded": False, "error": str(e)}
    finally:
        if own:
            conn.close()


def safe_record(*args, **kwargs):
    """Best-effort record that never raises (for non-material call sites)."""
    try:
        return record(*args, **kwargs)
    except Exception:
        return {"recorded": False}


# ── Reads ─────────────────────────────────────────────────────────────────────

def _row(r):
    d = dict(r)
    try:
        d["payload"] = json.loads(d.pop("payload_json", "{}") or "{}")
    except Exception:
        d["payload"] = {}
    return d


def recent(limit=100):
    limit = max(1, min(int(limit or 100), 500))
    conn = _db()
    rows = conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [_row(r) for r in rows]


def verify_chain():
    conn = _db()
    rows = conn.execute("SELECT * FROM audit_events ORDER BY id ASC").fetchall()
    conn.close()
    prev_hash = ""
    checked = 0
    for r in rows:
        expected = _hash(r["payload_json"], prev_hash, r["timestamp"], r["event_type"], r["idempotency_key"])
        if (r["prev_entry_hash"] or "") != prev_hash or r["entry_hash"] != expected:
            return {"valid": False, "checked": checked, "broken_at_id": r["id"],
                    "note": "The retained audit chain is internally INCONSISTENT."}
        prev_hash = r["entry_hash"]
        checked += 1
    return {"valid": True, "checked": checked, "chain_head": prev_hash,
            "note": ("The retained audit chain is internally consistent. This does not "
                     "prove completeness or external non-tampering.")}


def replay(correlation_id):
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM audit_events WHERE correlation_id=? ORDER BY id ASC", (correlation_id,)
    ).fetchall()
    conn.close()
    return {
        "correlation_id": correlation_id,
        "label": "Historical replay — reconstructed from records stored at evaluation time; "
                 "NOT recomputed against current policy or trust.",
        "events": [_row(r) for r in rows],
    }


def summary(since=0):
    """Action-level aggregates (by lifecycle, not raw event counts)."""
    since = int(since or 0)
    conn = _db()
    rows = conn.execute("SELECT * FROM audit_events WHERE timestamp>=? ORDER BY id ASC", (since,)).fetchall()
    conn.close()
    events = [_row(r) for r in rows]

    decisions = [e for e in events if e["event_type"] == "DECISION_EVALUATED"]
    final = {"ALLOW": 0, "GATED": 0, "BLOCK": 0}
    downgrades = {}
    candidate_allow_downgraded = 0
    for e in decisions:
        p = e["payload"]
        fo = p.get("final_outcome")
        if fo in final:
            final[fo] += 1
        if p.get("candidate_decision") == "ALLOW" and fo == "GATED":
            candidate_allow_downgraded += 1
        for reason in (p.get("safety_downgrade_reasons") or []):
            downgrades[reason] = downgrades.get(reason, 0) + 1

    transitions = [e for e in events if e["event_type"] == "QUEUE_TRANSITIONED"]
    approvals = sum(1 for e in transitions if e["outcome"] == "APPROVED")
    rejections = sum(1 for e in transitions if e["outcome"] == "REJECTED")
    cancellations = sum(1 for e in transitions if e["outcome"] == "CANCELLED")

    resolved = [e for e in events if e["event_type"] == "EXECUTION_RESOLVED"]
    executed_ok = sum(1 for e in resolved if e["outcome"] == "COMPLETED")
    manual_review = sum(1 for e in resolved if e["outcome"] == "MANUAL_REVIEW")

    trust_events = [e for e in events if e["event_type"] == "TRUST_CHANGED"]
    trust_delta_total = round(sum((e["payload"].get("delta") or 0) for e in trust_events), 2)

    reviewable = approvals + rejections
    return {
        "since": since,
        "decisions": {"total": len(decisions), **final,
                      "candidate_allow_downgraded_to_gated": candidate_allow_downgraded},
        "safety_downgrades_by_reason": downgrades,
        "human_oversight": {"approvals": approvals, "rejections": rejections,
                            "cancellations": cancellations,
                            "approval_rate": round(approvals / reviewable, 3) if reviewable else None},
        "execution": {"completed": executed_ok, "manual_review_unresolved": manual_review},
        "trust": {"changes": len(trust_events), "net_delta": trust_delta_total},
        "note": "Rates count reviewable lifecycles only; non-reviewable blocks and "
                "agent states are excluded. MANUAL_REVIEW is unresolved, not failed.",
    }
