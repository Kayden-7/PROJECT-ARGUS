import requests, time, sqlite3, os, subprocess, sys

BASE = 'http://127.0.0.1:8081'
DATABASE = os.path.join(os.path.dirname(__file__), 'instance', 'argus.db')

passed = 0
failed = 0

def check(name, condition, got=None):
    global passed, failed
    if condition:
        print('  PASS  ' + name)
        passed += 1
    else:
        print('  FAIL  ' + name + (' | got: ' + str(got) if got is not None else ''))
        failed += 1

server = subprocess.Popen([sys.executable, 'app.py'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3)

from argus.db import init_db
init_db()
from argus.kernel import kernel_entry, set_hard_stop
from argus.queue import enqueue, fetch_pending, approve, reject, cancel, expire_stale
from argus.trust_ledger import record_event, get_trust

try:
    print()
    print('=== PHASE 1: Flask skeleton ===')
    r = requests.get(f'{BASE}/health')
    check('Health check returns 200', r.status_code == 200)
    check('Health check body correct', r.json() == {'status': 'ok', 'system': 'ARGUS', 'version': '1.0'})

    print()
    print('=== PHASE 2A: Validation ===')

    r = kernel_entry({})
    check('Missing action_type -> BLOCK', r['decision'] == 'BLOCK')

    r = kernel_entry({'action_type': 'email.nuke', 'entities': {}})
    check('Unknown action -> BLOCK UNKNOWN_ACTION_TYPE', r['decision'] == 'BLOCK' and 'UNKNOWN_ACTION_TYPE' in r['failure_reason_code'])

    r = kernel_entry({'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com', 'subject': 'hi'}})
    check('Missing required field -> BLOCK MISSING_REQUIRED_FIELD', r['decision'] == 'BLOCK' and r['failure_reason_code'] == 'MISSING_REQUIRED_FIELD')

    r = kernel_entry({'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com', 'subject': '   ', 'body': 'hello'}})
    check('Whitespace field -> BLOCK EMPTY_REQUIRED_FIELD', r['decision'] == 'BLOCK' and r['failure_reason_code'] == 'EMPTY_REQUIRED_FIELD')

    r = kernel_entry({'action_type': 'email.send.external', 'entities': {'recipient': 123, 'subject': 'hi', 'body': 'hello'}})
    check('Wrong type field -> BLOCK INVALID_FIELD_TYPE', r['decision'] == 'BLOCK' and r['failure_reason_code'] == 'INVALID_FIELD_TYPE')

    r = kernel_entry({'action_type': 'email.archive', 'entities': {'email_id': 'msg-1'}, 'hacked_field': 'bad'})
    check('Extra fields stripped - still processes', r['decision'] == 'ALLOW')

    r = kernel_entry({'action_type': 'email.archive', 'entities': {'email_id': 'msg-1'}, 'action_expiry': 99999})
    check('Invalid expiry -> BLOCK INVALID_ACTION_EXPIRY', r['decision'] == 'BLOCK' and r['failure_reason_code'] == 'INVALID_ACTION_EXPIRY')

    r = kernel_entry({'action_type': 'email.archive', 'entities': {'email_id': 'msg-1'}, 'action_expiry': 'soon'})
    check('String expiry -> BLOCK', r['decision'] == 'BLOCK')

    print()
    print('=== PHASE 2B: Policy engine - FREE actions ===')

    free_cases = [
        ('email.archive',   {'email_id': 'x'}),
        ('email.mark_read', {'email_id': 'x'}),
        ('email.star',      {'email_id': 'x'}),
        ('email.move',      {'email_id': 'x', 'destination': 'inbox'}),
        ('email.compose',   {'subject': 'hi', 'body': 'hello'}),
        ('calendar.accept', {'event_id': 'ev1'}),
        ('calendar.label',  {'event_id': 'ev1', 'label': 'work'}),
        ('calendar.color',  {'event_id': 'ev1', 'color': 'blue'}),
        ('label.apply',     {'email_id': 'x', 'label': 'urgent'}),
    ]
    for action, entities in free_cases:
        r = kernel_entry({'action_type': action, 'entities': entities})
        check('FREE ' + action + ' -> ALLOW', r['decision'] == 'ALLOW' and r['decision_source'] == 'FREE_ACTION')

    print()
    print('=== PHASE 2C: Policy engine - GATED actions (Balanced, trust=40, threshold=70) ===')

    gated_cases = [
        ('email.send.external', {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}),
        ('email.send.internal', {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}),
        ('email.reply',         {'recipient': 'a@b.com', 'body': 'hello'}),
        ('email.forward',       {'recipient': 'a@b.com'}),
        ('email.delete',        {'email_id': 'x'}),
        ('calendar.create',     {'title': 'Meeting', 'start_time': '9am', 'end_time': '10am'}),
        ('calendar.modify',     {'event_id': 'ev1'}),
        ('calendar.delete',     {'event_id': 'ev1'}),
        ('calendar.reschedule', {'event_id': 'ev1', 'start_time': '9am', 'end_time': '10am'}),
        ('calendar.invite',     {'event_id': 'ev1', 'recipient': 'a@b.com'}),
        ('calendar.decline',    {'event_id': 'ev1'}),
    ]
    for action, entities in gated_cases:
        r = kernel_entry({'action_type': action, 'entities': entities})
        check('GATED ' + action + ' -> GATED', r['decision'] == 'GATED' and r['trust_at_evaluation'] == 40.0)

    print()
    print('=== PHASE 2D: Policy engine - special cases ===')

    set_hard_stop(True)
    r = kernel_entry({'action_type': 'email.archive', 'entities': {'email_id': 'x'}})
    check('Hard stop blocks FREE action', r['decision'] == 'BLOCK' and r['failure_reason_code'] == 'SYSTEM_HARD_STOP')
    r = kernel_entry({'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}})
    check('Hard stop blocks GATED action', r['decision'] == 'BLOCK' and r['failure_reason_code'] == 'SYSTEM_HARD_STOP')
    set_hard_stop(False)

    db = sqlite3.connect(DATABASE)
    db.execute("INSERT OR IGNORE INTO prime_rules VALUES (99, 'email.delete', '{}', 'Never delete')")
    db.commit()
    db.close()
    r = kernel_entry({'action_type': 'email.delete', 'entities': {'email_id': 'x'}})
    check('Prime rule -> BLOCK PRIME_RULE_MATCH', r['decision'] == 'BLOCK' and r['failure_reason_code'] == 'PRIME_RULE_MATCH')
    db = sqlite3.connect(DATABASE)
    db.execute('DELETE FROM prime_rules WHERE id=99')
    db.commit()
    db.close()

    db = sqlite3.connect(DATABASE)
    db.execute("UPDATE system_state SET value='Autonomous' WHERE key='ACTIVE_PROFILE'")
    db.commit()
    db.close()
    r = kernel_entry({'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}})
    check('Autonomous profile: trust 40 >= threshold 40 -> ALLOW', r['decision'] == 'ALLOW')
    db = sqlite3.connect(DATABASE)
    db.execute("UPDATE system_state SET value='Balanced' WHERE key='ACTIVE_PROFILE'")
    db.commit()
    db.close()

    db = sqlite3.connect(DATABASE)
    db.execute("UPDATE system_state SET value='Strict' WHERE key='ACTIVE_PROFILE'")
    db.commit()
    db.close()
    r = kernel_entry({'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}})
    check('Strict profile: trust 40 < threshold 101 -> GATED', r['decision'] == 'GATED')
    db = sqlite3.connect(DATABASE)
    db.execute("UPDATE system_state SET value='Balanced' WHERE key='ACTIVE_PROFILE'")
    db.commit()
    db.close()

    r = kernel_entry({'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}, 'importance': 'high'})
    check('High importance -> severity bumped to HIGH', r['decision'] == 'GATED' and r['modifier_breakdown'].get('severity') == 'HIGH')

    r = kernel_entry({'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}})
    for field in ['decision', 'decision_source', 'failure_type', 'failure_reason_code', 'trace', 'trust_at_evaluation', 'effective_threshold', 'narrative', 'modifier_breakdown']:
        check('Decision output has field: ' + field, field in r)
    check('Trace is non-empty list', isinstance(r['trace'], list) and len(r['trace']) > 0)
    check('Narrative is non-empty string', isinstance(r['narrative'], str) and len(r['narrative']) > 0)

    print()
    print('=== PHASE 3A: queue.py core ===')

    proposal = {'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}}
    decision = {'decision': 'GATED', 'action_expiry': 300}

    r = enqueue(proposal, decision)
    check('Enqueue returns success + id + expires_at', r['success'] and 'id' in r and 'expires_at' in r)
    item_id = r['id']

    items = fetch_pending()
    item = next((i for i in items if i['id'] == item_id), None)
    for field in ['id', 'status', 'created_at', 'expires_at', 'approved_at', 'updated_at', 'status_reason', 'execution_id', 'proposal_json', 'decision_json']:
        check('Queue schema field present: ' + field, item and field in item)

    check('Fetch pending ordered oldest first', items == sorted(items, key=lambda x: x['created_at']))

    r = approve(item_id)
    check('Approve -> APPROVED', r['success'] and r['status'] == 'APPROVED')
    check('Approve sets approved_at', r.get('approved_at') is not None)

    r = approve(item_id)
    check('Double approve -> INVALID_STATE_TRANSITION', r['error_code'] == 'INVALID_STATE_TRANSITION' and r['current_state'] == 'APPROVED')

    r = cancel(item_id)
    check('Cancel within undo window -> CANCELLED', r['success'] and r['status'] == 'CANCELLED')

    r = cancel(item_id)
    check('Cancel already cancelled -> INVALID_STATE_TRANSITION', r['error_code'] == 'INVALID_STATE_TRANSITION')

    r2 = enqueue(proposal, decision)
    item2 = r2['id']
    r = reject(item2, 'Wrong recipient')
    check('Reject -> REJECTED', r['success'] and r['status'] == 'REJECTED')

    r = reject(item2, 'Again')
    check('Double reject -> INVALID_STATE_TRANSITION', r['error_code'] == 'INVALID_STATE_TRANSITION')

    r = approve(item2)
    check('Approve after reject -> INVALID_STATE_TRANSITION', r['error_code'] == 'INVALID_STATE_TRANSITION')

    check('Approve non-existent -> ITEM_NOT_FOUND', approve('no-such-id')['error_code'] == 'ITEM_NOT_FOUND')
    check('Reject non-existent -> ITEM_NOT_FOUND', reject('no-such-id', 'x')['error_code'] == 'ITEM_NOT_FOUND')
    check('Cancel non-existent -> ITEM_NOT_FOUND', cancel('no-such-id')['error_code'] == 'ITEM_NOT_FOUND')

    r3 = enqueue(proposal, decision)
    item3 = r3['id']
    approve(item3)
    db = sqlite3.connect(DATABASE)
    db.execute('UPDATE approval_queue SET approved_at=? WHERE id=?', (int(time.time()) - 999, item3))
    db.commit()
    db.close()
    r = cancel(item3)
    check('Cancel after undo window elapsed -> UNDO_WINDOW_CLOSED', r['error_code'] == 'UNDO_WINDOW_CLOSED')

    r4 = enqueue(proposal, decision)
    item4 = r4['id']
    r = cancel(item4)
    check('Cancel PENDING (before approval) -> CANCELLED', r['success'] and r['status'] == 'CANCELLED')

    r5 = enqueue(proposal, {'decision': 'GATED', 'action_expiry': 1})
    time.sleep(2)
    r = expire_stale()
    check('Expire stale -> expired >= 1', r['success'] and r['expired'] >= 1)

    r6 = enqueue(proposal, decision)
    item6 = r6['id']
    db = sqlite3.connect(DATABASE)
    db.execute("UPDATE approval_queue SET status='MANUAL_REVIEW', expires_at=? WHERE id=?", (int(time.time()) - 999, item6))
    db.commit()
    db.close()
    expire_stale()
    db = sqlite3.connect(DATABASE)
    row = db.execute('SELECT status FROM approval_queue WHERE id=?', (item6,)).fetchone()
    db.close()
    check('Expire stale does NOT touch MANUAL_REVIEW', row[0] == 'MANUAL_REVIEW')

    r7 = enqueue(proposal, decision)
    item7 = r7['id']
    db = sqlite3.connect(DATABASE)
    db.execute("UPDATE approval_queue SET status='MANUAL_REVIEW' WHERE id=?", (item7,))
    db.commit()
    db.close()
    r = approve(item7)
    check('MANUAL_REVIEW -> APPROVED', r['success'] and r['status'] == 'APPROVED')

    r8 = enqueue(proposal, decision)
    item8 = r8['id']
    db = sqlite3.connect(DATABASE)
    db.execute("UPDATE approval_queue SET status='MANUAL_REVIEW' WHERE id=?", (item8,))
    db.commit()
    db.close()
    r = reject(item8, 'Needs more context')
    check('MANUAL_REVIEW -> REJECTED', r['success'] and r['status'] == 'REJECTED')

    r9 = enqueue(proposal, decision)
    item9 = r9['id']
    db = sqlite3.connect(DATABASE)
    db.execute("UPDATE approval_queue SET status='MANUAL_REVIEW' WHERE id=?", (item9,))
    db.commit()
    db.close()
    r = cancel(item9)
    check('MANUAL_REVIEW -> CANCELLED', r['success'] and r['status'] == 'CANCELLED')

    print()
    print('=== PHASE 3B: Endpoints ===')

    r = requests.post(f'{BASE}/api/propose', json={'action_type': 'email.archive', 'entities': {'email_id': 'msg-1'}})
    d = r.json()
    check('POST /api/propose FREE -> 200 ALLOW', r.status_code == 200 and d['decision'] == 'ALLOW')
    check('FREE trust stub has delta_preview', d['trust'] and '+' in d['trust']['delta_preview'])
    check('FREE queue is null', d['queue'] is None)

    r = requests.post(f'{BASE}/api/propose', json={'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}})
    d = r.json()
    check('POST /api/propose GATED -> 200 GATED', r.status_code == 200 and d['decision'] == 'GATED')
    check('GATED has queue id + expires_at + status PENDING', d['queue'] and d['queue']['status'] == 'PENDING' and 'expires_at' in d['queue'])
    check('GATED trust is null', d['trust'] is None)
    gated_id = d['queue']['id']

    r = requests.post(f'{BASE}/api/propose', json={'action_type': 'email.unknown', 'entities': {}})
    d = r.json()
    check('POST /api/propose validation BLOCK -> 400', r.status_code == 400 and d['decision'] == 'BLOCK')

    r = requests.post(f'{BASE}/api/propose', json={'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com'}})
    check('POST /api/propose missing field -> 400', r.status_code == 400)

    r = requests.post(f'{BASE}/api/propose')
    check('POST /api/propose no body -> 400', r.status_code == 400)

    set_hard_stop(True)
    r = requests.post(f'{BASE}/api/propose', json={'action_type': 'email.archive', 'entities': {'email_id': 'x'}})
    d = r.json()
    check('POST /api/propose hard stop BLOCK -> 200', r.status_code == 200 and d['decision'] == 'BLOCK')
    check('Hard stop BLOCK queue is null', d['queue'] is None)
    set_hard_stop(False)

    for dec, body in [
        ('ALLOW', {'action_type': 'email.archive', 'entities': {'email_id': 'x'}}),
        ('GATED', {'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}}),
    ]:
        r = requests.post(f'{BASE}/api/propose', json=body)
        d = r.json()
        check('Unified response shape for ' + dec, all(k in d for k in ['success', 'decision', 'decision_dict', 'queue', 'trust']))

    r = requests.get(f'{BASE}/api/queue')
    check('GET /api/queue -> 200 list', r.status_code == 200 and isinstance(r.json(), list))

    r = requests.get(f'{BASE}/api/queue/{gated_id}')
    d = r.json()
    check('GET /api/queue/<id> -> 200', r.status_code == 200)
    check('GET /api/queue/<id> has all schema fields', all(f in d for f in ['id', 'status', 'created_at', 'expires_at', 'proposal_json', 'decision_json', 'approved_at', 'updated_at']))
    check('GET /api/queue/<id> status is PENDING', d.get('status') == 'PENDING')

    r = requests.get(f'{BASE}/api/queue/does-not-exist')
    check('GET /api/queue/<id> not found -> 404', r.status_code == 404 and r.json()['error_code'] == 'ITEM_NOT_FOUND')

    r = requests.post(f'{BASE}/api/queue/{gated_id}/approve')
    check('POST approve -> 200 APPROVED', r.status_code == 200 and r.json()['status'] == 'APPROVED')

    r = requests.post(f'{BASE}/api/queue/{gated_id}/approve')
    check('POST double approve -> 409', r.status_code == 409 and r.json()['error_code'] == 'INVALID_STATE_TRANSITION')

    r = requests.post(f'{BASE}/api/queue/{gated_id}/cancel')
    check('POST cancel undo -> 200 CANCELLED', r.status_code == 200 and r.json()['status'] == 'CANCELLED')

    new_id = requests.post(f'{BASE}/api/propose', json={'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}}).json()['queue']['id']
    r = requests.post(f'{BASE}/api/queue/{new_id}/reject', json={'reason': 'Not appropriate'})
    check('POST reject -> 200 REJECTED', r.status_code == 200 and r.json()['status'] == 'REJECTED')

    r = requests.post(f'{BASE}/api/queue/{new_id}/reject', json={'reason': 'Again'})
    check('POST reject already rejected -> 409', r.status_code == 409)

    new_id2 = requests.post(f'{BASE}/api/propose', json={'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}}).json()['queue']['id']
    r = requests.post(f'{BASE}/api/queue/{new_id2}/reject', json={'reason': '   '})
    check('POST reject whitespace reason -> 400 MISSING_REASON', r.status_code == 400 and r.json()['error_code'] == 'MISSING_REASON')

    r = requests.post(f'{BASE}/api/queue/{new_id2}/reject')
    check('POST reject no body -> 400', r.status_code == 400)

    r = requests.post(f'{BASE}/api/queue/fake-xyz/approve')
    check('POST approve fake id -> 404', r.status_code == 404)

    r = requests.post(f'{BASE}/api/queue/fake-xyz/reject', json={'reason': 'test'})
    d = r.json()
    check('Error response shape has success + error_code + detail', all(k in d for k in ['success', 'error_code', 'detail']))

    # ── PHASE 4: Trust ledger ──────────────────────────────────────────────────
    print()
    print('=== PHASE 4A: record_event - basic writes ===')

    # email.archive = TRIVIAL FREE, starting trust = 40.0
    r = record_event('email.archive', 'SUCCESS', 'TRIVIAL')
    check('record_event SUCCESS returns success=True', r.get('success') is True)
    check('record_event has event_id', bool(r.get('event_id')))
    check('record_event trust_after > trust_before on SUCCESS', r['trust_after'] > r['trust_before'])
    check('record_event trust_before is 40.0 at cold start', r['trust_before'] == 40.0)
    check('record_event inertia_active on first event', r['inertia_active'] is True)
    check('record_event damping_active is False on first event', r['damping_active'] is False)
    check('record_event actual_delta is positive on SUCCESS', r['actual_delta'] > 0)

    # TRIVIAL SUCCESS with inertia: base=+0.5, weight=0.5, modifier=1.0 -> delta=0.25
    check('record_event TRIVIAL SUCCESS inertia delta = 0.25', round(r['actual_delta'], 4) == 0.25)

    # email.delete = HIGH GATED
    r_fail = record_event('email.delete', 'FAILURE', 'HIGH')
    check('record_event FAILURE trust_after < trust_before', r_fail['trust_after'] < r_fail['trust_before'])
    check('record_event HIGH FAILURE actual_delta is negative', r_fail['actual_delta'] < 0)
    check('record_event HIGH FAILURE activates damping', r_fail['damping_active'] is True)
    check('record_event HIGH FAILURE damping_remaining = 10', r_fail['damping_remaining'] == 10)
    check('record_event FAILURE returns success=True', r_fail.get('success') is True)

    print()
    print('=== PHASE 4B: Inertia behaviour ===')

    # Use email.star (TRIVIAL) — fresh action type, 0 events so far
    # Run 4 more events to get to event count = 5 (inertia threshold)
    results = [record_event('email.star', 'SUCCESS', 'TRIVIAL') for _ in range(5)]
    check('All 5 inertia events return success', all(r['success'] for r in results))
    check('Events 1-5 are all inertia_active=True', all(r['inertia_active'] for r in results))

    # 6th event should have inertia_active=False
    r6 = record_event('email.star', 'SUCCESS', 'TRIVIAL')
    check('Event 6 inertia_active=False', r6['inertia_active'] is False)
    # Without inertia: base=+0.5, weight=1.0
    check('Event 6 TRIVIAL SUCCESS delta > 0.25 (no inertia)', r6['actual_delta'] > 0.25)

    print()
    print('=== PHASE 4C: Damping behaviour ===')

    # email.delete already has HIGH failure above -> damping_remaining=10
    # Next SUCCESS during damping: effective_weight = min(inertia, 0.5)
    r_damp = record_event('email.delete', 'SUCCESS', 'HIGH')
    check('SUCCESS during damping returns success', r_damp['success'] is True)
    check('SUCCESS during damping still damping_active', r_damp['damping_active'] is True)
    check('SUCCESS during damping remaining decremented', r_damp['damping_remaining'] == 9)
    check('SUCCESS during damping trust increases', r_damp['trust_after'] > r_damp['trust_before'])
    # With damping: inertia_weight=0.5 (email.delete has 1 event), damping=0.5, min=0.5
    # TRIVIAL/LOW/MEDIUM/HIGH SUCCESS base=+10, effective=min(0.5,0.5)=0.5, delta=10*0.5*modifier
    # We just check it's less than the undampened full delta (10.0)
    check('SUCCESS during damping delta < undamped HIGH delta', r_damp['actual_delta'] < 10.0)

    # Inertia + damping: more restrictive wins, NOT multiplicative
    # Both at 0.5 -> result should be 0.5x not 0.25x
    # email.delete has 2 events now (still inertia active, threshold=5)
    # effective_weight = min(0.5, 0.5) = 0.5 -> delta = 10 * 0.5 * modifier
    # If multiplicative it would be 10 * 0.25 * modifier -> smaller
    r_check = record_event('email.delete', 'SUCCESS', 'HIGH')
    full_undamped_no_inertia = 10.0  # base HIGH success
    check('Inertia+damping not multiplicative (delta > 0.25x base)', r_check['actual_delta'] > full_undamped_no_inertia * 0.25)

    print()
    print('=== PHASE 4D: Damping exit via stability ===')

    # Use calendar.delete (HIGH) — fresh, no history
    # Trigger damping first
    record_event('calendar.delete', 'FAILURE', 'HIGH')

    # Now run 5 consecutive successes to exit damping early
    for _ in range(5):
        record_event('calendar.delete', 'SUCCESS', 'HIGH')

    r_exit = record_event('calendar.delete', 'SUCCESS', 'HIGH')
    check('After 5 consecutive successes damping exits', r_exit['damping_active'] is False)
    check('After damping exit damping_remaining = 0', r_exit['damping_remaining'] == 0)

    print()
    print('=== PHASE 4E: Second HIGH failure extends damping ===')

    # calendar.modify (HIGH) — fresh
    record_event('calendar.modify', 'FAILURE', 'HIGH')  # activates damping, remaining=10

    # Partial recovery: 3 successes (streak=3, remaining=7)
    for _ in range(3):
        record_event('calendar.modify', 'SUCCESS', 'HIGH')

    r_before_second = record_event('calendar.modify', 'FAILURE', 'HIGH')  # second HIGH fail
    check('Second HIGH failure resets damping_remaining to 10', r_before_second['damping_remaining'] == 10)
    check('Second HIGH failure resets damping_streak to 0', r_before_second['damping_streak'] == 0)

    print()
    print('=== PHASE 4F: Trust ceiling ===')

    # Balanced profile ceiling = 85.0
    # Push email.move (TRIVIAL FREE) high by running many SUCCESS events
    # Start at 40.0, keep running until we hit the ceiling
    for _ in range(200):
        record_event('email.move', 'SUCCESS', 'TRIVIAL')

    r_ceil = record_event('email.move', 'SUCCESS', 'TRIVIAL')
    check('Trust never exceeds Balanced ceiling of 85.0', r_ceil['trust_after'] <= 85.0)
    check('Trust ceiling enforced on record_event', r_ceil['profile_ceiling'] == 85.0)

    print()
    print('=== PHASE 4G: Policy gate block penalty ===')

    r_gate = record_event('email.forward', 'POLICY_GATE_BLOCK')
    check('POLICY_GATE_BLOCK returns success', r_gate['success'] is True)
    check('POLICY_GATE_BLOCK actual_delta is negative', r_gate['actual_delta'] < 0)
    check('POLICY_GATE_BLOCK does NOT activate damping (not HIGH failure)', r_gate['damping_active'] is False)
    check('POLICY_GATE_BLOCK does not affect overall modifier', r_gate['overall_modifier'] ==
          record_event('email.forward', 'POLICY_GATE_BLOCK')['overall_modifier'])

    print()
    print('=== PHASE 4H: get_trust ===')

    t = get_trust('email.archive')
    check('get_trust returns action_type', t.get('action_type') == 'email.archive')
    check('get_trust returns trust score', isinstance(t.get('trust'), float))
    check('get_trust returns label', bool(t.get('label')))
    check('get_trust returns description', bool(t.get('description')))
    check('get_trust returns event_count >= 1', t.get('event_count', 0) >= 1)
    check('get_trust returns inertia_active bool', isinstance(t.get('inertia_active'), bool))
    check('get_trust returns damping_active bool', isinstance(t.get('damping_active'), bool))
    check('get_trust returns ceiling', isinstance(t.get('ceiling'), float))

    # email.archive has been above ceiling push so trust should be above 40 (had 1 SUCCESS)
    check('get_trust trust > 40.0 after one SUCCESS', t['trust'] > 40.0)

    # Correct label for score just above 40 (Low Trust boundary)
    t_low = get_trust('email.forward')  # only POLICY_GATE_BLOCK events -> slightly below 40
    check('get_trust label is a known label', t_low['label'] in ['Untrusted', 'Low Trust', 'Developing', 'Trusted', 'Highly Reliable'])

    # Unknown action type returns safe defaults
    t_unknown = get_trust('email.nonexistent')
    check('get_trust unknown action returns STARTING_TRUST', t_unknown['trust'] == 40.0)
    check('get_trust unknown action event_count = 0', t_unknown['event_count'] == 0)

    print()
    print('=== PHASE 4I: Recency weighting (structural) ===')

    import sqlite3 as _sqlite3
    _db_path = os.path.join(os.path.dirname(__file__), 'instance', 'argus.db')
    _conn = _sqlite3.connect(_db_path)
    _conn.row_factory = _sqlite3.Row

    # Insert a synthetic old event (60 days ago) and a recent event for email.compose
    old_ts  = int(time.time()) - (60 * 86400)
    new_ts  = int(time.time())
    import uuid as _uuid
    _conn.execute(
        "INSERT OR IGNORE INTO trust_current (action_type, trust_current) VALUES ('email.compose', 40.0)"
    )
    _conn.execute(
        "INSERT INTO trust_events (event_id, timestamp, action_type, delta, reason, resulting_trust) VALUES (?, ?, 'email.compose', 10.0, 'test:old', 50.0)",
        (str(_uuid.uuid4()), old_ts)
    )
    _conn.execute(
        "INSERT INTO trust_events (event_id, timestamp, action_type, delta, reason, resulting_trust) VALUES (?, ?, 'email.compose', -5.0, 'test:recent', 45.0)",
        (str(_uuid.uuid4()), new_ts)
    )
    _conn.commit()
    _conn.close()

    t_recency = get_trust('email.compose')
    # Old +10 event at 0.1x weight = +1.0, recent -5 event at 1.0x = -5.0
    # Effective = 40 + 1.0 - 5.0 = 36.0
    # Raw would be 40 + 10 - 5 = 45.0
    check('Recency weighting: effective trust != raw trust when events are old', t_recency['trust'] != t_recency['raw_trust'])
    check('Recency weighting: old positive event discounted vs raw', t_recency['trust'] < t_recency['raw_trust'])
    check('Recency weighting: effective trust approx 36.0', abs(t_recency['trust'] - 36.0) < 1.0)

    print()
    print('=== PHASE 4J: Invalid outcome ===')

    r_bad = record_event('email.archive', 'EXPLODE', 'TRIVIAL')
    check('Invalid outcome returns success=False', r_bad['success'] is False)
    check('Invalid outcome returns INVALID_OUTCOME error_code', r_bad['error_code'] == 'INVALID_OUTCOME')

finally:
    server.terminate()
    print()
    print('=== RESULTS: ' + str(passed) + ' passed, ' + str(failed) + ' failed ===')
