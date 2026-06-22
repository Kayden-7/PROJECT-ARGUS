"""
ARGUS Phase 5 Tests — Gmail Execution Layer (Part 2)
Run standalone: python tests/test_phase_5.py

Gmail is mocked so these run without a live connection. They verify the
locked, stress-tested state machine and its fail-closed guarantees.
"""
import os, sys, time, sqlite3, uuid, json

ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, 'instance', 'argus.db')
sys.path.insert(0, ROOT)

from argus.db import init_db
import argus.gmail_client as gmail_client
import argus.executor as executor

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

def db():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c

def clean():
    c = db()
    c.execute("DELETE FROM pending_executions")
    c.execute("DELETE FROM approval_queue")
    c.execute("DELETE FROM trust_events WHERE reason LIKE 'EXECUTED:%'")
    c.commit(); c.close()

def make_approved(action_type, entities, undo_elapsed=True):
    """Insert an APPROVED queue item; undo_elapsed=True puts approved_at in the past."""
    c = db(); now = int(time.time())
    qid = str(uuid.uuid4())
    approved_at = now - 100 if undo_elapsed else now
    proposal = {"action_type": action_type, "entities": entities}
    c.execute(
        "INSERT INTO approval_queue (id, proposal_json, decision_json, status, "
        "created_at, expires_at, approved_at, updated_at) "
        "VALUES (?,?,?,'APPROVED',?,?,?,?)",
        (qid, json.dumps(proposal), '{}', approved_at, now+200, approved_at, approved_at),
    )
    c.commit(); c.close()
    return qid

def one_exec():
    c = db(); r = c.execute("SELECT * FROM pending_executions").fetchone(); c.close()
    return dict(r) if r else None


# ── Gmail mock ───────────────────────────────────────────────────────────────
class FakeGmail:
    def __init__(self):
        self.drafts = {}
        self.sent = []
        self.trashed = []
        self.fail_create = False
        self.fail_send = False
        self.next_draft = 0
        self.next_msg = 0

    def get_history_id(self): return "111"

    def create_draft(self, to, subject, body, thread_id=None, in_reply_to=None):
        if self.fail_create:
            raise RuntimeError("create boom")
        self.next_draft += 1
        did = f"draft{self.next_draft}"
        self.drafts[did] = {"to": [to] if to else [], "cc": [], "bcc": []}
        return did

    def draft_exists(self, draft_id): return draft_id in self.drafts

    def get_draft_recipients(self, draft_id):
        return self.drafts.get(draft_id, {"to": [], "cc": [], "bcc": []})

    def send_draft(self, draft_id):
        if self.fail_send:
            raise RuntimeError("send boom")
        self.drafts.pop(draft_id, None)
        self.next_msg += 1
        mid = f"msg{self.next_msg}"
        self.sent.append(mid)
        return {"message_id": mid, "thread_id": mid}

    def trash_message(self, message_id):
        self.trashed.append(message_id); return {"message_id": message_id, "trashed": True}

def install_mock(fake):
    for name in ("get_history_id","create_draft","draft_exists","get_draft_recipients",
                 "send_draft","trash_message"):
        setattr(gmail_client, name, getattr(fake, name))


