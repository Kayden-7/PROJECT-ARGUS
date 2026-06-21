"""
ARGUS — Message templates (Phase 5 Part 3)

Templates are a STYLE POLICY only: tone/length/format boundaries per
contact+action. They never authorize, redirect, add recipients, change the
subject, or alter the requested action — code owns all of that.

Design locked after brainstorm + stress test:
- Structured allowlisted fields only; NO free-form instruction field.
- `avoid_phrases` is VALIDATOR-ONLY — never rendered into the model prompt.
- Resolution picks exactly one row by precedence; multiple rows at the winning
  rank fail closed to MANUAL_REVIEW.
- A full settings SNAPSHOT is pinned on the proposal at resolve time, so later
  template edits/deletes can never change what a pinned proposal executes.
- The validator proves template CONFORMANCE (counts, banned phrases, structural
  headers) — not intent preservation or universal metadata detection.

GPT-4o consumes render_style_block()/validate_body() in Phase 9; this module
only stores, resolves, renders, and validates.
"""
import sqlite3
import os
import time
import uuid
import json
import re

DATABASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'argus.db')

TONES        = {'warm', 'neutral', 'direct', 'formal', 'friendly'}
FORMALITIES  = {'casual', 'professional', 'formal'}
LENGTHS      = {'brief', 'standard', 'detailed'}
GREETINGS    = {'none', 'first_name', 'formal_name'}
SIGNOFFS     = {'none', 'thanks', 'regards', 'best'}

MAX_AVOID_PHRASES = 20
MAX_AVOID_LEN     = 60

# Code-owned conservative default when no template matches. Not user-editable.
CONSERVATIVE_DEFAULT = {
    "tone": "neutral", "formality": "professional", "length_class": "brief",
    "greeting_style": "first_name", "signoff_style": "thanks",
    "max_words": 120, "max_sentences": 8, "max_paragraphs": 3,
    "avoid_phrases": [],
}

# Structural header / metadata forms the validator rejects in a body.
_HEADER_RE = re.compile(
    r'(?im)^\s*(to|cc|bcc|subject|from|reply-to|content-type|mime-version)\s*:',
)
_MIME_RE = re.compile(r'(?m)^--[-=_A-Za-z0-9]{6,}')


def _db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db


def canonical_contact(contact):
    if not contact:
        return None
    return contact.strip().lower() or None


# ── Validation on save ───────────────────────────────────────────────────────

def _validate_settings(s):
    """Return list of errors; empty = valid. Rejects impossible/contradictory configs."""
    errors = []
    if s.get("tone") not in TONES:                 errors.append("invalid tone")
    if s.get("formality") not in FORMALITIES:      errors.append("invalid formality")
    if s.get("length_class") not in LENGTHS:       errors.append("invalid length_class")
    if s.get("greeting_style") not in GREETINGS:   errors.append("invalid greeting_style")
    if s.get("signoff_style") not in SIGNOFFS:     errors.append("invalid signoff_style")

    mw, ms, mp = s.get("max_words"), s.get("max_sentences"), s.get("max_paragraphs")
    if not isinstance(mw, int) or not (10 <= mw <= 1000):  errors.append("max_words out of range (10-1000)")
    if not isinstance(ms, int) or not (1 <= ms <= 30):     errors.append("max_sentences out of range (1-30)")
    if not isinstance(mp, int) or not (1 <= mp <= 8):      errors.append("max_paragraphs out of range (1-8)")
    # Cross-field sanity: at least one word per sentence, one sentence per paragraph.
    if isinstance(mw, int) and isinstance(ms, int) and mw < ms:
        errors.append("impossible config: max_words < max_sentences")
    if isinstance(ms, int) and isinstance(mp, int) and ms < mp:
        errors.append("impossible config: max_sentences < max_paragraphs")

    ap = s.get("avoid_phrases", []) or []
    if not isinstance(ap, list):
        errors.append("avoid_phrases must be a list")
    else:
        if len(ap) > MAX_AVOID_PHRASES:
            errors.append(f"too many avoid_phrases (max {MAX_AVOID_PHRASES})")
        for p in ap:
            if not isinstance(p, str) or not p.strip():
                errors.append("avoid_phrases entries must be non-empty strings")
            elif len(p) > MAX_AVOID_LEN:
                errors.append(f"avoid_phrase too long (max {MAX_AVOID_LEN})")
    return errors


