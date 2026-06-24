# -*- coding: utf-8 -*-
"""
ARGUS Phase 8 Tests — Part 5/7: Queue lifecycle controls.

Covers Control 4 (MANUAL_REVIEW timeout, lazy) and Control 7 (invalid-transition
rate limiting + auto-lock). R-REOPEN is deferred to Part 6 (it needs the atomic
approval / generation machinery that lands there).

Runs against a THROWAWAY temp DB. Three angles: Normal · Hacker · Strict Teacher.
Run standalone: python tests/test_phase_8_queue_lifecycle.py
"""
import os, sys, json, time, uuid, sqlite3, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import argus.db as dbmod
import argus.queue as q
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
    q.DATABASE = path
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

def mkqueue(path, status='PENDING', started_at=None, mr_gen=0):
    """Insert one approval_queue row directly and return its id."""
    iid = str(uuid.uuid4()); now = int(time.time())
    c = raw(path)
    c.execute(
        "INSERT INTO approval_queue (id, proposal_json, decision_json, status, "
        "created_at, expires_at, updated_at, manual_review_started_at, "
        "manual_review_generation) VALUES (?,?,?,?,?,?,?,?,?)",
        (iid, json.dumps({"action_type": "email.archive", "entities": {}}),
         json.dumps({}), status, now, now + 3600, now, started_at, mr_gen))
    c.commit(); c.close()
    return iid

def set_cols(path, item_id, **cols):
    c = raw(path)
    sets = ", ".join(f"{k}=?" for k in cols)
    c.execute(f"UPDATE approval_queue SET {sets} WHERE id=?", (*cols.values(), item_id))
    c.commit(); c.close()

def col(path, item_id, name):
    c = raw(path)
    r = c.execute(f"SELECT {name} FROM approval_queue WHERE id=?", (item_id,)).fetchone()
    c.close(); return r[name] if r else None

def seed_attempts(path, item_id, count, valid=0, age=0):
    """Pre-load queue_transition_attempts rows aged `age` seconds in the past."""
    c = raw(path); now = int(time.time())
    for _ in range(count):
        c.execute("INSERT INTO queue_transition_attempts "
                  "(queue_id, attempted_from, attempted_to, valid, created_at) "
                  "VALUES (?,?,?,?,?)", (item_id, "X", "Y", valid, now - age))
    c.commit(); c.close()

