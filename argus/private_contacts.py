"""
ARGUS — Phase 8 Part 4 / CONTROL 3: private-contact protection.

A small owner-curated list of addresses ARGUS must never act on or send to. The
match is EXACT normalized address only (lowercased full address, display text
stripped) — no +tag stripping, no name match — and it applies to BOTH the
selected-email SOURCE (its sender) and the OUTGOING recipient / forward target.

Two enforcement points (defence in depth):
  1. Entry gate (argus.agent.run_agent): checked after grounding, before any GPT
     drafting exposure. A hit yields NO proposal and NO queue item.
  2. Executor preflight (argus.executor): re-checked just before a Gmail send, so
     a contact added AFTER approval still blocks a pending send.

A hit is audited with a REDACTED contact reference only — the raw address never
enters the audit payload. List mutations are owner-only (enforced at the
endpoint) and each commits atomically with its own audit event.
"""
import os
import time
import sqlite3
from email.utils import parseaddr

DATABASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'argus.db')

REASON_MAX = 500  # CONTROL 6: reason-like fields are capped, never truncated.

# Entity fields that carry an outgoing destination address. Kept explicit so a
# new action type can't silently introduce an unchecked recipient field.
_RECIPIENT_FIELDS = ("recipient",)


def _normalize(raw):
    """Canonical address: display text stripped, lowercased. Same extraction the
    safety filter and admission use, so the three agree on what an address is."""
    if not isinstance(raw, str):
        return ""
    _name, addr = parseaddr(raw)
    return (addr or "").strip().lower()


def _redact(norm):
    """Recognizable-but-redacted ref for audit, e.g. 'boss@company.com' ->
    'b***@c***.com'. Never reversible; the raw address never reaches audit."""
    if not norm or "@" not in norm:
        return "***"
    local, _, domain = norm.partition("@")
    parts = domain.rsplit(".", 1)
    tld = parts[1] if len(parts) == 2 else ""
    dlead = domain[0] if domain else "*"
    llead = local[0] if local else "*"
    return f"{llead}***@{dlead}***" + (f".{tld}" if tld else "")


def _db(conn=None):
    if conn is not None:
        return conn, False
    c = sqlite3.connect(DATABASE)
    c.row_factory = sqlite3.Row
    return c, True


# ── Matching (read path) ──────────────────────────────────────────────────────

def is_private(email, conn=None):
    """True iff `email` normalizes to an ENABLED private contact. Fail-closed: any
    read error is treated as a non-match here ONLY because both call sites wrap a
    broader decision; callers that must fail closed check the return explicitly."""
    norm = _normalize(email)
    if not norm:
        return False
    c, own = _db(conn)
    try:
        row = c.execute(
            "SELECT 1 FROM private_contacts WHERE normalized_email=? AND enabled=1",
            (norm,)).fetchone()
        return row is not None
    finally:
        if own:
            c.close()


def check_targets(action_type, entities, source_sender=None, conn=None):
    """Return {field, redacted} for the FIRST protected address among the source
    email's sender and the action's outgoing recipient(s), else None.

    `source_sender` is the raw sender of the selected email (if any) — protecting
    it stops both acting on a private contact's message and forwarding it out.
    """
    entities = entities or {}
    c, own = _db(conn)
    try:
        if source_sender and is_private(source_sender, conn=c):
            return {"field": "source", "redacted": _redact(_normalize(source_sender))}
        for f in _RECIPIENT_FIELDS:
            val = entities.get(f)
            if val and is_private(val, conn=c):
                return {"field": f, "redacted": _redact(_normalize(val))}
        return None
    finally:
        if own:
            c.close()


# ── List read ─────────────────────────────────────────────────────────────────

def list_contacts(include_disabled=False):
    """Owner view of the list. Returns enabled contacts by default."""
    c, own = _db()
    try:
        q = ("SELECT id, normalized_email, display_label, enabled, created_at, updated_at "
             "FROM private_contacts")
        if not include_disabled:
            q += " WHERE enabled=1"
        q += " ORDER BY normalized_email ASC"
        return [dict(r) for r in c.execute(q).fetchall()]
    finally:
        if own:
            c.close()


