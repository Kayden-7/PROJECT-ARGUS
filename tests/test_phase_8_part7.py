# -*- coding: utf-8 -*-
"""
ARGUS Phase 8 Tests — Part 7/7: reason cap (C6) + demo-reset reseed + genesis.

Covers the final part:
  - caller-supplied reasons are REJECTED (never truncated) when too long / malformed
  - system-generated reasons are degraded EXPLICITLY (visible marker), never silently
    truncated, and NEVER fail the status_reason CHECK (quarantine must always route)
  - reset_demo() refuses while a send is in flight, reseeds ONE private-contact
    fixture, and anchors the wiped audit chain with a DEMO_RESET_COMPLETED genesis

Throwaway temp DB. Three angles: Normal · Hacker · Strict Teacher.
Run standalone: python tests/test_phase_8_part7.py
"""
import os, sys, json, time, uuid, sqlite3, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import argus.db as dbmod
import argus.queue as q
import argus.audit as audit
import argus.demo as demo
import argus.private_contacts as pc

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
    for m in (q, audit, demo, pc):
        m.DATABASE = path
    _paths.append(path)
    return path

def cleanup():
    for p in _paths:
        for ext in ('', '-wal', '-shm'):
            try: os.remove(p + ext)
            except OSError: pass

def raw(path):
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row; return c

def mk_pending():
    r = q.enqueue({"action_type": "email.archive", "entities": {"email_id": "m1"}, "intent": "x"},
                  {"action_expiry": 300})
    return r["id"]

def qcol(path, qid, col):
    c = raw(path); r = c.execute(f"SELECT {col} FROM approval_queue WHERE id=?", (qid,)).fetchone()
    c.close(); return r[col] if r else None