def audit_count(path, event_type):
    c = raw(path)
    n = c.execute("SELECT COUNT(*) FROM audit_events WHERE event_type=?",
                  (event_type,)).fetchone()[0]
    c.close(); return n


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 1 — NORMAL
# ─────────────────────────────────────────────────────────────────────────────
def test_normal():
    sec('NORMAL — MANUAL_REVIEW entry stamps a clock')
    path = fresh_db()
    iid = mkqueue(path)
    r = q.to_manual_review(iid, "needs a human")
    check('to_manual_review succeeds', r.get("success") is True)
    check('status is MANUAL_REVIEW', col(path, iid, 'status') == 'MANUAL_REVIEW')
    check('manual_review_generation bumped to 1', col(path, iid, 'manual_review_generation') == 1)
    check('manual_review_started_at set', col(path, iid, 'manual_review_started_at') is not None)

    sec('NORMAL — fresh PENDING still approves (regression)')
    iid2 = mkqueue(path)
    r = q.approve(iid2)
    check('approve PENDING -> APPROVED', r.get("status") == "APPROVED")
    check('version bumped 0 -> 1 on approve', col(path, iid2, 'version') == 1)

    sec('NORMAL — MANUAL_REVIEW within window is NOT timed out on read')
    iid3 = mkqueue(path, status='MANUAL_REVIEW',
                   started_at=int(time.time()) - 100, mr_gen=1)
    q.fetch_pending()
    check('still MANUAL_REVIEW within window', col(path, iid3, 'status') == 'MANUAL_REVIEW')

    sec('NORMAL — MANUAL_REVIEW within window can be approved')
    r = q.approve(iid3)
    check('in-window MANUAL_REVIEW -> APPROVED', r.get("status") == "APPROVED")

    sec('NORMAL — MANUAL_REVIEW past window times out lazily on fetch')
    iid4 = mkqueue(path, status='MANUAL_REVIEW',
                   started_at=int(time.time()) - 700, mr_gen=1)
    q.fetch_pending()
    check('past-window item -> MANUAL_REVIEW_TIMEOUT',
          col(path, iid4, 'status') == 'MANUAL_REVIEW_TIMEOUT')
    check('one MANUAL_REVIEW_TIMEOUT audit event', audit_count(path, 'MANUAL_REVIEW_TIMEOUT') == 1)


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 2 — HACKER (adversarial)
# ─────────────────────────────────────────────────────────────────────────────
def test_hacker():
    sec('[HACKER] timed-out item can NEVER be approved directly')
    path = fresh_db()
    iid = mkqueue(path, status='MANUAL_REVIEW_TIMEOUT', mr_gen=1)
    r = q.approve(iid)
    check('approve MANUAL_REVIEW_TIMEOUT refused', r.get("success") is False)
    check('still MANUAL_REVIEW_TIMEOUT (not approved)',
          col(path, iid, 'status') == 'MANUAL_REVIEW_TIMEOUT')

    sec('[HACKER] hammering invalid transitions auto-locks a lockable item')
    iid2 = mkqueue(path, status='MANUAL_REVIEW_TIMEOUT', mr_gen=1)
    results = [q.approve(iid2) for _ in range(5)]
    check('attempts 1-4 -> INVALID_STATE_TRANSITION',
          all(x.get("error_code") == "INVALID_STATE_TRANSITION" for x in results[:4]))
    check('attempt 5 -> INVALID_TRANSITION_RATE_LIMITED',
          results[4].get("error_code") == "INVALID_TRANSITION_RATE_LIMITED")
    check('item is now TRANSITION_LOCKED', col(path, iid2, 'status') == 'TRANSITION_LOCKED')
    check('lock reason recorded', bool(col(path, iid2, 'transition_lock_reason')))
    check('one QUEUE_TRANSITION_LOCKED audit', audit_count(path, 'QUEUE_TRANSITION_LOCKED') == 1)

    sec('[HACKER] terminal items are NEVER lockable (no resurrection)')
    iid3 = mkqueue(path, status='REJECTED')
    res = [q.approve(iid3) for _ in range(8)]
    check('every attempt stays INVALID_STATE_TRANSITION (never RATE_LIMITED)',
          all(x.get("error_code") == "INVALID_STATE_TRANSITION" for x in res))
    check('REJECTED item never becomes TRANSITION_LOCKED',
          col(path, iid3, 'status') == 'REJECTED')

    sec('[HACKER] timeout escalation is idempotent across repeated reads')
    iid4 = mkqueue(path, status='MANUAL_REVIEW',
                   started_at=int(time.time()) - 700, mr_gen=2)
    q.fetch_pending(); q.fetch_pending(); q.approve(iid4)
    check('still exactly ONE timeout audit despite 3 reads',
          audit_count(path, 'MANUAL_REVIEW_TIMEOUT') == 1)


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 3 — STRICT TEACHER (boundaries)
# ─────────────────────────────────────────────────────────────────────────────
def test_strict():
    sec('[STRICT] only invalid attempts INSIDE the 60s window count')
    path = fresh_db()
    iid = mkqueue(path, status='MANUAL_REVIEW_TIMEOUT', mr_gen=1)
    seed_attempts(path, iid, count=4, valid=0, age=120)  # stale, must not count
    r = q.approve(iid)  # 1 fresh invalid -> only 1 in window
    check('stale attempts ignored -> not rate-limited',
          r.get("error_code") == "INVALID_STATE_TRANSITION")
    check('item not locked by stale attempts',
          col(path, iid, 'status') == 'MANUAL_REVIEW_TIMEOUT')

    sec('[STRICT] lock boundary is exactly 5 (4 holds, 5th trips)')
    iid2 = mkqueue(path, status='MANUAL_REVIEW_TIMEOUT', mr_gen=1)
    four = [q.approve(iid2) for _ in range(4)]
    check('4 invalid attempts do NOT lock', col(path, iid2, 'status') == 'MANUAL_REVIEW_TIMEOUT')
    check('4th still INVALID_STATE_TRANSITION',
          four[3].get("error_code") == "INVALID_STATE_TRANSITION")
    fifth = q.approve(iid2)
    check('5th attempt trips the lock',
          fifth.get("error_code") == "INVALID_TRANSITION_RATE_LIMITED")

    sec('[STRICT] timeout boundary is strict (==window holds, +1 escalates)')
    at_edge = mkqueue(path, status='MANUAL_REVIEW',
                      started_at=int(time.time()) - q.MANUAL_REVIEW_TIMEOUT_SECONDS, mr_gen=1)
    q.fetch_pending()
    check('exactly at window -> still MANUAL_REVIEW', col(path, at_edge, 'status') == 'MANUAL_REVIEW')
    over = mkqueue(path, status='MANUAL_REVIEW',
                   started_at=int(time.time()) - q.MANUAL_REVIEW_TIMEOUT_SECONDS - 1, mr_gen=1)
    q.fetch_pending()
    check('one second over -> MANUAL_REVIEW_TIMEOUT', col(path, over, 'status') == 'MANUAL_REVIEW_TIMEOUT')

    sec('[STRICT] valid transitions record an attempt but never lock')
    iid3 = mkqueue(path)
    q.approve(iid3)
    c = raw(path)
    n_valid = c.execute("SELECT COUNT(*) FROM queue_transition_attempts "
                        "WHERE queue_id=? AND valid=1", (iid3,)).fetchone()[0]
    c.close()
    check('valid approve logged a valid=1 attempt', n_valid == 1)
    check('approved item is APPROVED, not locked', col(path, iid3, 'status') == 'APPROVED')

    sec('[STRICT] auto-lock NEVER fires from a non-lockable APPROVED item')
    iid4 = mkqueue(path, status='APPROVED')
    res = [q.cancel(iid4) for _ in range(6)]  # cancel invalid once undo window logic aside
    # APPROVED is not in LOCKABLE_STATES, so even repeated invalid attempts can't lock it.
    check('APPROVED never becomes TRANSITION_LOCKED',
          col(path, iid4, 'status') != 'TRANSITION_LOCKED')


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
