# -*- coding: utf-8 -*-
"""
ARGUS Phase 8 Tests — Part 4/7: Private-contact protection (CONTROL 3).

Exercises argus.private_contacts directly against a THROWAWAY temp DB:
exact-normalized matching, source + recipient target checks, atomic audited
mutations, and the redaction guarantee (raw address NEVER reaches audit).

Three angles: Normal · Hacker (adversarial) · Strict Teacher.
Run standalone: python tests/test_phase_8_private_contacts.py
"""
import os, sys, json, sqlite3, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import argus.db as dbmod
import argus.private_contacts as pc
import argus.audit as audit

passed = 0
failed = 0

def sec(name): print(f'\n  [{name}]')
def check(name, cond, got=None):
    global passed, failed
    if cond:
        print(f'    [PASS] {name}'); passed += 1
    else:
        d = f' | got: {got}' if got is not None else ''
        print(f'    [FAIL] {name}{d}'); failed += 1

_paths = []

def fresh_db():
    fd, path = tempfile.mkstemp(suffix='.db'); os.close(fd); os.remove(path)
    dbmod.DATABASE = path
    dbmod.init_db()
    pc.DATABASE = path
    audit.DATABASE = path
    _paths.append(path)
    return path

def cleanup():
    for p in _paths:
        for ext in ('', '-wal', '-shm'):
            try: os.remove(p + ext)
            except OSError: pass

def raw(path):
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row; return c

def row_count(path):
    c = raw(path)
    n = c.execute("SELECT COUNT(*) FROM private_contacts").fetchone()[0]
    c.close(); return n

def latest_payload(path, event_type):
    """Most recent audit payload (parsed) for an event type, or None."""
    c = raw(path)
    r = c.execute("SELECT payload_json FROM audit_events WHERE event_type=? "
                  "ORDER BY rowid DESC LIMIT 1", (event_type,)).fetchone()
    c.close()
    return json.loads(r["payload_json"]) if r and r["payload_json"] else None

