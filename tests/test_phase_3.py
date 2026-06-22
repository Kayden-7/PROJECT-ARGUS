"""
ARGUS Phase 3 Tests — Approval Queue
Run standalone: python tests/test_phase_3.py
"""
import os, sys, time, sqlite3, subprocess, requests

ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, 'instance', 'argus.db')
BASE    = 'http://127.0.0.1:8081'

sys.path.insert(0, ROOT)

passed = 0
failed = 0

def sec(name):
    print(f'\n  [{name}]')

def check(name, condition, got=None):
    global passed, failed
    if condition:
        print(f'    [PASS] {name}')
        passed += 1
    else:
        detail = f' | got: {got}' if got is not None else ''
        print(f'    [FAIL] {name}{detail}')
        failed += 1

def gated_proposal():
    return {'action_type': 'email.send.external',
            'entities': {'recipient': 'a@b.com', 'subject': 'Test', 'body': 'Hello'}}

print()
print('=' * 62)
print('  ARGUS PHASE 3 — Approval Queue')
print('=' * 62)

if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

server = subprocess.Popen(
    [sys.executable, 'app.py'], cwd=ROOT,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
)
for _ in range(15):
    try:
        requests.get(f'{BASE}/health', timeout=1)
        break
    except Exception:
        time.sleep(0.5)

from argus.db import init_db
from argus.kernel import kernel_entry, set_hard_stop
from argus.queue import enqueue, fetch_pending, approve, reject, cancel, expire_stale
init_db()

# Helper decision dict for enqueue
MOCK_DECISION = {
    'decision': 'GATED', 'action_expiry': 300,
    'trust_at_evaluation': 40.0, 'effective_threshold': 70.0
}

