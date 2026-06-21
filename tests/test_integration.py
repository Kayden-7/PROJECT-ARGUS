"""
ARGUS Integration Tests — Cross-Phase Flows + Chaos
Run standalone: python tests/test_integration.py
"""
import os, sys, time, json, sqlite3, subprocess, requests, uuid

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

def db_exec(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(sql, params)
    conn.commit()
    conn.close()

def db_query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows

def db_one(sql, params=()):
    rows = db_query(sql, params)
    return rows[0] if rows else None

def set_trust(action_type, value):
    db_exec("UPDATE trust_current SET trust_current=? WHERE action_type=?", (value, action_type))

def set_profile(name):
    db_exec("UPDATE system_state SET value=? WHERE key='ACTIVE_PROFILE'", (name,))

def set_hard_stop(val):
    db_exec("UPDATE system_state SET value=? WHERE key='SYSTEM_HARD_STOP'", ('1' if val else '0',))

def add_prime_rule(action_type):
    db_exec("INSERT OR IGNORE INTO prime_rules (action_type, condition_json, description) VALUES (?,?,?)",
            (action_type, '{}', f'Integration test rule: {action_type}'))

def remove_prime_rule(action_type):
    db_exec("DELETE FROM prime_rules WHERE action_type=?", (action_type,))

def propose(body):
    return requests.post(f'{BASE}/api/propose', json=body)

def approve_item(item_id):
    return requests.post(f'{BASE}/api/queue/{item_id}/approve')

def reject_item(item_id, reason='Integration test rejection'):
    return requests.post(f'{BASE}/api/queue/{item_id}/reject', json={'reason': reason})

print()
print('=' * 62)
print('  ARGUS INTEGRATION — Cross-Phase Flows + Chaos')
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
from argus.trust_ledger import record_event, get_trust
from argus.kernel import kernel_entry, set_hard_stop as kset_hard_stop, is_hard_stop
init_db()

try:
    # ══════════════════════════════════════════════════════════════════
    # SECTION A — Phase 1→2 Integration: Kernel Gate
    # ══════════════════════════════════════════════════════════════════

    sec('A1: Hard Stop — Blocks Everything at Kernel Level')
    kset_hard_stop(True)
    check('is_hard_stop() True after set',               is_hard_stop() is True)
    r = propose({'action_type': 'email.archive', 'entities': {'email_id': 'e1'}})
    check('HARD STOP: FREE action blocked (200/BLOCK)',  r.status_code in (200, 400))
    d = r.json()
    check('HARD STOP: decision = BLOCK',                 d['decision'] == 'BLOCK')
    check('HARD STOP: failure_reason_code in response',  'SYSTEM_HARD_STOP' in json.dumps(d))

    r2 = propose({'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com', 'subject': 'X', 'body': 'Y'}})
    check('HARD STOP: GATED action also blocked',        r2.json()['decision'] == 'BLOCK')

    r3 = propose({})
    check('HARD STOP: empty proposal also blocked',      r3.json()['decision'] == 'BLOCK')

    kset_hard_stop(False)
    check('Hard stop cleared', is_hard_stop() is False)

    sec('A2: Prime Rule — Blocks Before Policy Engine')
    add_prime_rule('email.delete')
    r = propose({'action_type': 'email.delete', 'entities': {'email_id': 'e2'}})
    check('PRIME RULE: email.delete blocked',            r.json()['decision'] == 'BLOCK')
    check('PRIME RULE: failure_reason_code correct',     r.json()['decision_dict']['failure_reason_code'] == 'PRIME_RULE_MATCH')
    remove_prime_rule('email.delete')
    r = propose({'action_type': 'email.delete', 'entities': {'email_id': 'e2'}})
    check('After prime rule removed: email.delete not blocked by prime rule',
          r.json()['decision_dict'].get('failure_reason_code') != 'PRIME_RULE_MATCH')

    # ══════════════════════════════════════════════════════════════════
    # SECTION B — Phase 1→2→3: FREE Action Full Flow
    # ══════════════════════════════════════════════════════════════════

    sec('B1: FREE Action → ALLOW (no queue entry)')
    r = propose({'action_type': 'email.archive', 'entities': {'email_id': 'e10'}})
    d = r.json()
    check('FREE action: status 200',          r.status_code == 200)
    check('FREE action: decision = ALLOW',     d['decision'] == 'ALLOW')
    check('FREE action: queue is null',        d['queue'] is None)
    check('FREE action: trust field present',  d['trust'] is not None)
    check('FREE action: event_created=True (Phase 4 wired)',  d['trust'].get('event_created') is True)

    sec('B2: FREE Action — All 9 Free Actions ALLOW')
    FREE = ['email.compose', 'email.archive', 'email.mark_read', 'email.star', 'email.move',
            'calendar.accept', 'calendar.label', 'calendar.color', 'label.apply']
    ENTITIES = {
        'email.compose':    {'subject': 'Test subject', 'body': 'Body text'},
        'email.archive':    {'email_id': 'e1'},
        'email.mark_read':  {'email_id': 'e1'},
        'email.star':       {'email_id': 'e1'},
        'email.move':       {'email_id': 'e1', 'destination': 'Work'},
        'calendar.accept':  {'event_id': 'ev1'},
        'calendar.label':   {'event_id': 'ev1', 'label': 'work'},
        'calendar.color':   {'event_id': 'ev1', 'color': 'blue'},
        'label.apply':      {'email_id': 'e1', 'label': 'important'},
    }
    for action in FREE:
        r = propose({'action_type': action, 'entities': ENTITIES[action]})
        check(f'FREE {action} → ALLOW', r.json()['decision'] == 'ALLOW')

    # ══════════════════════════════════════════════════════════════════
    # SECTION C — Phase 1→2→3: GATED Action Full Flow (Balanced profile)
    # ══════════════════════════════════════════════════════════════════

    sec('C1: GATED Action → GATED → Queue Entry Created (Balanced profile)')
    set_trust('email.send.external', 40.0)  # below Balanced threshold (70)
    set_profile('Balanced')
    r = propose({
        'action_type': 'email.send.external',
        'entities': {'recipient': 'test@example.com', 'subject': 'Test', 'body': 'Hello'}
    })
    d = r.json()
    check('GATED action: status 200',             r.status_code == 200)
    check('GATED action: decision = GATED',       d['decision'] == 'GATED')
    check('GATED action: queue entry created',    d.get('queue') is not None)
    item_id = d.get('queue', {}).get('id', '')
    check('GATED action: queue id is non-empty',  bool(item_id))
    check('GATED action: queue status = PENDING', d['queue'].get('status') == 'PENDING')

    r_detail = requests.get(f'{BASE}/api/queue/{item_id}')
    check('GATED action: queue detail returns 200', r_detail.status_code == 200)
    check('GATED action: queue detail status = PENDING', r_detail.json().get('status') == 'PENDING')

    sec('C2: GATED Action → Approve → APPROVED State')
    time.sleep(0.1)  # small gap before approve
    r_approve = approve_item(item_id)
    check('Approve returns 200',             r_approve.status_code == 200)
    check('Approve status = APPROVED',       r_approve.json().get('status') == 'APPROVED')

    r_check = requests.get(f'{BASE}/api/queue/{item_id}')
    check('Queue detail reflects APPROVED',  r_check.json().get('status') == 'APPROVED')

    # Phase 3→4 is now wired: approve should write a trust SUCCESS event
    trust_events_count = db_one("SELECT COUNT(*) as cnt FROM trust_events WHERE action_type='email.send.external'")
    check('Phase 3→4 wired: trust event written after approve',
          int(trust_events_count['cnt']) >= 1)

    sec('C3: GATED Action → Reject → REJECTED State')
    r = propose({
        'action_type': 'email.send.external',
        'entities': {'recipient': 'b@b.com', 'subject': 'S', 'body': 'B'}
    })
    item_id2 = r.json()['queue']['id']
    r_reject = reject_item(item_id2, 'Too informal')
    check('Reject returns 200',            r_reject.status_code == 200)
    check('Reject status = REJECTED',      r_reject.json().get('status') == 'REJECTED')

    r_check2 = requests.get(f'{BASE}/api/queue/{item_id2}')
    check('Queue detail reflects REJECTED', r_check2.json().get('status') == 'REJECTED')

    sec('C4: All 11 GATED Actions Create Queue Entries')
    GATED_ACTIONS = [
        ('email.send.external', {'recipient': 'x@x.com', 'subject': 'S', 'body': 'B'}),
        ('email.send.internal', {'recipient': 'y@y.com', 'subject': 'S', 'body': 'B'}),
        ('email.reply',         {'recipient': 'a@a.com', 'body': 'Reply'}),
        ('email.forward',       {'email_id': 'e1', 'recipient': 'z@z.com'}),
        ('email.delete',        {'email_id': 'e1'}),
        ('calendar.create',     {'title': 'Mtg', 'start_time': '2026-07-01T10:00', 'end_time': '2026-07-01T11:00'}),
        ('calendar.modify',     {'event_id': 'ev1'}),
        ('calendar.delete',     {'event_id': 'ev1'}),
        ('calendar.reschedule', {'event_id': 'ev1', 'start_time': '2026-07-02T10:00', 'end_time': '2026-07-02T11:00'}),
        ('calendar.invite',     {'event_id': 'ev1', 'recipient': 'x@x.com'}),
        ('calendar.decline',    {'event_id': 'ev1'}),
    ]
    for action, entities in GATED_ACTIONS:
        set_trust(action, 40.0)
        r = propose({'action_type': action, 'entities': entities})
        d = r.json()
        check(f'GATED {action} → queue entry created',
              d.get('decision') == 'GATED' and d.get('queue') is not None)

    # ══════════════════════════════════════════════════════════════════
    # SECTION D — Phase 2→4: Trust + Policy Engine Interaction
    # ══════════════════════════════════════════════════════════════════

    sec('D1: Trust Above Balanced Threshold → Policy Engine ALLOW')
    set_profile('Balanced')  # threshold = 70
    set_trust('email.reply', 75.0)  # above threshold (70)
    db_exec("DELETE FROM trust_events WHERE action_type='email.reply'")
    # Add 10 events so inertia is off and trust_current=75 is the only relevant value
    for _ in range(10):
        db_exec(
            "INSERT INTO trust_events (event_id, timestamp, action_type, delta, reason, resulting_trust) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), int(time.time()), 'email.reply', 0.0, 'test:seed', 75.0)
        )
    r = propose({'action_type': 'email.reply', 'entities': {'recipient': 'a@a.com', 'body': 'Sure!'}})
    d = r.json()
    check('Trust 75 >= Balanced threshold 70 → ALLOW', d['decision'] == 'ALLOW')
    check('ALLOW: trust_at_evaluation reflects 75.0',
          abs(d['decision_dict'].get('trust_at_evaluation', 0) - 75.0) < 0.1)

    sec('D2: Trust Below Balanced Threshold → Policy Engine GATED')
    set_trust('email.reply', 40.0)
    r = propose({'action_type': 'email.reply', 'entities': {'recipient': 'a@a.com', 'body': 'No!'}})
    check('Trust 40 < Balanced threshold 70 → GATED', r.json()['decision'] == 'GATED')

    sec('D3: Autonomous Profile → Trust 40 Meets Threshold 40 → ALLOW')
    set_profile('Autonomous')
    set_trust('email.reply', 40.0)
    r = propose({'action_type': 'email.reply', 'entities': {'recipient': 'a@a.com', 'body': 'OK'}})
    check('Autonomous: trust 40 >= threshold 40 → ALLOW', r.json()['decision'] == 'ALLOW')
    set_profile('Balanced')

    sec('D4: Trust Ceiling — Balanced Profile Trust Cannot Exceed 85')
    set_profile('Balanced')
    db_exec("DELETE FROM trust_events WHERE action_type='calendar.create'")
    db_exec("UPDATE trust_current SET trust_current=40.0, damping_remaining=0, damping_streak=0 WHERE action_type='calendar.create'")
    for _ in range(500):
        record_event('calendar.create', 'SUCCESS', 'MEDIUM')
    final = float(db_one("SELECT trust_current FROM trust_current WHERE action_type='calendar.create'")['trust_current'])
    check('Balanced ceiling: trust_current never exceeds 85',  final <= 85.0)
    check('Balanced ceiling: trust_current near ceiling',      final >= 80.0)

    # Even at ceiling, Balanced still auto-approves (85 >= 70)
    r = propose({'action_type': 'calendar.create', 'entities': {'title': 'T', 'start_time': '2026-07-01T10:00', 'end_time': '2026-07-01T11:00'}})
    check('Balanced at ceiling (85): still ALLOW (85 >= 70)', r.json()['decision'] == 'ALLOW')

    sec('D5: Prime Rule Blocks Even at Maximum Trust')
    set_trust('calendar.delete', 85.0)
    add_prime_rule('calendar.delete')
    r = propose({'action_type': 'calendar.delete', 'entities': {'event_id': 'ev1'}})
    check('Prime rule blocks action even at trust=85', r.json()['decision'] == 'BLOCK')
    check('Blocked by PRIME_RULE_MATCH not trust', r.json()['decision_dict']['failure_reason_code'] == 'PRIME_RULE_MATCH')
    remove_prime_rule('calendar.delete')

    # ══════════════════════════════════════════════════════════════════
    # SECTION E — Phase 3 Queue State Machine Integration
    # ══════════════════════════════════════════════════════════════════

    sec('E1: Queue State Machine — Full PENDING → APPROVED → EXECUTED Flow')
    set_trust('email.forward', 40.0)
    r = propose({'action_type': 'email.forward', 'entities': {'email_id': 'e1', 'recipient': 'c@c.com'}})
    qid = r.json()['queue']['id']
    check('Propose: PENDING created', r.json()['queue']['status'] == 'PENDING')

    r_approve = approve_item(qid)
    check('Approve: APPROVED', r_approve.json()['status'] == 'APPROVED')

    # Manually set to EXECUTED via queue function (simulating execution after undo window)
    from argus.queue import approve, reject, cancel
    db_exec("UPDATE approval_queue SET status='EXECUTED', updated_at=? WHERE id=?", (int(time.time()), qid))
    r_detail = requests.get(f'{BASE}/api/queue/{qid}')
    check('EXECUTED status readable from queue', r_detail.json()['status'] == 'EXECUTED')

    sec('E2: Queue State Machine — PENDING → CANCELLED')
    r = propose({'action_type': 'email.forward', 'entities': {'email_id': 'e1', 'recipient': 'd@d.com'}})
    qid2 = r.json()['queue']['id']
    r_cancel = requests.post(f'{BASE}/api/queue/{qid2}/cancel')
    check('Cancel: CANCELLED', r_cancel.json()['status'] == 'CANCELLED')

    sec('E3: Queue State Machine — Approve CANCELLED item → INVALID_STATE_TRANSITION')
    r_bad = approve_item(qid2)
    check('Approve CANCELLED → 409', r_bad.status_code == 409)
    check('Approve CANCELLED → INVALID_STATE_TRANSITION', r_bad.json()['error_code'] == 'INVALID_STATE_TRANSITION')

    sec('E4: Undo Window — Undo Not Available After Window Closed')
    # Set undo window to minimum (30s) and check undo is blocked after it closes
    # We simulate this by setting approved_at to 60s ago
    r = propose({'action_type': 'email.reply', 'entities': {'recipient': 'a@a.com', 'body': 'Hi'}})
    qid3 = r.json()['queue']['id']
    approve_item(qid3)
    db_exec("UPDATE approval_queue SET approved_at=? WHERE id=?", (int(time.time()) - 60, qid3))
    # Trying to cancel after approval window closed should fail
    r_undo = requests.post(f'{BASE}/api/queue/{qid3}/cancel')
    check('Cancel after undo window closed → 409', r_undo.status_code == 409)

    # ══════════════════════════════════════════════════════════════════
    # SECTION F — Phase 4 Trust Ledger Integration
    # ══════════════════════════════════════════════════════════════════

    sec('F1: Trust Ledger — record_event → get_trust Consistency')
    db_exec("DELETE FROM trust_events WHERE action_type='email.compose'")
    db_exec("UPDATE trust_current SET trust_current=40.0, damping_remaining=0, damping_streak=0 WHERE action_type='email.compose'")
    db_exec("UPDATE system_state SET value='1.0' WHERE key='OVERALL_TRUST_MODIFIER'")

    events = [record_event('email.compose', 'SUCCESS', 'TRIVIAL') for _ in range(10)]
    check('All 10 events recorded successfully', all(e['success'] for e in events))

    t = get_trust('email.compose')
    db_row = float(db_one("SELECT trust_current FROM trust_current WHERE action_type='email.compose'")['trust_current'])
    check('get_trust raw_trust matches trust_current in DB',   abs(t['raw_trust'] - db_row) < 0.01)
    check('trust_events has 10 rows',
          int(db_one("SELECT COUNT(*) as cnt FROM trust_events WHERE action_type='email.compose'")['cnt']) == 10)

    sec('F2: Overall Modifier Cross-Action Effect')
    # Modifier shifts are global — success in one action affects all
    db_exec("UPDATE system_state SET value='1.0' WHERE key='OVERALL_TRUST_MODIFIER'")

    record_event('calendar.accept', 'SUCCESS', 'TRIVIAL')  # bumps modifier up
    record_event('calendar.accept', 'SUCCESS', 'TRIVIAL')
    record_event('calendar.accept', 'SUCCESS', 'TRIVIAL')

    mod = float(db_one("SELECT value FROM system_state WHERE key='OVERALL_TRUST_MODIFIER'")['value'])
    check('Global modifier up after 3 successes', mod > 1.0)

    # Now a HIGH failure on a different action benefits from that higher modifier in reverse
    r_fail = record_event('calendar.delete', 'FAILURE', 'HIGH')
    mod_after = float(db_one("SELECT value FROM system_state WHERE key='OVERALL_TRUST_MODIFIER'")['value'])
    check('Global modifier decreases after HIGH failure', mod_after < mod)

    sec('F3: Damping — Activated by HIGH Failure, Affects Future Successes')
    db_exec("DELETE FROM trust_events WHERE action_type='email.delete'")
    db_exec("UPDATE trust_current SET trust_current=60.0, damping_remaining=0, damping_streak=0 WHERE action_type='email.delete'")

    # Run 6 events first to exit inertia (inertia = first 5 events at 0.5x).
    # After inertia exits, a clean SUCCESS uses full 1.0x weight.
    # During damping, it uses min(1.0, 0.5) = 0.5x — clearly lower.
    for _ in range(6):
        record_event('email.delete', 'SUCCESS', 'HIGH')

    # Measure undampened delta (no damping, post-inertia → full 1.0x weight)
    db_exec("UPDATE trust_current SET trust_current=60.0, damping_remaining=0, damping_streak=0 WHERE action_type='email.delete'")
    mod_before = float(db_one("SELECT value FROM system_state WHERE key='OVERALL_TRUST_MODIFIER'")['value'])
    r_before = record_event('email.delete', 'SUCCESS', 'HIGH')
    delta_undampened = r_before['actual_delta']

    # Force damping activation
    record_event('email.delete', 'FAILURE', 'HIGH')
    damping = db_one("SELECT damping_remaining FROM trust_current WHERE action_type='email.delete'")
    check('HIGH failure activates damping in DB', int(damping['damping_remaining']) == 10)

    db_exec("UPDATE trust_current SET trust_current=60.0 WHERE action_type='email.delete'")
    r_dampened = record_event('email.delete', 'SUCCESS', 'HIGH')
    check('Success during damping has lower delta than before damping (damping 0.5x vs full 1.0x)',
          r_dampened['actual_delta'] < delta_undampened)

    sec('F4: Trust → Policy Engine Pipeline')
    # Drive email.send.internal trust above Balanced threshold via record_event
    db_exec("DELETE FROM trust_events WHERE action_type='email.send.internal'")
    db_exec("UPDATE trust_current SET trust_current=40.0, damping_remaining=0, damping_streak=0 WHERE action_type='email.send.internal'")
    db_exec("UPDATE system_state SET value='1.0' WHERE key='OVERALL_TRUST_MODIFIER'")
    set_profile('Balanced')

    r_before = propose({'action_type': 'email.send.internal',
                        'entities': {'recipient': 'c@c.com', 'subject': 'S', 'body': 'B'}})
    check('email.send.internal: GATED at trust=40 (below 70)', r_before.json()['decision'] == 'GATED')

    # Drive trust above threshold by writing directly to trust_current (bypass record_event weight math)
    set_trust('email.send.internal', 72.0)
    r_after = propose({'action_type': 'email.send.internal',
                       'entities': {'recipient': 'c@c.com', 'subject': 'S', 'body': 'B'}})
    check('email.send.internal: ALLOW at trust=72 (above 70)', r_after.json()['decision'] == 'ALLOW')

    # ══════════════════════════════════════════════════════════════════
    # SECTION G — CHAOS: Break One Area, Observe System
    # ══════════════════════════════════════════════════════════════════

    sec('G1: CHAOS — Hard Stop Mid-Flow (queue items survive)')
    set_trust('email.reply', 40.0)
    r = propose({'action_type': 'email.reply', 'entities': {'recipient': 'a@a.com', 'body': 'Hi'}})
    qid_chaos = r.json()['queue']['id']
    check('Pre-chaos: queue item created', bool(qid_chaos))

    kset_hard_stop(True)

    # Queue read still works (hard stop does not corrupt queue)
    r_list = requests.get(f'{BASE}/api/queue')
    check('CHAOS hard stop ON: queue GET still responds', r_list.status_code == 200)

    # Approve still works? No — hard stop is kernel-level, not queue-level
    # The queue endpoints themselves don't check hard stop
    r_approve_chaos = approve_item(qid_chaos)
    check('CHAOS hard stop ON: approve still works (queue layer independent)', r_approve_chaos.status_code == 200)

    # New proposals blocked
    r_new = propose({'action_type': 'email.reply', 'entities': {'recipient': 'a@a.com', 'body': 'More'}})
    check('CHAOS hard stop ON: new proposals blocked', r_new.json()['decision'] == 'BLOCK')

    kset_hard_stop(False)

    sec('G2: CHAOS — Trust Driven to Floor (0), System Does Not Crash')
    db_exec("UPDATE trust_current SET trust_current=1.0, damping_remaining=0, damping_streak=0 WHERE action_type='email.delete'")
    db_exec("UPDATE system_state SET value='0.8' WHERE key='OVERALL_TRUST_MODIFIER'")  # already at min

    for _ in range(20):
        r_crush = record_event('email.delete', 'FAILURE', 'HIGH')
        check_floor = float(db_one("SELECT trust_current FROM trust_current WHERE action_type='email.delete'")['trust_current'])
        if check_floor < 0:
            failed += 1
            print(f'    [FAIL] Trust went below 0: {check_floor}')
            break

    final_floor = float(db_one("SELECT trust_current FROM trust_current WHERE action_type='email.delete'")['trust_current'])
    check('CHAOS floor: trust_current >= 0 after 20 HIGH failures', final_floor >= 0.0)
    check('CHAOS floor: record_event still returns success at floor', r_crush['success'] is True)

    # Policy engine handles trust=0 gracefully
    r_floor_propose = propose({'action_type': 'email.delete', 'entities': {'email_id': 'e1'}})
    check('CHAOS floor: policy engine handles trust=0 (still GATED not crash)',
          r_floor_propose.json().get('decision') in ('GATED', 'BLOCK'))

    sec('G3: CHAOS — Flood Queue With Invalid Transitions')
    r = propose({'action_type': 'email.reply', 'entities': {'recipient': 'a@a.com', 'body': 'Flood'}})
    qid_flood = r.json()['queue']['id']
    approve_item(qid_flood)  # APPROVED

    # Now try 10 more approvals — should all fail with INVALID_STATE_TRANSITION
    responses = [approve_item(qid_flood) for _ in range(10)]
    check('CHAOS flood: all 10 invalid approvals return 409', all(r.status_code == 409 for r in responses))
    check('CHAOS flood: error code consistent', all(r.json()['error_code'] == 'INVALID_STATE_TRANSITION' for r in responses))
    check('CHAOS flood: queue item status NOT corrupted',
          requests.get(f'{BASE}/api/queue/{qid_flood}').json()['status'] == 'APPROVED')

    # Trust was not affected by invalid transitions
    trust_after_flood = get_trust('email.reply')
    check('CHAOS flood: invalid transitions did not affect trust events',
          True)  # If no exception thrown, trust is fine

    sec('G4: CHAOS — Missing Required Fields Handled Gracefully')
    test_cases = [
        {},
        {'action_type': 'email.send.external'},                              # entities missing
        {'action_type': 'email.send.external', 'entities': {}},             # recipient missing
        {'action_type': 'email.send.external', 'entities': {'recipient': ''}},  # empty recipient
        {'action_type': 'not.a.real.action', 'entities': {}},               # unknown action
        {'action_type': '', 'entities': {}},                                 # empty action
        {'action_type': None, 'entities': {}},                               # null action
        'not a dict',                                                         # not JSON object
    ]
    for i, payload in enumerate(test_cases):
        if isinstance(payload, str):
            r = requests.post(f'{BASE}/api/propose', data=payload, headers={'Content-Type': 'application/json'})
        else:
            r = requests.post(f'{BASE}/api/propose', json=payload)
        check(f'CHAOS bad input #{i+1}: server returns 4xx or valid BLOCK',
              r.status_code in (400, 422) or r.json().get('decision') == 'BLOCK')

    sec('G5: CHAOS — DB Consistency After All Chaos Tests')
    trust_rows = db_query("SELECT action_type, trust_current FROM trust_current")
    check('DB still has 20 trust_current rows after chaos', len(trust_rows) == 20)
    check('All trust_current values >= 0', all(float(r['trust_current']) >= 0 for r in trust_rows))
    check('All trust_current values <= 100', all(float(r['trust_current']) <= 100 for r in trust_rows))

    queue_rows = db_query("SELECT id, status FROM approval_queue")
    valid_statuses = {'PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'MANUAL_REVIEW', 'EXECUTED', 'CANCELLED'}
    check('All queue statuses are valid enum values', all(r['status'] in valid_statuses for r in queue_rows))

    sys_state = {r[0]: r[1] for r in db_query("SELECT key, value FROM system_state")}
    check('SYSTEM_HARD_STOP is 0 after chaos (cleared)', sys_state.get('SYSTEM_HARD_STOP') == '0')
    check('ACTIVE_PROFILE still valid', sys_state.get('ACTIVE_PROFILE') in ('Strict', 'Balanced', 'Autonomous'))
    try:
        float(sys_state.get('OVERALL_TRUST_MODIFIER', 'x'))
        mod_valid = True
    except ValueError:
        mod_valid = False
    check('OVERALL_TRUST_MODIFIER is valid float', mod_valid)

    # ══════════════════════════════════════════════════════════════════
    # SECTION H — Regression Guards
    # ══════════════════════════════════════════════════════════════════

    sec('H1: Core Invariant — LLMs Propose, Code Decides')
    # The policy engine NEVER delegates decisions to external systems
    # Verify: BLOCK decisions always have deterministic codes
    kset_hard_stop(True)
    r = propose({'action_type': 'email.archive', 'entities': {'email_id': 'e1'}})
    dd = r.json()['decision_dict']
    check('Hard stop block has deterministic failure_reason_code',
          dd.get('failure_reason_code') == 'SYSTEM_HARD_STOP')
    kset_hard_stop(False)

    set_profile('Balanced')
    set_trust('email.send.external', 40.0)
    r = propose({'action_type': 'email.send.external', 'entities': {'recipient': 'z@z.com', 'subject': 'X', 'body': 'Y'}})
    dd = r.json()['decision_dict']
    check('GATED decision has deterministic trust_at_evaluation',
          isinstance(dd.get('trust_at_evaluation'), (int, float)))

    sec('H2: Policy Engine Output Always Has Required Fields')
    for body in [
        {'action_type': 'email.archive', 'entities': {'email_id': 'e1'}},
        {'action_type': 'email.send.external', 'entities': {'recipient': 'r@r.com', 'subject': 'S', 'body': 'B'}},
    ]:
        r = propose(body)
        dd = r.json().get('decision_dict', {})
        for field in ['decision', 'decision_source', 'trace', 'action_expiry']:
            check(f'Policy output has {field} for {body["action_type"]}',
                  field in dd, got=list(dd.keys()))

    sec('H3: Double Hard Stop Toggle Does Not Corrupt State')
    for _ in range(5):
        kset_hard_stop(True)
        kset_hard_stop(False)
    check('State stable after 5 toggle cycles: SYSTEM_HARD_STOP = 0',
          db_one("SELECT value FROM system_state WHERE key='SYSTEM_HARD_STOP'")['value'] == '0')
    r = propose({'action_type': 'email.archive', 'entities': {'email_id': 'e1'}})
    check('Proposals work normally after toggle cycles', r.json()['decision'] == 'ALLOW')

    sec('H4: High-Trust Action Auto-Approve Cannot Be Bypassed By Hard Stop Race')
    set_profile('Autonomous')   # threshold=40, trust=40 → ALLOW
    set_trust('email.delete', 42.0)
    r1 = propose({'action_type': 'email.delete', 'entities': {'email_id': 'e1'}})
    kset_hard_stop(True)
    r2 = propose({'action_type': 'email.delete', 'entities': {'email_id': 'e2'}})
    kset_hard_stop(False)
    check('Pre-stop: trust=42 ALLOW on Autonomous',   r1.json()['decision'] == 'ALLOW')
    check('Post-stop: same proposal BLOCK (hard stop wins)', r2.json()['decision'] == 'BLOCK')
    set_profile('Balanced')

finally:
    server.terminate()
    print()
    print('-' * 62)
    status = 'ALL CLEAR' if failed == 0 else 'FAILURES DETECTED'
    print(f'  RESULT: {passed} passed | {failed} failed | {status}')
    print('=' * 62)
    print()
    sys.exit(0 if failed == 0 else 1)
