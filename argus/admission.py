"""
ARGUS — Phase 8 Part 3: atomic admission (duplicate detection + business rate
limiting), fused into ONE transaction with canonical-proposal storage.

admit() is the gate for agent proposal creation. run_agent() calls it (when
ARGUS_ADMISSION_ENABLED != "0") just before a proposal would be stored. In one
SQLite transaction it either suppresses an exact duplicate, rejects an over-
budget request, or stores the proposal + claims the dedup slot + reserves a rate
slot + writes the AGENT_PROPOSAL audit event — all-or-nothing.

SCOPE: admission gates the agent (AI) proposal-creation path, which is the only
path the demo UI uses. The raw /api/propose API is STILL fully governed by the
deterministic policy + safety + trust engine (kernel_entry); it is intentionally
NOT rate/dedup limited (it is the engine surface used by the test suite, not an
AI action). Hardening that path is deferred.

Deduplication is EXACT-canonical, NOT semantic: two differently-drafted bodies of
the same intent are distinct proposals and are not suppressed.
"""
import os
import json
import time
import uuid
import sqlite3
import hashlib
from email.utils import parseaddr

DATABASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'argus.db')

BUSINESS_LIMIT = 10     # actions per rolling window
WINDOW = 3600           # 60 minutes
BUCKET = 60             # minute granularity
DEDUP_TTL = 60          # duplicate-suppression window (seconds)


def admission_enabled():
    return os.environ.get("ARGUS_ADMISSION_ENABLED", "1") != "0"


def _normalize_recipient(raw):
    """Canonical address (display text stripped, lowercased) via the same
    parseaddr extraction the safety filter uses — never hash display text."""
    if not isinstance(raw, str):
        return raw
    _name, addr = parseaddr(raw)
    return (addr or raw).strip().lower()


def _norm_text(s):
    return " ".join(s.split()) if isinstance(s, str) else s


def proposal_hash(proposal):
    """Exact-canonical SHA-256 over action_type + every code-owned entity field,
    so distinct non-body actions (different destination/label/email_id) never
    collapse to one hash. Recipient normalized; all string values whitespace-
    canonicalized; key order stable."""
    e = dict(proposal.get("entities") or {})
    norm = {}
    for k, v in e.items():
        if k == "recipient":
            norm[k] = _normalize_recipient(v)
        elif isinstance(v, str):
            norm[k] = _norm_text(v)
        else:
            norm[k] = v
    canonical = {"action_type": proposal.get("action_type"), "entities": norm}
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def admit(proposal, user_id="owner"):
    """Atomic admission + canonical proposal storage. Returns one of:
      {admitted:True,  proposal_id, proposal_hash}
      {admitted:False, reason:'DUPLICATE_SUPPRESSED', existing_proposal_id}
      {admitted:False, reason:'RATE_LIMIT_EXCEEDED', retry_at}
      {admitted:False, reason:'BUSY'|'AUDIT_WRITE_FAILED'|'ADMISSION_FAILED'}
    Time is DB-owned (read inside the transaction). Denials commit an audit-only
    transaction (no rate/dedup mutation); a failed audit fails closed."""
    from argus import audit
    cat = f"business:{user_id}"
    h = proposal_hash(proposal)
    conn = sqlite3.connect(DATABASE, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return {"admitted": False, "reason": "BUSY", "retryable": True}

        now = int(conn.execute("SELECT CAST(strftime('%s','now') AS INTEGER)").fetchone()[0])

        # (a) duplicate within the active window
        row = conn.execute(
            "SELECT proposal_id, expires_at FROM proposal_dedup WHERE user_id=? AND proposal_hash=?",
            (user_id, h)).fetchone()
        if row and row["expires_at"] > now:
            rec = audit.record("DUPLICATE_SUPPRESSED", action_type=proposal.get("action_type"),
                               outcome="SUPPRESSED",
                               payload={"proposal_hash": h, "existing": row["proposal_id"]}, db=conn)
            if not rec.get("recorded"):
                conn.execute("ROLLBACK")
                return {"admitted": False, "reason": "AUDIT_WRITE_FAILED"}
            conn.execute("COMMIT")
            return {"admitted": False, "reason": "DUPLICATE_SUPPRESSED",
                    "existing_proposal_id": row["proposal_id"]}

        # (b) business rate limit — conservative rolling minute-buckets.
        # purge buckets fully outside the conservative window (bounds growth)
        conn.execute("DELETE FROM rate_limits WHERE action_category=? AND window_start<=?",
                     (cat, now - WINDOW - BUCKET))
        # include any bucket whose END can overlap [now-3600, now]: window_start > now-3660
        agg = conn.execute(
            "SELECT COALESCE(SUM(count),0) AS c, MIN(window_start) AS oldest FROM rate_limits "
            "WHERE action_category=? AND window_start > ?", (cat, now - WINDOW - BUCKET)).fetchone()
        if (agg["c"] or 0) >= BUSINESS_LIMIT:
            retry_at = int(agg["oldest"]) + WINDOW + BUCKET  # when the oldest bucket fully exits
            rec = audit.record("RATE_LIMIT_EXCEEDED", action_type=proposal.get("action_type"),
                               outcome="REJECTED",
                               payload={"retry_at": retry_at, "count": int(agg["c"])}, db=conn)
            if not rec.get("recorded"):
                conn.execute("ROLLBACK")
                return {"admitted": False, "reason": "AUDIT_WRITE_FAILED"}
            conn.execute("COMMIT")
            return {"admitted": False, "reason": "RATE_LIMIT_EXCEEDED", "retry_at": retry_at}

        # (c) admit: store proposal + claim dedup + reserve rate + audit, atomically
        pid = str(uuid.uuid4())
        conn.execute("INSERT INTO agent_proposals (id, proposal_json, status, created_at) "
                     "VALUES (?, ?, 'PROPOSAL', ?)", (pid, json.dumps(proposal), now))
        conn.execute(
            "INSERT INTO proposal_dedup (user_id, proposal_hash, proposal_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(user_id, proposal_hash) DO UPDATE SET "
            "proposal_id=excluded.proposal_id, created_at=excluded.created_at, expires_at=excluded.expires_at",
            (user_id, h, pid, now, now + DEDUP_TTL))
        bucket = (now // BUCKET) * BUCKET
        conn.execute(
            "INSERT INTO rate_limits (action_category, window_start, count) VALUES (?,?,1) "
            "ON CONFLICT(action_category, window_start) DO UPDATE SET count=count+1", (cat, bucket))
        rec = audit.record("AGENT_PROPOSAL", correlation_id=pid,
                           idempotency_key=f"{pid}:AGENT_PROPOSAL",
                           action_type=proposal.get("action_type"), outcome="PROPOSAL",
                           payload={"action_type": proposal.get("action_type"),
                                    "has_body": "body" in (proposal.get("entities") or {}),
                                    "proposal_hash": h}, db=conn)
        if not rec.get("recorded"):
            conn.execute("ROLLBACK")
            return {"admitted": False, "reason": "AUDIT_WRITE_FAILED"}
        conn.execute("COMMIT")
        return {"admitted": True, "proposal_id": pid, "proposal_hash": h}
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return {"admitted": False, "reason": "ADMISSION_FAILED", "detail": str(e)[:120]}
    finally:
        conn.close()