def audit_count(path, event_type):
    c = raw(path)
    n = c.execute("SELECT COUNT(*) FROM audit_events WHERE event_type=?",
                  (event_type,)).fetchone()[0]
    c.close(); return n


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 1 — NORMAL
# ─────────────────────────────────────────────────────────────────────────────
def test_normal():
    sec('NORMAL — add a contact, then it matches')
    path = fresh_db()
    r = pc.add_contact("boss@company.com", display_label="The Boss")
    check('add_contact succeeds', r.get("success") is True)
    check('is_private matches exact address', pc.is_private("boss@company.com") is True)
    check('non-contact does not match', pc.is_private("someone@else.com") is False)

    sec('NORMAL — check_targets flags an outgoing recipient')
    hit = pc.check_targets("email.send.external", {"recipient": "boss@company.com"})
    check('recipient hit detected', hit is not None and hit.get("field") == "recipient")
    check('hit carries a redacted ref', hit.get("redacted") == "b***@c***.com")

    sec('NORMAL — check_targets flags the SELECTED-EMAIL source sender')
    hit = pc.check_targets(None, {}, source_sender="The Boss <boss@company.com>")
    check('source hit detected', hit is not None and hit.get("field") == "source")

    sec('NORMAL — no protected target returns None')
    check('clean action -> None',
          pc.check_targets("email.send.external", {"recipient": "ok@elsewhere.com"}) is None)

    sec('NORMAL — list shows the contact; remove disables it')
    check('list_contacts shows one enabled', len(pc.list_contacts()) == 1)
    rr = pc.remove_contact("boss@company.com")
    check('remove_contact succeeds', rr.get("success") is True and rr.get("changed") is True)
    check('removed contact no longer matches', pc.is_private("boss@company.com") is False)
    check('default list now empty', len(pc.list_contacts()) == 0)


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 2 — HACKER (adversarial)
# ─────────────────────────────────────────────────────────────────────────────
def test_hacker():
    sec('[HACKER] display-name + mixed case cannot smuggle past the match')
    path = fresh_db()
    pc.add_contact("boss@company.com")
    check('"Boss <BOSS@Company.COM>" still matches', pc.is_private("Boss <BOSS@Company.COM>") is True)
    check('UPPER-CASE address still matches', pc.is_private("BOSS@COMPANY.COM") is True)

    sec('[HACKER] +tag is NOT a match (exact-address policy, no tag stripping)')
    check('boss+urgent@company.com does NOT match', pc.is_private("boss+urgent@company.com") is False)

    sec('[HACKER] the raw address NEVER reaches the audit payload')
    pay = latest_payload(path, "PRIVATE_CONTACT_ADDED")
    check('add audit stored a redacted contact', pay and pay.get("contact") == "b***@c***.com")
    check('add audit does NOT contain the raw local-part',
          pay and "boss@company.com" not in json.dumps(pay))

    sec('[HACKER] disabled contact does not match')
    pc.remove_contact("boss@company.com")
    check('disabled contact -> not private', pc.is_private("boss@company.com") is False)
    rm_pay = latest_payload(path, "PRIVATE_CONTACT_REMOVED")
    check('remove audit is also redacted', rm_pay and rm_pay.get("contact") == "b***@c***.com")

    sec('[HACKER] garbage / non-addresses are rejected, never stored')
    check('no "@" -> INVALID_EMAIL', pc.add_contact("notanemail").get("error_code") == "INVALID_EMAIL")
    check('empty -> INVALID_EMAIL', pc.add_contact("").get("error_code") == "INVALID_EMAIL")
    check('empty string is never private', pc.is_private("") is False)

    sec('[HACKER] removing an absent contact is an idempotent no-op (no audit noise)')
    before = audit_count(path, "PRIVATE_CONTACT_REMOVED")
    rr = pc.remove_contact("ghost@nowhere.com")
    check('absent remove -> success, changed=False',
          rr.get("success") is True and rr.get("changed") is False)
    check('no extra REMOVED audit written', audit_count(path, "PRIVATE_CONTACT_REMOVED") == before)


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 3 — STRICT TEACHER (boundaries)
# ─────────────────────────────────────────────────────────────────────────────
def test_strict():
    sec('[STRICT] re-adding re-enables in place — never a duplicate row')
    path = fresh_db()
    pc.add_contact("boss@company.com")
    pc.remove_contact("boss@company.com")
    r = pc.add_contact("boss@company.com")
    check('re-add reports reactivated', r.get("reactivated") is True)
    check('exactly one row for the address (UPSERT, no dup)', row_count(path) == 1)
    check('re-added contact matches again', pc.is_private("boss@company.com") is True)

    sec('[STRICT] reason cap is enforced, never silently truncated')
    long_reason = "x" * (pc.REASON_MAX + 1)
    r = pc.add_contact("c@d.com", reason=long_reason)
    check('over-long reason -> REJECTION_REASON_TOO_LONG',
          r.get("error_code") == "REJECTION_REASON_TOO_LONG")
    check('rejected add did NOT store a row', pc.is_private("c@d.com") is False)

    sec('[STRICT] source is checked before recipient (first hit wins)')
    pc.add_contact("vip@secret.org")
    hit = pc.check_targets("email.send.external",
                           {"recipient": "boss@company.com"},
                           source_sender="vip@secret.org")
    check('source precedence over recipient', hit and hit.get("field") == "source")

    sec('[STRICT] only the recipient entity field is treated as a destination')
    # A private address sitting in some OTHER field must not be read as a target.
    hit = pc.check_targets("email.send.external",
                           {"subject": "boss@company.com", "recipient": "ok@elsewhere.com"})
    check('private addr in non-recipient field is ignored', hit is None)

    sec('[STRICT] redaction handles odd shapes without leaking or crashing')
    check('no "@" -> "***"', pc._redact("notanemail") == "***")
    check('empty -> "***"', pc._redact("") == "***")
    check('no-TLD domain still redacts', pc._redact("a@localhost") == "a***@l***")

    sec('[STRICT] mutations write their audit atomically (one event each)')
    p2 = fresh_db()
    pc.add_contact("solo@x.com")
    pc.remove_contact("solo@x.com")
    check('exactly one ADDED audit', audit_count(p2, "PRIVATE_CONTACT_ADDED") == 1)
    check('exactly one REMOVED audit', audit_count(p2, "PRIVATE_CONTACT_REMOVED") == 1)


if __name__ == '__main__':
    try:
        test_normal()
        test_hacker()
        test_strict()
    finally:
        cleanup()
        print()
        print('-' * 62)
        status = 'ALL CLEAR' if failed == 0 else 'FAILURES DETECTED'
        print(f'  RESULT: {passed} passed | {failed} failed | {status}')
        print('=' * 62)
        print()
        sys.exit(0 if failed == 0 else 1)