# ── CRUD ─────────────────────────────────────────────────────────────────────

def save_template(contact, action_type, settings):
    """
    Upsert one template at its scope (contact/action_type, either may be None).
    Replaces an existing row at the same scope and bumps its version.
    Returns {success, id, version} or {success: False, errors: [...]}.
    """
    errs = _validate_settings(settings)
    if errs:
        return {"success": False, "error_code": "INVALID_TEMPLATE", "errors": errs}

    contact = canonical_contact(contact)
    action_type = action_type or None
    now = int(time.time())
    ap_json = json.dumps([p.strip() for p in (settings.get("avoid_phrases") or [])])

    db = _db()
    try:
        # Find existing row at this exact scope (NULL-safe).
        existing = db.execute(
            "SELECT id, version FROM email_templates "
            "WHERE contact IS ? AND action_type IS ?", (contact, action_type)
        ).fetchone()

        if existing:
            new_version = existing["version"] + 1
            db.execute(
                "UPDATE email_templates SET tone=?, formality=?, length_class=?, "
                "greeting_style=?, signoff_style=?, max_words=?, max_sentences=?, "
                "max_paragraphs=?, avoid_phrases=?, enabled=1, version=?, updated_at=? "
                "WHERE id=?",
                (settings["tone"], settings["formality"], settings["length_class"],
                 settings["greeting_style"], settings["signoff_style"],
                 settings["max_words"], settings["max_sentences"], settings["max_paragraphs"],
                 ap_json, new_version, now, existing["id"]),
            )
            db.commit()
            return {"success": True, "id": existing["id"], "version": new_version}

        tid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO email_templates (id, contact, action_type, tone, formality, "
            "length_class, greeting_style, signoff_style, max_words, max_sentences, "
            "max_paragraphs, avoid_phrases, enabled, version, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,1,?,?)",
            (tid, contact, action_type, settings["tone"], settings["formality"],
             settings["length_class"], settings["greeting_style"], settings["signoff_style"],
             settings["max_words"], settings["max_sentences"], settings["max_paragraphs"],
             ap_json, now, now),
        )
        db.commit()
        return {"success": True, "id": tid, "version": 1}
    except sqlite3.IntegrityError as e:
        return {"success": False, "error_code": "SCOPE_CONFLICT", "detail": str(e)}
    finally:
        db.close()


def list_templates():
    db = _db()
    rows = db.execute("SELECT * FROM email_templates ORDER BY created_at DESC").fetchall()
    db.close()
    return [_row_to_dict(r) for r in rows]


def delete_template(template_id):
    db = _db()
    n = db.execute("DELETE FROM email_templates WHERE id=?", (template_id,)).rowcount
    db.commit(); db.close()
    return {"success": n > 0, "deleted": n}


def _row_to_dict(r):
    d = dict(r)
    try:
        d["avoid_phrases"] = json.loads(d.get("avoid_phrases") or "[]")
    except Exception:
        d["avoid_phrases"] = []
    return d


# ── Resolution ───────────────────────────────────────────────────────────────