# ── Mutations (owner-only at the endpoint; each atomic with its audit) ─────────

def _validate_reason(reason):
    if reason is None:
        return None, None
    if not isinstance(reason, str):
        return None, "INVALID_REASON"
    if len(reason) > REASON_MAX:
        return None, "REJECTION_REASON_TOO_LONG"
    return reason, None


def add_contact(email, display_label=None, updated_by="owner", reason=None):
    """Add (or re-enable) a private contact. UPSERT on normalized_email so a
    re-add flips enabled back on without a duplicate row. Atomic with audit."""
    from argus import audit
    norm = _normalize(email)
    if not norm or "@" not in norm:
        return {"success": False, "error_code": "INVALID_EMAIL"}
    reason, rerr = _validate_reason(reason)
    if rerr:
        return {"success": False, "error_code": rerr}

    conn = sqlite3.connect(DATABASE, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        prev = conn.execute(
            "SELECT enabled FROM private_contacts WHERE normalized_email=?", (norm,)).fetchone()
        was_enabled = bool(prev["enabled"]) if prev else False
        conn.execute(
            "INSERT INTO private_contacts (normalized_email, display_label, enabled, created_at, updated_at) "
            "VALUES (?,?,1,?,?) ON CONFLICT(normalized_email) DO UPDATE SET "
            "enabled=1, display_label=COALESCE(excluded.display_label, private_contacts.display_label), "
            "updated_at=excluded.updated_at",
            (norm, display_label, now, now))
        rec = audit.record("PRIVATE_CONTACT_ADDED", action_type=None, outcome="ADDED",
                           reason=reason,
                           payload={"actor": updated_by, "contact": _redact(norm),
                                    "old": {"enabled": was_enabled}, "new": {"enabled": True}},
                           db=conn)
        if not rec.get("recorded"):
            conn.execute("ROLLBACK")
            return {"success": False, "error_code": "AUDIT_WRITE_FAILED"}
        conn.execute("COMMIT")
        return {"success": True, "contact": _redact(norm), "reactivated": bool(prev) and not was_enabled}
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return {"success": False, "error_code": "MUTATION_FAILED", "detail": str(e)[:120]}
    finally:
        conn.close()


def remove_contact(email, updated_by="owner", reason=None):
    """Soft-disable a private contact (enabled=0), keeping the row for history.
    Idempotent: removing an absent/already-disabled contact is a no-op success."""
    from argus import audit
    norm = _normalize(email)
    if not norm:
        return {"success": False, "error_code": "INVALID_EMAIL"}
    reason, rerr = _validate_reason(reason)
    if rerr:
        return {"success": False, "error_code": rerr}

    conn = sqlite3.connect(DATABASE, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        row = conn.execute(
            "SELECT enabled FROM private_contacts WHERE normalized_email=?", (norm,)).fetchone()
        if not row or not row["enabled"]:
            conn.execute("COMMIT")  # nothing to change; no audit noise
            return {"success": True, "changed": False}
        conn.execute(
            "UPDATE private_contacts SET enabled=0, updated_at=? WHERE normalized_email=?",
            (now, norm))
        rec = audit.record("PRIVATE_CONTACT_REMOVED", action_type=None, outcome="REMOVED",
                           reason=reason,
                           payload={"actor": updated_by, "contact": _redact(norm),
                                    "old": {"enabled": True}, "new": {"enabled": False}},
                           db=conn)
        if not rec.get("recorded"):
            conn.execute("ROLLBACK")
            return {"success": False, "error_code": "AUDIT_WRITE_FAILED"}
        conn.execute("COMMIT")
        return {"success": True, "changed": True}
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return {"success": False, "error_code": "MUTATION_FAILED", "detail": str(e)[:120]}
    finally:
        conn.close()
