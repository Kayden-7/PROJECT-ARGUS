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
        self.drafts[did] = True
        return did

    def draft_exists(self, draft_id): return draft_id in self.drafts

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
    for name in ("get_history_id","create_draft","draft_exists","send_draft","trash_message"):
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

    clean()

finally:
    print()
    print('-' * 62)
    status = 'ALL CLEAR' if failed == 0 else 'FAILURES DETECTED'
    print(f'  RESULT: {passed} passed | {failed} failed | {status}')
    print('=' * 62)
    print()
    sys.exit(0 if failed == 0 else 1)