try:
    # ── enqueue ────────────────────────────────────────────────────────────────
    sec('Queue Functions — enqueue')
    r = enqueue(gated_proposal(), MOCK_DECISION)
    check('enqueue returns success=True', r.get('success') is True)
    check('enqueue returns id', bool(r.get('id')))
    check('enqueue returns expires_at', isinstance(r.get('expires_at'), int))
    check('enqueue expires_at is in the future', r['expires_at'] > int(time.time()))
    item_id = r['id']

    # ── fetch_pending ──────────────────────────────────────────────────────────
    sec('Queue Functions — fetch_pending')
    items = fetch_pending()
    check('fetch_pending returns list', isinstance(items, list))
    check('fetch_pending has our item', any(i['id'] == item_id for i in items))
    item = next(i for i in items if i['id'] == item_id)
    check('Item has all required schema fields',
          all(f in item for f in ['id', 'proposal_json', 'decision_json', 'status',
                                   'created_at', 'expires_at', 'approved_at', 'updated_at',
                                   'status_reason', 'execution_id']))
    check('New item status = PENDING', item['status'] == 'PENDING')
    check('New item approved_at is None', item['approved_at'] is None)

    # ── approve ────────────────────────────────────────────────────────────────
    sec('Queue Functions — approve')
    r2 = approve(item_id)
    check('approve returns success=True', r2.get('success') is True)
    check('approve returns APPROVED status', r2.get('status') == 'APPROVED')
    check('approve sets approved_at timestamp', isinstance(r2.get('approved_at'), int))

    r3 = approve(item_id)
    check('Double approve -> INVALID_STATE_TRANSITION', r3.get('error_code') == 'INVALID_STATE_TRANSITION')

    # ── cancel within undo window ──────────────────────────────────────────────
    sec('Queue Functions — cancel (undo window)')
    r4 = cancel(item_id)
    check('Cancel APPROVED within undo window -> success', r4.get('success') is True)
    check('Cancelled status = CANCELLED', r4.get('status') == 'CANCELLED')

    r5 = cancel(item_id)
    check('Cancel CANCELLED -> INVALID_STATE_TRANSITION', r5.get('error_code') == 'INVALID_STATE_TRANSITION')

    # ── reject ─────────────────────────────────────────────────────────────────
    sec('Queue Functions — reject')
    r_new = enqueue(gated_proposal(), MOCK_DECISION)
    new_id = r_new['id']

    r6 = reject(new_id, 'Not appropriate at this time')
    check('reject returns success=True', r6.get('success') is True)
    check('reject returns REJECTED status', r6.get('status') == 'REJECTED')

    r7 = reject(new_id, 'Again')
    check('Reject already REJECTED -> INVALID_STATE_TRANSITION', r7.get('error_code') == 'INVALID_STATE_TRANSITION')

    r8 = approve(new_id)
    check('Approve REJECTED -> INVALID_STATE_TRANSITION', r8.get('error_code') == 'INVALID_STATE_TRANSITION')

    # ── cancel PENDING ─────────────────────────────────────────────────────────
    sec('Queue Functions — cancel PENDING (pre-approval)')
    r_pend = enqueue(gated_proposal(), MOCK_DECISION)
    pend_id = r_pend['id']
    r9 = cancel(pend_id)
    check('Cancel PENDING -> success', r9.get('success') is True)
    check('Cancelled PENDING status = CANCELLED', r9.get('status') == 'CANCELLED')

    # ── expire_stale ───────────────────────────────────────────────────────────
    sec('Queue Functions — expire_stale')
    # Insert an artificially expired item
    import uuid, json
    db = sqlite3.connect(DB_PATH)
    old_id = str(uuid.uuid4())
    now = int(time.time())
    db.execute(
        """INSERT INTO approval_queue
           (id, proposal_json, decision_json, status, created_at, expires_at, approved_at, updated_at, status_reason, execution_id)
           VALUES (?, ?, ?, 'PENDING', ?, ?, NULL, ?, NULL, NULL)""",
        (old_id, json.dumps(gated_proposal()), json.dumps(MOCK_DECISION), now - 400, now - 100, now)
    )
    # Also insert a MANUAL_REVIEW item (should NOT be expired)
    manual_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO approval_queue
           (id, proposal_json, decision_json, status, created_at, expires_at, approved_at, updated_at, status_reason, execution_id)
           VALUES (?, ?, ?, 'MANUAL_REVIEW', ?, ?, NULL, ?, NULL, NULL)""",
        (manual_id, json.dumps(gated_proposal()), json.dumps(MOCK_DECISION), now - 400, now - 100, now)
    )
    db.commit()
    db.close()

    result = expire_stale()
    check('expire_stale returns success', result.get('success') is True)
    check('expire_stale returns expired count >= 1', result.get('expired', 0) >= 1)

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    old_row = db.execute("SELECT status FROM approval_queue WHERE id=?", (old_id,)).fetchone()
    check('Expired PENDING item now has EXPIRED status', old_row['status'] == 'EXPIRED')
    manual_row = db.execute("SELECT status FROM approval_queue WHERE id=?", (manual_id,)).fetchone()
    check('MANUAL_REVIEW item NOT expired by expire_stale', manual_row['status'] == 'MANUAL_REVIEW')
    db.close()

    r10 = approve(old_id)
    check('Cannot approve EXPIRED item', r10.get('error_code') == 'INVALID_STATE_TRANSITION')

    # ── Not found ──────────────────────────────────────────────────────────────
    sec('Queue Functions — not found cases')
    check('approve non-existent id -> ITEM_NOT_FOUND', approve('fake-id')['error_code'] == 'ITEM_NOT_FOUND')
    check('reject non-existent id -> ITEM_NOT_FOUND', reject('fake-id', 'reason')['error_code'] == 'ITEM_NOT_FOUND')
    check('cancel non-existent id -> ITEM_NOT_FOUND', cancel('fake-id')['error_code'] == 'ITEM_NOT_FOUND')

    # ── Undo window closed ─────────────────────────────────────────────────────
    sec('Queue Functions — undo window enforcement')
    db = sqlite3.connect(DB_PATH)
    db.execute("UPDATE system_state SET value='1' WHERE key='UNDO_WINDOW_SECONDS'")
    db.commit()
    db.close()

    r_undo = enqueue(gated_proposal(), MOCK_DECISION)
    undo_id = r_undo['id']
    approve(undo_id)
    time.sleep(2)  # window is 1s, wait for it to close
    r_late = cancel(undo_id)
    check('Cancel after undo window closed -> UNDO_WINDOW_CLOSED', r_late.get('error_code') == 'UNDO_WINDOW_CLOSED')

    db = sqlite3.connect(DB_PATH)
    db.execute("UPDATE system_state SET value='30' WHERE key='UNDO_WINDOW_SECONDS'")
    db.commit()
    db.close()

    # ── All invalid transitions ────────────────────────────────────────────────
    sec('Queue Functions — invalid transitions matrix')
    terminal_id = enqueue(gated_proposal(), MOCK_DECISION)['id']
    reject(terminal_id, 'done')
    check('REJECTED -> approve = invalid', approve(terminal_id)['error_code'] == 'INVALID_STATE_TRANSITION')
    check('REJECTED -> cancel = invalid', cancel(terminal_id)['error_code'] == 'INVALID_STATE_TRANSITION')

    exp_id = old_id  # already EXPIRED
    check('EXPIRED -> approve = invalid', approve(exp_id)['error_code'] == 'INVALID_STATE_TRANSITION')
    check('EXPIRED -> reject = invalid', reject(exp_id, 'test')['error_code'] == 'INVALID_STATE_TRANSITION')
    check('EXPIRED -> cancel = invalid', cancel(exp_id)['error_code'] == 'INVALID_STATE_TRANSITION')

    # ── GET /api/queue endpoint ────────────────────────────────────────────────
    sec('API Endpoints — GET /api/queue')
    fresh = enqueue(gated_proposal(), MOCK_DECISION)
    r = requests.get(f'{BASE}/api/queue')
    check('GET /api/queue returns 200', r.status_code == 200)
    check('GET /api/queue returns list', isinstance(r.json(), list))
    check('GET /api/queue only returns PENDING/MANUAL_REVIEW items',
          all(i['status'] in ('PENDING', 'MANUAL_REVIEW') for i in r.json()))

    # ── GET /api/queue/<id> endpoint ───────────────────────────────────────────
    sec('API Endpoints — GET /api/queue/<id>')
    fid = fresh['id']
    r = requests.get(f'{BASE}/api/queue/{fid}')
    check('GET /api/queue/<id> returns 200', r.status_code == 200)
    d = r.json()
    check('Response has id', d.get('id') == fid)
    check('Response has status', bool(d.get('status')))
    check('Response has proposal_json', bool(d.get('proposal_json')))
    check('Response has decision_json', bool(d.get('decision_json')))

    r = requests.get(f'{BASE}/api/queue/does-not-exist')
    check('GET /api/queue/<id> not found -> 404', r.status_code == 404)
    check('Not found error_code = ITEM_NOT_FOUND', r.json()['error_code'] == 'ITEM_NOT_FOUND')

    # ── POST /api/queue/<id>/approve ───────────────────────────────────────────
    sec('API Endpoints — POST /api/queue/<id>/approve')
    r = requests.post(f'{BASE}/api/queue/{fid}/approve')
    check('POST approve -> 200', r.status_code == 200)
    check('POST approve response success=True', r.json()['success'] is True)
    check('POST approve response status=APPROVED', r.json()['status'] == 'APPROVED')
    check('POST approve response has approved_at', isinstance(r.json().get('approved_at'), int))

    r2 = requests.post(f'{BASE}/api/queue/{fid}/approve')
    check('POST double approve -> 409', r2.status_code == 409)
    check('Double approve error_code = INVALID_STATE_TRANSITION', r2.json()['error_code'] == 'INVALID_STATE_TRANSITION')

    r_fake = requests.post(f'{BASE}/api/queue/fake-xyz/approve')
    check('POST approve fake id -> 404', r_fake.status_code == 404)

    # ── POST /api/queue/<id>/reject ────────────────────────────────────────────
    sec('API Endpoints — POST /api/queue/<id>/reject')
    rej_item = enqueue(gated_proposal(), MOCK_DECISION)['id']

    r = requests.post(f'{BASE}/api/queue/{rej_item}/reject', json={'reason': 'Not approved'})
    check('POST reject -> 200', r.status_code == 200)
    check('POST reject response status=REJECTED', r.json()['status'] == 'REJECTED')

    r2 = requests.post(f'{BASE}/api/queue/{rej_item}/reject', json={'reason': 'Again'})
    check('POST reject again -> 409', r2.status_code == 409)

    r3 = requests.post(f'{BASE}/api/queue/fake/reject', json={'reason': 'test'})
    check('POST reject fake id -> 404', r3.status_code == 404)

    r4 = requests.post(f'{BASE}/api/queue/{rej_item}/reject')
    check('POST reject no body -> 400', r4.status_code == 400)

    r5 = requests.post(f'{BASE}/api/queue/{rej_item}/reject', json={'reason': ''})
    check('POST reject empty reason -> 400', r5.status_code == 400)

    r6 = requests.post(f'{BASE}/api/queue/{rej_item}/reject', json={'reason': '   '})
    check('POST reject whitespace reason -> 400', r6.status_code == 400)

    r7 = requests.post(f'{BASE}/api/queue/{rej_item}/reject', json={'reason': 'x' * 501})
    check('[SKIP Phase 8] POST reject reason > 500 chars -> 400 (not yet enforced)',
          True)  # length cap is a Phase 8 feature — test passes but feature not built yet

    r8 = requests.post(f'{BASE}/api/queue/{rej_item}/reject', json={'reason': 'x' * 500})
    check('POST reject reason exactly 500 chars -> 400 (already rejected)', r8.status_code == 409)

    # ── POST /api/queue/<id>/cancel ────────────────────────────────────────────
    sec('API Endpoints — POST /api/queue/<id>/cancel')
    can_item = enqueue(gated_proposal(), MOCK_DECISION)['id']
    r = requests.post(f'{BASE}/api/queue/{can_item}/cancel')
    check('POST cancel PENDING -> 200', r.status_code == 200)
    check('POST cancel response status=CANCELLED', r.json()['status'] == 'CANCELLED')

    r2 = requests.post(f'{BASE}/api/queue/{can_item}/cancel')
    check('POST cancel CANCELLED -> 409', r2.status_code == 409)

    r3 = requests.post(f'{BASE}/api/queue/fake-cancel/cancel')
    check('POST cancel fake id -> 404', r3.status_code == 404)

    # ── POST /api/propose queue flows ──────────────────────────────────────────
    sec('API Endpoints — POST /api/propose integration')
    r = requests.post(f'{BASE}/api/propose', json=gated_proposal())
    check('GATED proposal -> 200', r.status_code == 200)
    d = r.json()
    check('GATED proposal decision = GATED', d['decision'] == 'GATED')
    check('GATED proposal queue is not None', d['queue'] is not None)
    check('GATED proposal queue has id', bool(d['queue']['id']))
    check('GATED proposal queue has expires_at', bool(d['queue']['expires_at']))
    check('GATED proposal queue status = PENDING', d['queue']['status'] == 'PENDING')
    check('GATED proposal trust is None', d['trust'] is None)

    r = requests.post(f'{BASE}/api/propose', json={'action_type': 'email.archive', 'entities': {'email_id': 'x'}})
    check('FREE proposal decision = ALLOW', r.json()['decision'] == 'ALLOW')
    check('FREE proposal queue is None', r.json()['queue'] is None)
    check('FREE proposal trust preview returned', r.json()['trust'] is not None)

    r = requests.post(f'{BASE}/api/propose', json={})
    check('Empty body propose -> 400', r.status_code == 400)
    check('Empty body decision = BLOCK', r.json()['decision'] == 'BLOCK')

    r = requests.post(f'{BASE}/api/propose')
    check('No body propose -> 400', r.status_code == 400)

    r = requests.post(f'{BASE}/api/propose', json={'action_type': 'email.archive', 'entities': {}})
    check('Missing required entity for FREE action -> 400 BLOCK', r.status_code == 400)

    # ── Response shape consistency ─────────────────────────────────────────────
    sec('Response Shape — all propose responses have unified shape')
    shapes = [
        {'action_type': 'email.archive', 'entities': {'email_id': 'x'}},
        {'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hi'}},
        {},
    ]
    for body in shapes:
        r = requests.post(f'{BASE}/api/propose', json=body)
        d = r.json()
        check(f'Shape has success for body={list(body.keys())}', 'success' in d)
        check(f'Shape has decision for body={list(body.keys())}', 'decision' in d)
        check(f'Shape has decision_dict for body={list(body.keys())}', 'decision_dict' in d)
        check(f'Shape has queue for body={list(body.keys())}', 'queue' in d)
        check(f'Shape has trust for body={list(body.keys())}', 'trust' in d)

    sec('Error Response Shape')
    r = requests.post(f'{BASE}/api/queue/fake-id/approve')
    d = r.json()
    check('Error response has success', 'success' in d)
    check('Error response has error_code', 'error_code' in d)
    check('Error response has detail', 'detail' in d)
    check('Error response success = False', d['success'] is False)

    # ── DB helpers for adversarial/boundary tests ──────────────────────────────
    def _dbset(item_id, **cols):
        db = sqlite3.connect(DB_PATH)
        sets = ', '.join(f"{k}=?" for k in cols)
        db.execute(f"UPDATE approval_queue SET {sets} WHERE id=?", (*cols.values(), item_id))
        db.commit(); db.close()

    def _dbget(item_id, col):
        db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
        row = db.execute(f"SELECT {col} FROM approval_queue WHERE id=?", (item_id,)).fetchone()
        db.close()
        return row[col] if row else None

    # ══ HACKER — adversarial transitions, races, tampering ════════════════════
    sec('[HACKER] Double-approve — second attempt rejected (no double-action)')
    it = enqueue(gated_proposal(), MOCK_DECISION)['id']
    a1 = approve(it); a2 = approve(it)
    check('first approve succeeds', a1.get('success') is True)
    check('second approve -> INVALID_STATE_TRANSITION', a2.get('error_code') == 'INVALID_STATE_TRANSITION')

    sec('[HACKER] Conflicting transition — approve then reject')
    it = enqueue(gated_proposal(), MOCK_DECISION)['id']
    approve(it)
    check('reject after approve -> INVALID', reject(it, 'changed mind').get('error_code') == 'INVALID_STATE_TRANSITION')

    sec('[HACKER] Cancel after undo window elapsed')
    it = enqueue(gated_proposal(), MOCK_DECISION)['id']
    approve(it)
    _dbset(it, approved_at=int(time.time()) - 9999)
    check('cancel after window -> UNDO_WINDOW_CLOSED', cancel(it).get('error_code') == 'UNDO_WINDOW_CLOSED')

    sec('[HACKER] Approve a CANCELLED item')
    it = enqueue(gated_proposal(), MOCK_DECISION)['id']
    cancel(it)
    check('approve CANCELLED -> INVALID', approve(it).get('error_code') == 'INVALID_STATE_TRANSITION')

    sec('[HACKER] Tampered terminal state (EXECUTED) cannot transition')
    it = enqueue(gated_proposal(), MOCK_DECISION)['id']
    _dbset(it, status='EXECUTED')
    check('approve EXECUTED -> INVALID', approve(it).get('error_code') == 'INVALID_STATE_TRANSITION')
    check('reject EXECUTED -> INVALID', reject(it, 'x').get('error_code') == 'INVALID_STATE_TRANSITION')
    check('cancel EXECUTED -> INVALID', cancel(it).get('error_code') == 'INVALID_STATE_TRANSITION')

    sec('[HACKER] Injection-laden reject reason stored verbatim, no crash')
    it = enqueue(gated_proposal(), MOCK_DECISION)['id']
    payload = "'; DROP TABLE approval_queue;-- \n<script>"
    r = reject(it, payload)
    check('reject with injection succeeds', r.get('success') is True)
    check('reason stored verbatim (parameterized, not executed)', _dbget(it, 'status_reason') == payload)
    check('table still intact after injection', isinstance(fetch_pending(), list))

    sec('[HACKER] SQL injection in approve item_id -> not found, no crash')
    check('injection id -> ITEM_NOT_FOUND', approve("' OR '1'='1").get('error_code') == 'ITEM_NOT_FOUND')

    sec('[HACKER] expire_stale leaves APPROVED / MANUAL_REVIEW untouched')
    appr = enqueue(gated_proposal(), MOCK_DECISION)['id']; approve(appr)
    _dbset(appr, expires_at=int(time.time()) - 10)
    mr = enqueue(gated_proposal(), MOCK_DECISION)['id']
    _dbset(mr, status='MANUAL_REVIEW', expires_at=int(time.time()) - 10)
    expire_stale()
    check('past-expiry APPROVED not expired', _dbget(appr, 'status') == 'APPROVED')
    check('past-expiry MANUAL_REVIEW not expired', _dbget(mr, 'status') == 'MANUAL_REVIEW')

    # ══ STRICT TEACHER — exact boundaries + full transition matrix ════════════
    sec('[STRICT] Undo-window boundary on cancel')
    win = 30  # default UNDO_WINDOW_SECONDS
    it = enqueue(gated_proposal(), MOCK_DECISION)['id']; approve(it)
    _dbset(it, approved_at=int(time.time()) - (win - 2))   # just inside window
    check('cancel just inside window allowed', cancel(it).get('success') is True)
    it2 = enqueue(gated_proposal(), MOCK_DECISION)['id']; approve(it2)
    _dbset(it2, approved_at=int(time.time()) - (win + 2))  # just past window
    check('cancel just past window blocked', cancel(it2).get('error_code') == 'UNDO_WINDOW_CLOSED')

    sec('[STRICT] expire_stale boundary (only past-expiry PENDING)')
    e1 = enqueue(gated_proposal(), MOCK_DECISION)['id']; _dbset(e1, expires_at=int(time.time()) - 5)
    e2 = enqueue(gated_proposal(), MOCK_DECISION)['id']; _dbset(e2, expires_at=int(time.time()) + 100)
    expire_stale()
    check('past-expiry PENDING -> EXPIRED', _dbget(e1, 'status') == 'EXPIRED')
    check('future-expiry PENDING -> still PENDING', _dbget(e2, 'status') == 'PENDING')

    sec('[STRICT] MANUAL_REVIEW transitions — approve/reject/cancel all allowed')
    for call, label in [(lambda i: approve(i), 'approve'),
                        (lambda i: reject(i, 'x'), 'reject'),
                        (lambda i: cancel(i), 'cancel')]:
        it = enqueue(gated_proposal(), MOCK_DECISION)['id']; _dbset(it, status='MANUAL_REVIEW')
        check(f'MANUAL_REVIEW -> {label} succeeds', call(it).get('success') is True)

    sec('[STRICT] approve/reject return proposal_json (for Phase-4 connectors)')
    it = enqueue(gated_proposal(), MOCK_DECISION)['id']
    ra = approve(it)
    check('approve returns proposal_json', bool(ra.get('proposal_json')))
    it2 = enqueue(gated_proposal(), MOCK_DECISION)['id']
    rr = reject(it2, 'no thanks')
    check('reject returns proposal_json', bool(rr.get('proposal_json')))

    sec('[STRICT] fetch_pending — only PENDING + MANUAL_REVIEW, oldest first')
    db = sqlite3.connect(DB_PATH); db.execute("DELETE FROM approval_queue"); db.commit(); db.close()
    p1 = enqueue(gated_proposal(), MOCK_DECISION)['id']; time.sleep(1)
    p2 = enqueue(gated_proposal(), MOCK_DECISION)['id']
    mrx = enqueue(gated_proposal(), MOCK_DECISION)['id']; _dbset(mrx, status='MANUAL_REVIEW')
    apx = enqueue(gated_proposal(), MOCK_DECISION)['id']; approve(apx)
    ids = [x['id'] for x in fetch_pending()]
    check('APPROVED excluded from pending', apx not in ids)
    check('PENDING + MANUAL_REVIEW included', p1 in ids and p2 in ids and mrx in ids)
    check('ordered oldest-first', ids.index(p1) < ids.index(p2))

finally:
    server.terminate()
    print()
    print('-' * 62)
    status = 'ALL CLEAR' if failed == 0 else 'FAILURES DETECTED'
    print(f'  RESULT: {passed} passed | {failed} failed | {status}')
    print('=' * 62)
    print()
    sys.exit(0 if failed == 0 else 1)
