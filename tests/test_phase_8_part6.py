# -*- coding: utf-8 -*-
"""
ARGUS Phase 8 Tests — Part 6/7: execution identity + Fence A + R-REOPEN + preflight.

Covers the Part 6 build (keep-deferred execution-creation model):
  - approve() stamps approval_generation + approval_epoch and refuses under hard stop
  - promote_approved() carries generation + epoch onto the execution
  - R-REOPEN, state-branched so it can NEVER resurrect an ambiguous-delivery send
  - Control 1 executor preflight: hard stop / stale epoch -> HELD (owner_token cleared)
  - fenced pre-draft claim: a claimed/superseded row is never advanced

NOTE: R-REOPEN here completes Phase 8 Part 5's deferred reopen piece too.
Fence B (FAILED->queue HELD bridge) is deferred — see DEFERRED.md.

Throwaway temp DB. Three angles: Normal · Hacker · Strict Teacher.
Run standalone: python tests/test_phase_8_part6.py
"""
import os, sys, json, time, uuid, sqlite3, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import argus.db as dbmod
import argus.queue as q
import argus.audit as audit
import argus.kernel as kernel
import argus.executor as executor
import argus.trust_ledger as trust_ledger
from argus import gmail_client

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
    for m in (q, audit, kernel, executor, trust_ledger):
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

def mk_pending(action="email.send.external"):
    r = q.enqueue({"action_type": action,
                   "entities": {"recipient": "a@b.com", "subject": "S", "body": "B"},
                   "intent": "x"}, {"action_expiry": 300})
    return r["id"]

def qcol(path, qid, col):
    c = raw(path); r = c.execute(f"SELECT {col} FROM approval_queue WHERE id=?", (qid,)).fetchone()
    c.close(); return r[col] if r else None

def set_q(path, qid, **cols):
    c = raw(path); sets = ", ".join(f"{k}=?" for k in cols)
    c.execute(f"UPDATE approval_queue SET {sets} WHERE id=?", (*cols.values(), qid))
    c.commit(); c.close()

def mk_exec(path, qid, status, gen=1, epoch=0, owner_token=None):
    eid = str(uuid.uuid4()); now = int(time.time())
    c = raw(path)
    c.execute(
        "INSERT INTO pending_executions (execution_id, approval_id, action_type, "
        "payload_json, status, owner_token, approval_generation, approval_epoch, "
        "approved_at, execute_after, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, qid, "email.send.external", json.dumps({"entities": {"recipient": "a@b.com"}}),
         status, owner_token, gen, epoch, now, now, now, now))
    c.commit(); c.close()
    return eid

def ecol(path, eid, col):
    c = raw(path); r = c.execute(f"SELECT {col} FROM pending_executions WHERE execution_id=?", (eid,)).fetchone()
    c.close(); return r[col] if r else None

def audit_count(path, event_type):
    c = raw(path); n = c.execute("SELECT COUNT(*) FROM audit_events WHERE event_type=?",
                                 (event_type,)).fetchone()[0]; c.close(); return n


class FakeGmail:
    def __init__(self): self.created = []; self.sent = []
    def get_history_id(self): return "h1"
    def create_draft(self, **kw): self.created.append(kw); return "draft-1"
    def get_draft_recipients(self, draft_id): return {"to": ["a@b.com"], "cc": [], "bcc": []}
    def send_draft(self, draft_id): self.sent.append(draft_id); return {"message_id": "m1"}
    def trash_message(self, mid): return {"message_id": mid, "trashed": True}