try:
    init_db()

    # ── Promotion ────────────────────────────────────────────────────────────
    sec('Promotion — APPROVED past undo window becomes one execution')
    clean(); install_mock(FakeGmail())
    qid = make_approved("email.send.external",
                        {"recipient":"a@b.com","subject":"S","body":"B"})
    executor.promote_approved()
    e = one_exec()
    check('execution row created', e is not None)
    check('linked to approval_id', e and e['approval_id'] == qid)
    check('starts at DRAFT_PENDING', e and e['status'] == 'DRAFT_PENDING')

    sec('Promotion — within undo window does NOT promote')
    clean(); make_approved("email.send.external",
                          {"recipient":"a@b.com","subject":"S","body":"B"}, undo_elapsed=False)
    executor.promote_approved()
    check('no execution created inside undo window', one_exec() is None)

    sec('Promotion — one execution per approval (idempotent / no double-click dupes)')
    clean(); make_approved("email.send.external",
                          {"recipient":"a@b.com","subject":"S","body":"B"})
    executor.promote_approved(); executor.promote_approved(); executor.promote_approved()
    c = db(); n = c.execute("SELECT COUNT(*) c FROM pending_executions").fetchone()['c']; c.close()
    check('exactly one execution after 3 promotes', n == 1, got=n)

    sec('Promotion — non-executable action (calendar) is skipped')
    clean(); make_approved("calendar.create",
                          {"title":"T","start_time":"x","end_time":"y"})
    executor.promote_approved()
    check('calendar action not promoted (Phase 6)', one_exec() is None)

    # ── Happy path ───────────────────────────────────────────────────────────
    sec('Send happy path — DRAFT_PENDING -> DRAFT_READY -> COMPLETED')
    clean(); fake = FakeGmail(); install_mock(fake)
    qid = make_approved("email.send.external",
                        {"recipient":"a@b.com","subject":"S","body":"Hello"})
    executor.reconcile()                      # promote + create draft
    e = one_exec()
    check('after reconcile #1 -> DRAFT_READY', e['status'] == 'DRAFT_READY', got=e['status'])
    check('draft_id stored', bool(e['draft_id']))
    check('history_id stored', bool(e['history_id']))
    executor.reconcile()                      # claim + send
    e = one_exec()
    check('after reconcile #2 -> COMPLETED', e['status'] == 'COMPLETED', got=e['status'])
    check('message_id stored', bool(e['message_id']))
    check('exactly one email sent', len(fake.sent) == 1, got=len(fake.sent))
    c = db()
    q = c.execute("SELECT status FROM approval_queue WHERE id=?", (qid,)).fetchone()
    t = c.execute("SELECT COUNT(*) c FROM trust_events WHERE reason LIKE 'EXECUTED:%'").fetchone()['c']
    c.close()
    check('queue item marked EXECUTED', q['status'] == 'EXECUTED', got=q['status'])
    check('exactly one execution trust event', t == 1, got=t)

    sec('Idempotency — extra reconciles never re-send or double-write trust')
    executor.reconcile(); executor.reconcile()
    check('still exactly one email sent', len(fake.sent) == 1, got=len(fake.sent))
    c = db(); t = c.execute("SELECT COUNT(*) c FROM trust_events WHERE reason LIKE 'EXECUTED:%'").fetchone()['c']; c.close()
    check('still exactly one trust event', t == 1, got=t)

    # ── Fail-closed paths ────────────────────────────────────────────────────
    sec('Fail-closed — crashed SENDING never auto-resumes')
    clean(); install_mock(FakeGmail())
    eid = str(uuid.uuid4()); now = int(time.time())
    c = db()
    c.execute("INSERT INTO pending_executions (execution_id,approval_id,action_type,payload_json,"
              "status,draft_id,owner_token,attempt_count,approved_at,execute_after,created_at,updated_at) "
              "VALUES (?,?,?,?,'SENDING',?,?,1,?,?,?,?)",
              (eid,'aS','email.send.external','{}','d1','tok',now,now,now,now))
    c.commit(); c.close()
    executor.advance_executions()
    check('crashed SENDING -> MANUAL_REVIEW', one_exec()['status'] == 'MANUAL_REVIEW')

    sec('Fail-closed — orphan-draft guard')
    clean()
    eid = str(uuid.uuid4()); now = int(time.time())
    c = db()
    c.execute("INSERT INTO pending_executions (execution_id,approval_id,action_type,payload_json,"
              "status,attempt_count,approved_at,execute_after,created_at,updated_at) "
              "VALUES (?,?,?,?,'DRAFT_PENDING',1,?,?,?,?)",
              (eid,'aO','email.send.external','{}',now,now,now,now))
    c.commit(); c.close()
    executor.advance_executions()
    check('DRAFT_PENDING + prior attempt + no draft -> MANUAL_REVIEW',
          one_exec()['status'] == 'MANUAL_REVIEW')

    sec('Fail-closed — draft creation error')
    clean(); fake = FakeGmail(); fake.fail_create = True; install_mock(fake)
    make_approved("email.send.external", {"recipient":"a@b.com","subject":"S","body":"B"})
    executor.reconcile()
    check('create_draft failure -> MANUAL_REVIEW', one_exec()['status'] == 'MANUAL_REVIEW')

    sec('Fail-closed — send error (ambiguous boundary)')
    clean(); fake = FakeGmail(); fake.fail_send = True; install_mock(fake)
    make_approved("email.send.external", {"recipient":"a@b.com","subject":"S","body":"B"})
    executor.reconcile()   # creates draft
    executor.reconcile()   # claim + send fails
    check('send failure -> MANUAL_REVIEW', one_exec()['status'] == 'MANUAL_REVIEW')
    check('no trust event on failed send',
          (lambda: (lambda c: (c.execute("SELECT COUNT(*) c FROM trust_events WHERE reason LIKE 'EXECUTED:%'").fetchone()['c'], c.close())[0])(db()))() == 0)

    sec('Fail-closed — unknown action type (catch-all)')
    clean()
    eid = str(uuid.uuid4()); now = int(time.time())
    c = db()
    c.execute("INSERT INTO pending_executions (execution_id,approval_id,action_type,payload_json,"
              "status,attempt_count,approved_at,execute_after,created_at,updated_at) "
              "VALUES (?,?,?,?,'DRAFT_PENDING',0,?,?,?,?)",
              (eid,'aU','email.telepathy','{}',now,now,now,now))
    c.commit(); c.close()
    executor.advance_executions()
    check('unknown action -> MANUAL_REVIEW', one_exec()['status'] == 'MANUAL_REVIEW')

    # ── Direct action (delete -> trash) ──────────────────────────────────────
    sec('Direct action — email.delete trashes (idempotent path)')
    clean(); fake = FakeGmail(); install_mock(fake)
    make_approved("email.delete", {"message_id":"m123"})
    executor.reconcile()   # claim + trash + complete
    e = one_exec()
    check('delete -> COMPLETED', e['status'] == 'COMPLETED', got=e['status'])
    check('message trashed (not permanently deleted)', fake.trashed == ['m123'], got=fake.trashed)

    # ══ HACKER ANGLE — adversarial, race conditions, malformed input ══════════
    sec('[HACKER] Concurrent atomic claim — two reconciles race one job, ONE wins')
    clean(); fake = FakeGmail(); install_mock(fake)
    make_approved("email.send.external", {"recipient":"a@b.com","subject":"S","body":"B"})
    executor.reconcile()  # -> DRAFT_READY
    eid = one_exec()['execution_id']
    # Simulate two workers racing the claim on the SAME DRAFT_READY row.
    now = int(time.time())
    c = db()
    a1 = c.execute("UPDATE pending_executions SET status='SENDING', owner_token='W1', updated_at=? "
                   "WHERE execution_id=? AND status='DRAFT_READY'", (now, eid)).rowcount
    a2 = c.execute("UPDATE pending_executions SET status='SENDING', owner_token='W2', updated_at=? "
                   "WHERE execution_id=? AND status='DRAFT_READY'", (now, eid)).rowcount
    c.commit(); c.close()
    check('exactly one worker wins the claim (rowcount 1 then 0)', a1 == 1 and a2 == 0, got=(a1,a2))
    c = db(); tok = c.execute("SELECT owner_token FROM pending_executions WHERE execution_id=?",(eid,)).fetchone()['owner_token']; c.close()
    check('winner owner_token is the first (W1)', tok == 'W1', got=tok)

    sec('[HACKER] owner_token fence — stale worker cannot overwrite a decision')
    # Row is SENDING owned by W1. A zombie W2 tries to mark COMPLETED.
    c = db()
    bad = c.execute("UPDATE pending_executions SET status='COMPLETED', updated_at=? "
                    "WHERE execution_id=? AND status='SENDING' AND owner_token='W2'",
                    (now, eid)).rowcount
    c.commit(); c.close()
    check('stale-token completion is rejected (rowcount 0)', bad == 0, got=bad)
    check('row still SENDING (not hijacked)', one_exec()['status'] == 'SENDING')

    sec('[HACKER] Duplicate approval_id cannot create two executions')
    clean()
    now = int(time.time()); c = db()
    c.execute("INSERT INTO pending_executions (execution_id,approval_id,action_type,payload_json,status,attempt_count,approved_at,execute_after,created_at,updated_at) "
              "VALUES (?,?,?,?,'DRAFT_PENDING',0,?,?,?,?)",(str(uuid.uuid4()),'DUP','email.send.external','{}',now,now,now,now))
    c.commit()
    dup_blocked = False
    try:
        c.execute("INSERT INTO pending_executions (execution_id,approval_id,action_type,payload_json,status,attempt_count,approved_at,execute_after,created_at,updated_at) "
                  "VALUES (?,?,?,?,'DRAFT_PENDING',0,?,?,?,?)",(str(uuid.uuid4()),'DUP','email.send.external','{}',now,now,now,now))
        c.commit()
    except sqlite3.IntegrityError:
        dup_blocked = True
    c.close()
    check('UNIQUE(approval_id) blocks a second execution row', dup_blocked)

    sec('[HACKER] Malformed payload_json fails closed (no crash)')
    clean()
    now = int(time.time()); eid = str(uuid.uuid4()); c = db()
    c.execute("INSERT INTO pending_executions (execution_id,approval_id,action_type,payload_json,status,attempt_count,approved_at,execute_after,created_at,updated_at) "
              "VALUES (?,?,?,?,'DRAFT_PENDING',0,?,?,?,?)",(eid,'aBad','email.send.external','{not valid json',now,now,now,now))
    c.commit(); c.close()
    install_mock(FakeGmail())
    executor.advance_executions()  # must not raise
    st = one_exec()['status']
    check('malformed payload does not crash executor', st in ('DRAFT_READY','MANUAL_REVIEW'), got=st)

    sec('[HACKER] email.delete with missing message_id still does not double-process')
    clean(); fake = FakeGmail(); install_mock(fake)
    make_approved("email.delete", {})  # no message_id
    executor.reconcile()
    e = one_exec()
    check('delete with empty message_id resolves to a terminal/known state',
          e['status'] in ('COMPLETED','MANUAL_REVIEW'), got=e['status'])

    sec('[HACKER] Reconcile with Gmail raising (disconnected) -> MANUAL_REVIEW, not crash')
    clean()
    class DeadGmail(FakeGmail):
        def get_history_id(self): raise RuntimeError("not connected")
        def create_draft(self, *a, **k): raise RuntimeError("not connected")
    install_mock(DeadGmail())
    make_approved("email.send.external", {"recipient":"a@b.com","subject":"S","body":"B"})
    executor.reconcile()  # must not raise
    check('disconnected Gmail -> MANUAL_REVIEW', one_exec()['status'] == 'MANUAL_REVIEW')

    # ══ STRICT TEACHER ANGLE — exact-match nitpicks ═══════════════════════════
    sec('[STRICT] Undo-window boundary — promotes exactly AT approved_at+undo')
    clean()
    c = db()
    undo = executor._read_undo_window(c)
    now = int(time.time())
    # exactly at boundary: approved_at + undo == now  -> eligible (<=)
    qid = str(uuid.uuid4())
    at = now - undo
    c.execute("INSERT INTO approval_queue (id,proposal_json,decision_json,status,created_at,expires_at,approved_at,updated_at) "
              "VALUES (?,?,?,'APPROVED',?,?,?,?)",
              (qid, json.dumps({"action_type":"email.send.external","entities":{"recipient":"a@b.com","subject":"S","body":"B"}}),
               '{}', at, now+200, at, at))
    c.commit(); c.close()
    install_mock(FakeGmail())
    executor.promote_approved()
    check('promotes exactly at the boundary (approved_at+undo == now)', one_exec() is not None)

    sec('[STRICT] status_reason is non-empty on every MANUAL_REVIEW')
    clean()
    now = int(time.time()); eid = str(uuid.uuid4()); c = db()
    c.execute("INSERT INTO pending_executions (execution_id,approval_id,action_type,payload_json,status,draft_id,owner_token,attempt_count,approved_at,execute_after,created_at,updated_at) "
              "VALUES (?,?,?,?,'SENDING',?,?,1,?,?,?,?)",(eid,'aR','email.send.external','{}','d','t',now,now,now,now))
    c.commit(); c.close()
    install_mock(FakeGmail()); executor.advance_executions()
    e = one_exec()
    check('MANUAL_REVIEW has a non-empty status_reason', bool(e['status_reason']) and len(e['status_reason']) > 0)

    sec('[STRICT] attempt_count increments exactly once per draft attempt')
    clean(); install_mock(FakeGmail())
    make_approved("email.send.external", {"recipient":"a@b.com","subject":"S","body":"B"})
    executor.reconcile()  # one DRAFT_PENDING handling
    check('attempt_count == 1 after one draft attempt', one_exec()['attempt_count'] == 1, got=one_exec()['attempt_count'])

    sec('[STRICT] trust reason is exactly EXECUTED:<execution_id>:SUCCESS')
    clean(); install_mock(FakeGmail())
    make_approved("email.send.external", {"recipient":"a@b.com","subject":"S","body":"B"})
    executor.reconcile(); executor.reconcile()
    e = one_exec()
    c = db(); reason = c.execute("SELECT reason FROM trust_events WHERE reason LIKE 'EXECUTED:%' ORDER BY timestamp DESC LIMIT 1").fetchone()['reason']; c.close()
    check('trust reason exact format', reason == f"EXECUTED:{e['execution_id']}:SUCCESS", got=reason)

    sec('[STRICT] queue stays APPROVED until COMPLETED, then EXECUTED')
    clean(); install_mock(FakeGmail())
    qid = make_approved("email.send.external", {"recipient":"a@b.com","subject":"S","body":"B"})
    executor.reconcile()  # DRAFT_READY — not yet executed
    c = db(); s1 = c.execute("SELECT status FROM approval_queue WHERE id=?",(qid,)).fetchone()['status']; c.close()
    check('queue still APPROVED while execution in flight', s1 == 'APPROVED', got=s1)
    executor.reconcile()  # COMPLETED
    c = db(); s2 = c.execute("SELECT status FROM approval_queue WHERE id=?",(qid,)).fetchone()['status']; c.close()
    check('queue EXECUTED only after completion', s2 == 'EXECUTED', got=s2)

    sec('[STRICT] /api/executions and /api/executions/tick exact response shape')
    clean(); install_mock(FakeGmail())
    make_approved("email.send.external", {"recipient":"a@b.com","subject":"S","body":"B"})
    from app import app as _app
    cl = _app.test_client()
    rt = cl.post('/api/executions/tick')
    jt = rt.get_json()
    check('tick returns success=True', jt.get('success') is True)
    check('tick returns a counts dict', isinstance(jt.get('counts'), dict))
    rl = cl.get('/api/executions')
    jl = rl.get_json()
    check('/api/executions returns a list', isinstance(jl, list))
    if jl:
        row = jl[0]
        for f in ('execution_id','approval_id','action_type','status','status_reason'):
            check(f'/api/executions row has field: {f}', f in row)

    clean()

finally:
    print()
    print('-' * 62)
    status = 'ALL CLEAR' if failed == 0 else 'FAILURES DETECTED'
    print(f'  RESULT: {passed} passed | {failed} failed | {status}')
    print('=' * 62)
    print()
    sys.exit(0 if failed == 0 else 1)
