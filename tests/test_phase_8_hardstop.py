# -*- coding: utf-8 -*-
"""
ARGUS Phase 8 Tests — Part 2/7: Emergency stop + hard-stop epoch
Run standalone: python tests/test_phase_8_hardstop.py

Covers kernel.py (snapshot / is_hard_stop / hard_stop_status / is_execution_stale
/ set_hard_stop) and app.py _control_authorized. All against a THROWAWAY temp DB.
Three angles: Normal · Hacker (adversarial) · Strict Teacher (exact mechanics).
"""
import os, sys, sqlite3, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import argus.db as dbmod
import argus.kernel as kernel

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
    """Point db + kernel + audit at a fresh temp DB and initialise the schema."""
    fd, path = tempfile.mkstemp(suffix='.db'); os.close(fd); os.remove(path)
    dbmod.DATABASE = path
    dbmod.init_db()
    kernel.DATABASE = path
    import argus.audit as audit
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

def hs_event_count(path):
    c = raw(path)
    n = c.execute("SELECT COUNT(*) FROM audit_events WHERE event_type LIKE 'SYSTEM_HARD_STOP%'").fetchone()[0]
    c.close(); return n


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 1 — NORMAL
# ─────────────────────────────────────────────────────────────────────────────
def test_normal():
    sec('NORMAL — toggle, epoch, status, staleness')
    fresh_db()
    check('default not engaged', kernel.is_hard_stop() is False)
    st = kernel.hard_stop_status()
    check('default status engaged False, epoch 0', st["engaged"] is False and st["epoch"] == 0, got=st)

    r = kernel.set_hard_stop(True, updated_by="op", reason="halt now")
    check('enable success + transitioned', r["success"] and r["transitioned"] is True, got=r)
    check('enable bumped epoch 0->1', r["epoch"] == 1, got=r["epoch"])
    check('is_hard_stop True after enable', kernel.is_hard_stop() is True)
    st = kernel.hard_stop_status()
    check('status carries reason + updated_by', st["reason"] == "halt now" and st["updated_by"] == "op", got=st)

    r = kernel.set_hard_stop(False)
    check('disable success', r["success"] and r["transitioned"] is True, got=r)
    check('disable did NOT bump epoch (stays 1)', r["epoch"] == 1, got=r["epoch"])
    check('is_hard_stop False after disable', kernel.is_hard_stop() is False)

    # epoch now 1, hard stop off
    check('is_execution_stale(1) False (matches, off)', kernel.is_execution_stale(1) is False)
    check('is_execution_stale(0) True (epoch mismatch)', kernel.is_execution_stale(0) is True)


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 2 — HACKER
# ─────────────────────────────────────────────────────────────────────────────
def test_hacker():
    sec('HACKER — bad inputs rejected, fail-closed reads')
    path = fresh_db()

    check('reject non-bool engaged', kernel.set_hard_stop("yes").get("error_code") == "INVALID_ENGAGED")
    check('reject non-str reason', kernel.set_hard_stop(True, reason=123).get("error_code") == "INVALID_REASON")
    r = kernel.set_hard_stop(True, reason="x" * 501)
    check('reject >500 reason (not truncate)', r.get("error_code") == "REJECTION_REASON_TOO_LONG", got=r)
    check('rejected over-long reason did NOT engage', kernel.is_hard_stop() is False)

    # bool is an int subclass — must be rejected as an epoch
    check('is_execution_stale(True) -> True', kernel.is_execution_stale(True) is True)
    check('is_execution_stale(False) -> True', kernel.is_execution_stale(False) is True)
    check('is_execution_stale("5") -> True', kernel.is_execution_stale("5") is True)
    check('is_execution_stale(-1) -> True', kernel.is_execution_stale(-1) is True)
    check('is_execution_stale(None) -> True', kernel.is_execution_stale(None) is True)

    # malformed persisted flag -> fail closed
    c = raw(path); c.execute("UPDATE system_state SET value='maybe' WHERE key='SYSTEM_HARD_STOP'"); c.commit(); c.close()
    check('malformed flag -> is_hard_stop True (fail closed)', kernel.is_hard_stop() is True)
    check('malformed flag -> status degraded', kernel.hard_stop_status().get("degraded") is True)

    # malformed epoch -> fail closed everywhere
    c = raw(path); c.execute("UPDATE system_state SET value='1' WHERE key='SYSTEM_HARD_STOP'")
    c.execute("UPDATE system_state SET value='abc' WHERE key='HARD_STOP_EPOCH'"); c.commit(); c.close()
    check('malformed epoch -> is_execution_stale True', kernel.is_execution_stale(0) is True)
    check('malformed epoch -> status degraded', kernel.hard_stop_status().get("degraded") is True)


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 3 — STRICT TEACHER
# ─────────────────────────────────────────────────────────────────────────────
def test_strict_teacher():
    sec('STRICT — exact epoch + audit + idempotency mechanics')
    path = fresh_db()

    kernel.set_hard_stop(True)                       # 0 -> 1
    r = kernel.set_hard_stop(True)                   # idempotent
    check('re-enable is no-op (transitioned False)', r["transitioned"] is False, got=r)
    check('re-enable does NOT bump epoch (stays 1)', r["epoch"] == 1, got=r["epoch"])

    # idempotent no-op must write NO audit event
    before = hs_event_count(path)
    kernel.set_hard_stop(True)                        # another no-op
    check('no-op writes NO audit event', hs_event_count(path) == before, got=hs_event_count(path))

    kernel.set_hard_stop(False)                       # real transition
    check('disable wrote one more audit event', hs_event_count(path) == before + 1, got=hs_event_count(path))

    # enable -> disable -> enable: epoch 1 -> 1 -> 2
    r = kernel.set_hard_stop(True)
    check('second enable bumps epoch 1->2', r["epoch"] == 2, got=r["epoch"])

    # engaged blocks execution even when epoch matches
    check('engaged => is_execution_stale(2) True', kernel.is_execution_stale(2) is True)
    kernel.set_hard_stop(False)
    check('released + matching epoch => stale False', kernel.is_execution_stale(2) is False)

    # exactly one audit event per real transition (enable,disable,enable,disable = 4)
    # (we did: enable, disable, enable, disable across this test)
    check('audit events == real transitions (4)', hs_event_count(path) == 4, got=hs_event_count(path))

    sec('STRICT — _control_authorized gate')
    from app import _control_authorized
    class Req:
        def __init__(self, headers=None, addr=None):
            self.headers = headers or {}; self.remote_addr = addr

    os.environ.pop("ARGUS_CONTROL_TOKEN", None)
    check('no token + loopback -> allowed', _control_authorized(Req(addr="127.0.0.1")) is True)
    check('no token + ::1 -> allowed', _control_authorized(Req(addr="::1")) is True)
    check('no token + remote IP -> denied', _control_authorized(Req(addr="10.0.0.5")) is False)

    os.environ["ARGUS_CONTROL_TOKEN"] = "s3cret"
    try:
        check('token set + correct header -> allowed',
              _control_authorized(Req(headers={"X-Control-Token": "s3cret"}, addr="10.0.0.5")) is True)
        check('token set + wrong header -> denied',
              _control_authorized(Req(headers={"X-Control-Token": "nope"}, addr="127.0.0.1")) is False)
        check('token set + missing header -> denied',
              _control_authorized(Req(addr="127.0.0.1")) is False)
    finally:
        os.environ.pop("ARGUS_CONTROL_TOKEN", None)


if __name__ == '__main__':
    print('=' * 64)
    print('  ARGUS Phase 8 — Part 2/7: Emergency stop + epoch tests')
    print('=' * 64)
    try:
        test_normal()
        test_hacker()
        test_strict_teacher()
    finally:
        cleanup()
    print('\n' + '=' * 64)
    total = passed + failed
    rate = (passed / total * 100) if total else 0.0
    status = 'ALL PASS' if failed == 0 else 'FAILURES'
    print(f'  RESULT: {passed} passed | {failed} failed | {status} ({rate:.1f}%)')
    print('=' * 64)
    sys.exit(1 if failed else 0)