def count(path, tbl):
    c = raw(path); n = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]; c.close(); return n


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 1 — NORMAL
# ─────────────────────────────────────────────────────────────────────────────
def test_normal():
    sec('NORMAL — a normal reason validates and rejects cleanly')
    path = fresh_db()
    qid = mk_pending()
    r = q.reject(qid, "Not appropriate to send right now.")
    check('reject succeeds', r.get("status") == "REJECTED")
    check('reason stored verbatim', qcol(path, qid, 'status_reason') == "Not appropriate to send right now.")

    sec('NORMAL — short reasons pass through both validators unchanged')
    check('caller validator passes short', q._validate_caller_reason("ok") == ("ok", None))
    check('system bounder leaves short unchanged', q._bound_system_reason("short") == "short")

    sec('NORMAL — reset seeds exactly one protected contact the demo can show')
    demo.reset_demo()
    check('one private_contacts fixture', count(path, 'private_contacts') == 1)
    check('seeded contact is private', pc.is_private("legal@confidential-client.com") is True)

    sec('NORMAL — reset anchors a fresh audit chain with a genesis event')
    c = raw(path); rows = c.execute("SELECT event_type FROM audit_events").fetchall(); c.close()
    check('exactly one audit row after reset', len(rows) == 1, got=len(rows))
    check('genesis is DEMO_RESET_COMPLETED', rows and rows[0]["event_type"] == "DEMO_RESET_COMPLETED")


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 2 — HACKER (adversarial)
# ─────────────────────────────────────────────────────────────────────────────
def test_hacker():
    sec('[HACKER] an over-long CALLER reason is rejected, not truncated')
    path = fresh_db()
    qid = mk_pending()
    r = q.reject(qid, "x" * 501)
    check('501-char reason -> REJECTION_REASON_TOO_LONG', r.get("error_code") == "REJECTION_REASON_TOO_LONG")
    check('item NOT rejected (stays PENDING)', qcol(path, qid, 'status') == 'PENDING')
    check('no reason written', qcol(path, qid, 'status_reason') is None)

    sec('[HACKER] a NUL-laced reason is rejected (len vs SQLite length divergence)')
    r = q.reject(qid, "ok\x00hidden")
    check('NUL reason -> INVALID_REASON', r.get("error_code") == "INVALID_REASON")
    check('item still PENDING', qcol(path, qid, 'status') == 'PENDING')

    sec('[HACKER] an over-long SYSTEM reason is degraded explicitly, never dropped')
    big = "Gmail blew up: " + "E" * 5000
    bounded = q._bound_system_reason(big)
    check('bounded length within the 500 cap', len(bounded) <= q.REASON_MAX)
    check('degradation is VISIBLE (marker present)', "chars]" in bounded)
    check('keeps the meaningful prefix', bounded.startswith("Gmail blew up:"))

    sec('[HACKER] a system hold ALWAYS routes — to_manual_review never fails on length')
    qid2 = mk_pending()
    r = q.to_manual_review(qid2, "y" * 4000)
    check('to_manual_review succeeds despite a 4000-char reason', r.get("status") == "MANUAL_REVIEW")
    check('stored reason fits the CHECK', len(qcol(path, qid2, 'status_reason')) <= q.REASON_MAX)

    sec('[HACKER] reset refuses while a send is in flight, and wipes nothing')
    fresh_db_path = fresh_db()
    # Seed a fixture contact + a SENDING execution; reset must bail out and touch neither.
    pc.add_contact("pre-existing@x.com")
    c = raw(fresh_db_path); now = int(time.time())
    c.execute("INSERT INTO pending_executions (execution_id, approval_id, action_type, "
              "payload_json, status, approved_at, execute_after, created_at, updated_at) "
              "VALUES (?,?,?,?,'SENDING',?,?,?,?)",
              (str(uuid.uuid4()), "a1", "email.send.external", "{}", now, now, now, now))
    c.commit(); c.close()
    r = demo.reset_demo()
    check('reset -> EXECUTION_IN_FLIGHT', r.get("error_code") == "EXECUTION_IN_FLIGHT")
    check('SENDING row NOT wiped', count(fresh_db_path, 'pending_executions') == 1)
    check('contacts NOT reseeded mid-flight', pc.is_private("pre-existing@x.com") is True)


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 3 — STRICT TEACHER (boundaries)
# ─────────────────────────────────────────────────────────────────────────────
def test_strict():
    sec('[STRICT] caller-reason boundary: exactly 500 passes, 501 fails')
    path = fresh_db()
    check('len 500 -> accepted', q._validate_caller_reason("a" * 500) == ("a" * 500, None))
    check('len 501 -> REJECTION_REASON_TOO_LONG',
          q._validate_caller_reason("a" * 501)[1] == "REJECTION_REASON_TOO_LONG")

    sec('[STRICT] system-bounder boundary: 500 unchanged, 501 degraded within cap')
    check('len 500 unchanged', q._bound_system_reason("b" * 500) == "b" * 500)
    over = q._bound_system_reason("b" * 501)
    check('len 501 -> degraded, still <= cap', len(over) <= q.REASON_MAX)
    check('degraded shows a positive dropped count', "[+" in over and "chars]" in over)

    sec('[STRICT] a rejected over-long reason mutates NOTHING (no audit, no transition)')
    qid = mk_pending()
    a_before = count(path, 'audit_events')
    q.reject(qid, "z" * 600)
    check('no QUEUE_TRANSITIONED audit written', count(path, 'audit_events') == a_before)
    check('queue row untouched (PENDING)', qcol(path, qid, 'status') == 'PENDING')

    sec('[STRICT] reset is idempotent: one fixture + one genesis every time')
    demo.reset_demo(); demo.reset_demo()
    check('still exactly one private contact', count(path, 'private_contacts') == 1)
    c = raw(path); rows = c.execute("SELECT event_type, payload_json FROM audit_events").fetchall(); c.close()
    check('still exactly one genesis audit row', len(rows) == 1, got=len(rows))

    sec('[STRICT] the genesis event carries NO user content')
    pay = json.loads(rows[0]["payload_json"]) if rows and rows[0]["payload_json"] else {}
    check('genesis payload has no recipient/body/subject fields',
          not any(k in pay for k in ("recipient", "body", "subject", "email")))


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