def install_gmail(fake):
    for n in ("get_history_id", "create_draft", "get_draft_recipients", "send_draft", "trash_message"):
        setattr(gmail_client, n, getattr(fake, n))


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 1 — NORMAL
# ─────────────────────────────────────────────────────────────────────────────
def test_normal():
    sec('NORMAL — approve stamps a fresh generation + the live epoch')
    path = fresh_db()
    qid = mk_pending()
    r = q.approve(qid)
    check('approve succeeds', r.get("status") == "APPROVED")
    check('approval_generation bumped 0 -> 1', qcol(path, qid, 'approval_generation') == 1)
    check('approval_epoch stamped (0 on a fresh system)', qcol(path, qid, 'approval_epoch') == 0)

    sec('NORMAL — promote carries generation + epoch onto the execution')
    set_q(path, qid, approved_at=int(time.time()) - 100)  # undo elapsed
    executor.promote_approved()
    c = raw(path); ex = c.execute("SELECT * FROM pending_executions WHERE approval_id=?", (qid,)).fetchone(); c.close()
    check('execution created', ex is not None)
    check('execution carries generation 1', ex and ex["approval_generation"] == 1)
    check('execution carries epoch 0', ex and ex["approval_epoch"] == 0)

    sec('NORMAL — reopen a HELD item with no execution -> PENDING')
    qid2 = mk_pending()
    set_q(path, qid2, status='HELD')
    r = q.reopen(qid2, "operator re-queued after review")
    check('reopen succeeds', r.get("status") == "PENDING")
    check('queue back to PENDING', qcol(path, qid2, 'status') == 'PENDING')
    check('QUEUE_REOPENED audited', audit_count(path, 'QUEUE_REOPENED') == 1)


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 2 — HACKER (adversarial)
# ─────────────────────────────────────────────────────────────────────────────
def test_hacker():
    sec('[HACKER] approve is refused while the emergency stop is engaged')
    path = fresh_db()
    qid = mk_pending()
    kernel.set_hard_stop(True, updated_by="test", reason="drill")
    r = q.approve(qid)
    check('approve -> HARD_STOP_ACTIVE', r.get("error_code") == "HARD_STOP_ACTIVE")
    check('item stays PENDING', qcol(path, qid, 'status') == 'PENDING')
    kernel.set_hard_stop(False, updated_by="test")

    sec('[HACKER] reopen NEVER resurrects an ambiguous-delivery send')
    for amb in ("SENDING", "MANUAL_REVIEW"):
        qx = mk_pending(); q.approve(qx); set_q(path, qx, status='HELD')
        mk_exec(path, qx, amb, gen=1)
        r = q.reopen(qx, "try to reopen mid-send")
        check(f'{amb} execution -> EXECUTION_OUTCOME_UNRESOLVED',
              r.get("error_code") == "EXECUTION_OUTCOME_UNRESOLVED")
        check(f'{amb}: queue NOT reopened', qcol(path, qx, 'status') == 'HELD')

    sec('[HACKER] Fence A refuses to supersede a CLAIMED pre-send execution')
    qc = mk_pending(); q.approve(qc); set_q(path, qc, status='HELD')
    mk_exec(path, qc, "DRAFT_READY", gen=1, owner_token="held-by-worker")
    r = q.reopen(qc, "reopen while worker holds it")
    check('claimed DRAFT_READY -> EXECUTION_OUTCOME_UNRESOLVED',
          r.get("error_code") == "EXECUTION_OUTCOME_UNRESOLVED")

    sec('[HACKER] cancellation is TERMINAL — a cancelled item cannot be reopened')
    qcan = mk_pending(); set_q(path, qcan, status='CANCELLED')
    r = q.reopen(qcan, "try to revive a cancelled item")
    check('cancelled -> INVALID_REOPEN_STATE', r.get("error_code") == "INVALID_REOPEN_STATE")

    sec('[HACKER] hard stop mid-flight HOLDS a pre-send execution (never sends)')
    install_gmail(FakeGmail())
    qh = mk_pending(); q.approve(qh)
    eid = mk_exec(path, qh, "DRAFT_PENDING", gen=1, epoch=qcol(path, qh, 'approval_epoch'))
    kernel.set_hard_stop(True, updated_by="test", reason="mid-flight")  # bumps epoch
    executor.advance_executions()
    check('pre-send execution -> HELD', ecol(path, eid, 'status') == 'HELD')
    check('owner_token cleared on hold', ecol(path, eid, 'owner_token') is None)
    check('HELD_STALE_EPOCH audited', audit_count(path, 'HELD_STALE_EPOCH') >= 1)
    kernel.set_hard_stop(False, updated_by="test")

    sec('[HACKER] a stale epoch (no active stop) still HOLDS at preflight')
    install_gmail(FakeGmail())
    qe = mk_pending(); q.approve(qe)
    # Bump the epoch past the one stamped on this execution, then clear the stop.
    eid2 = mk_exec(path, qe, "DRAFT_PENDING", gen=1, epoch=qcol(path, qe, 'approval_epoch'))
    kernel.set_hard_stop(True, updated_by="test"); kernel.set_hard_stop(False, updated_by="test")
    executor.advance_executions()
    check('stale-epoch execution -> HELD', ecol(path, eid2, 'status') == 'HELD')


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 3 — STRICT TEACHER (boundaries)
# ─────────────────────────────────────────────────────────────────────────────
def test_strict():
    sec('[STRICT] reopen demands a non-empty, capped reason')
    path = fresh_db()
    qid = mk_pending(); set_q(path, qid, status='HELD')
    check('empty reason -> REASON_REQUIRED', q.reopen(qid, "  ").get("error_code") == "REASON_REQUIRED")
    check('over-long reason -> REJECTION_REASON_TOO_LONG',
          q.reopen(qid, "x" * 501).get("error_code") == "REJECTION_REASON_TOO_LONG")
    check('item untouched by rejected reopen', qcol(path, qid, 'status') == 'HELD')

    sec('[STRICT] reopen of a COMPLETED execution reconciles forward, never reopens')
    qco = mk_pending(); q.approve(qco); set_q(path, qco, status='HELD')
    mk_exec(path, qco, "COMPLETED", gen=1)
    r = q.reopen(qco, "tried to reopen an already-sent item")
    check('COMPLETED -> ALREADY_EXECUTED', r.get("error_code") == "ALREADY_EXECUTED")
    check('queue reconciled to EXECUTED', qcol(path, qco, 'status') == 'EXECUTED')

    sec('[STRICT] FAILED (proven unsent) reopens WITHOUT trying to supersede')
    qf = mk_pending(); q.approve(qf); set_q(path, qf, status='HELD')
    fid = mk_exec(path, qf, "FAILED", gen=1)
    r = q.reopen(qf, "proven unsent, re-queue")
    check('FAILED execution -> reopen succeeds', r.get("status") == "PENDING")
    check('FAILED row left intact (not superseded)', ecol(path, fid, 'status') == 'FAILED')

    sec('[STRICT] Fence A supersedes an UNCLAIMED pre-send row, then reopens')
    qs = mk_pending(); q.approve(qs); set_q(path, qs, status='HELD')
    sid = mk_exec(path, qs, "DRAFT_READY", gen=1, owner_token=None)
    r = q.reopen(qs, "re-queue an unclaimed draft")
    check('reopen succeeds', r.get("status") == "PENDING")
    check('unclaimed execution -> SUPERSEDED', ecol(path, sid, 'status') == 'SUPERSEDED')

    sec('[STRICT] re-approve after reopen mints the NEXT generation (no collision)')
    set_q(path, qs, approved_at=None)  # reopened item is PENDING again
    r2 = q.approve(qs)
    check('re-approve succeeds', r2.get("status") == "APPROVED")
    check('generation advances 1 -> 2', qcol(path, qs, 'approval_generation') == 2)
    set_q(path, qs, approved_at=int(time.time()) - 100)
    executor.promote_approved()
    c = raw(path)
    gens = sorted(r["approval_generation"] for r in
                  c.execute("SELECT approval_generation FROM pending_executions WHERE approval_id=?", (qs,)))
    c.close()
    check('gen-1 (SUPERSEDED) and gen-2 executions coexist', gens == [1, 2], got=gens)

    sec('[STRICT] the executor never advances a CLAIMED DRAFT_PENDING row')
    fake = FakeGmail(); install_gmail(fake)
    qcl = mk_pending(); q.approve(qcl)
    cid = mk_exec(path, qcl, "DRAFT_PENDING", gen=1, epoch=qcol(path, qcl, 'approval_epoch'),
                  owner_token="someone-else")
    executor.advance_executions()
    check('claimed row not advanced (still DRAFT_PENDING)', ecol(path, cid, 'status') == 'DRAFT_PENDING')
    check('claimed row was never drafted (draft_id still NULL)', ecol(path, cid, 'draft_id') is None)


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