def resolve(contact, action_type):
    """
    Deterministic precedence: exact -> contact-wide -> action-wide -> global -> default.
    Picks exactly ONE enabled row. >1 candidate at the winning rank -> MANUAL_REVIEW.
    Returns {status, scope, settings, template_id, version}.
      status: 'OK' | 'DEFAULT' | 'MANUAL_REVIEW'
    """
    contact = canonical_contact(contact)
    action_type = action_type or None
    db = _db()
    try:
        ranks = [
            ("exact",   "contact IS ? AND action_type IS ?", (contact, action_type)),
            ("contact", "contact IS ? AND action_type IS NULL", (contact,)),
            ("action",  "contact IS NULL AND action_type IS ?", (action_type,)),
            ("global",  "contact IS NULL AND action_type IS NULL", ()),
        ]
        for scope, where, params in ranks:
            # 'exact'/'contact' need a real contact; 'exact'/'action' need a real action.
            if scope in ("exact", "contact") and contact is None:
                continue
            if scope in ("exact", "action") and action_type is None:
                continue
            rows = db.execute(
                f"SELECT * FROM email_templates WHERE enabled=1 AND {where}", params
            ).fetchall()
            if len(rows) > 1:
                return {"status": "MANUAL_REVIEW", "scope": scope,
                        "reason": f"{len(rows)} templates at scope '{scope}' — ambiguous"}
            if len(rows) == 1:
                r = _row_to_dict(rows[0])
                return {"status": "OK", "scope": scope, "template_id": r["id"],
                        "version": r["version"], "settings": _settings_of(r)}
        return {"status": "DEFAULT", "scope": "default", "template_id": None,
                "version": None, "settings": dict(CONSERVATIVE_DEFAULT)}
    finally:
        db.close()


def _settings_of(r):
    return {k: r[k] for k in (
        "tone", "formality", "length_class", "greeting_style", "signoff_style",
        "max_words", "max_sentences", "max_paragraphs", "avoid_phrases")}


def snapshot_for_proposal(contact, action_type):
    """
    Resolve + return the exact settings to PIN on a proposal. Once pinned, the
    proposal executes against this snapshot regardless of later template edits.
    """
    res = resolve(contact, action_type)
    return {
        "status": res["status"], "scope": res["scope"],
        "template_id": res.get("template_id"), "version": res.get("version"),
        "settings": res.get("settings"),
        "pinned_at": int(time.time()),
    }


# ── Render (code-owned style block for GPT-4o; NO avoid_phrases) ──────────────

def render_style_block(settings):
    """
    Produce the body-only style constraints for the model. Renders fixed strings
    from allowlisted values. avoid_phrases is deliberately EXCLUDED — it is a
    validator rule, never a model instruction.
    """
    greet = {"none": "no greeting", "first_name": "greet by first name",
             "formal_name": "greet by formal name"}[settings["greeting_style"]]
    sign = {"none": "no sign-off", "thanks": "sign off with “Thanks,”",
            "regards": "sign off with “Regards,”",
            "best": "sign off with “Best,”"}[settings["signoff_style"]]
    return (
        "WRITING STYLE CONSTRAINTS — BODY ONLY (wording and format only)\n"
        f"Tone: {settings['tone']}, {settings['formality']}.\n"
        f"Length: {settings['length_class']}; maximum {settings['max_words']} words, "
        f"{settings['max_sentences']} sentences, {settings['max_paragraphs']} paragraphs.\n"
        f"Greeting: {greet}. Closing: {sign}.\n"
        "Do not add recipients, change the subject, alter the requested action, or "
        "introduce commitments not present in the requested intent. Return only the email body."
    )


# ── Validate (structural conformance only) ────────────────────────────────────

def _count_words(text):     return len(re.findall(r"\b\w+\b", text))
def _count_sentences(text): return len([s for s in re.split(r"[.!?]+", text) if s.strip()])
def _count_paragraphs(text):return len([p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()])


def validate_body(body, settings):
    """
    Proves CONFORMANCE only: length limits, banned phrases, no structural headers.
    Does NOT prove intent preservation or universal metadata non-leakage.
    Returns {valid, failures: [...]}.
    """
    failures = []
    body = body or ""
    if _count_words(body) > settings["max_words"]:
        failures.append("exceeds max_words")
    if _count_sentences(body) > settings["max_sentences"]:
        failures.append("exceeds max_sentences")
    if _count_paragraphs(body) > settings["max_paragraphs"]:
        failures.append("exceeds max_paragraphs")
    low = body.lower()
    for p in (settings.get("avoid_phrases") or []):
        if p.lower() in low:
            failures.append(f"contains avoided phrase: {p}")
    if _HEADER_RE.search(body) or _MIME_RE.search(body):
        failures.append("contains structural header / metadata")
    return {"valid": len(failures) == 0, "failures": failures}
