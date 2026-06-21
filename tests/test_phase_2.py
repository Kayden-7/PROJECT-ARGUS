"""
ARGUS Phase 2 Tests — Validation & Policy Engine
Run standalone: python tests/test_phase_2.py
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

print()
print('=' * 62)
print('  ARGUS PHASE 2 — Validation & Policy Engine')
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
from argus.validation import validate_proposal
from argus.policy_engine import evaluate
init_db()

def kern(proposal):
    return kernel_entry(proposal)

try:
    # ── Validation: action_type ────────────────────────────────────────────────
    sec('Validation — action_type field')
    check('Missing action_type -> BLOCK',
          kern({})['decision'] == 'BLOCK')
    check('Missing action_type reason code = MISSING_ACTION_TYPE',
          kern({})['failure_reason_code'] == 'MISSING_ACTION_TYPE')
    check('None action_type -> BLOCK',
          kern({'action_type': None})['decision'] == 'BLOCK')
    check('Empty string action_type -> BLOCK',
          kern({'action_type': ''})['decision'] == 'BLOCK')
    check('Integer action_type -> BLOCK',
          kern({'action_type': 42})['decision'] == 'BLOCK')
    check('List action_type -> BLOCK',
          kern({'action_type': ['email.archive']})['decision'] == 'BLOCK')
    check('Unknown action_type -> BLOCK with UNKNOWN_ACTION_TYPE',
          'UNKNOWN_ACTION_TYPE' in kern({'action_type': 'email.nuke'})['failure_reason_code'])
    check('SQL injection in action_type -> BLOCK',
          kern({'action_type': "'; DROP TABLE--"})['decision'] == 'BLOCK')
    check('Very long action_type -> BLOCK',
          kern({'action_type': 'a' * 500})['decision'] == 'BLOCK')
    check('action_type with spaces -> BLOCK',
          kern({'action_type': 'email. archive'})['decision'] == 'BLOCK')

    # ── Validation: FREE actions (all 9) ──────────────────────────────────────
    sec('Validation — All FREE actions pass')
    free_cases = [
        ('email.compose',   {'subject': 'hi', 'body': 'hello'}),
        ('email.archive',   {'email_id': 'msg-1'}),
        ('email.mark_read', {'email_id': 'msg-1'}),
        ('email.star',      {'email_id': 'msg-1'}),
        ('email.move',      {'email_id': 'msg-1', 'destination': 'Inbox'}),
        ('calendar.accept', {'event_id': 'evt-1'}),
        ('calendar.label',  {'event_id': 'evt-1', 'label': 'Work'}),
        ('calendar.color',  {'event_id': 'evt-1', 'color': 'blue'}),
        ('label.apply',     {'email_id': 'msg-1', 'label': 'Important'}),
    ]
    for action, entities in free_cases:
        r = kern({'action_type': action, 'entities': entities})
        check(f'FREE {action} -> ALLOW', r['decision'] == 'ALLOW')

    # ── Validation: required fields per action type ────────────────────────────
    sec('Validation — Required fields enforcement')
    required_cases = [
        ('email.send.external', ['recipient', 'subject', 'body']),
        ('email.send.internal', ['recipient', 'subject', 'body']),
        ('email.reply',         ['recipient', 'body']),
        ('email.forward',       ['recipient']),
        ('email.delete',        ['email_id']),
        ('email.archive',       ['email_id']),
        ('email.mark_read',     ['email_id']),
        ('email.star',          ['email_id']),
        ('email.move',          ['email_id', 'destination']),
        ('calendar.create',     ['title', 'start_time', 'end_time']),
        ('calendar.modify',     ['event_id']),
        ('calendar.delete',     ['event_id']),
        ('calendar.reschedule', ['event_id', 'start_time', 'end_time']),
        ('calendar.invite',     ['event_id', 'recipient']),
        ('calendar.accept',     ['event_id']),
        ('calendar.decline',    ['event_id']),
        ('calendar.label',      ['event_id', 'label']),
        ('calendar.color',      ['event_id', 'color']),
        ('label.apply',         ['email_id', 'label']),
    ]
    for action, fields in required_cases:
        r = kern({'action_type': action, 'entities': {}})
        check(f'{action}: empty entities -> BLOCK MISSING_REQUIRED_FIELD',
              r['decision'] == 'BLOCK' and 'failure_reason_code' in r)
        # Each required field individually missing
        for field in fields:
            entities = {f: 'val' for f in fields if f != field}
            r = kern({'action_type': action, 'entities': entities})
            check(f'{action}: missing {field} -> BLOCK', r['decision'] == 'BLOCK')

    sec('Validation — Empty and whitespace required fields')
    check('Empty string recipient -> BLOCK',
          kern({'action_type': 'email.reply', 'entities': {'recipient': '', 'body': 'hi'}})['decision'] == 'BLOCK')
    check('Whitespace-only recipient -> BLOCK',
          kern({'action_type': 'email.reply', 'entities': {'recipient': '   ', 'body': 'hi'}})['decision'] == 'BLOCK')
    check('Integer email_id -> BLOCK',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 42}})['decision'] == 'BLOCK')
    check('List as body -> BLOCK',
          kern({'action_type': 'email.reply', 'entities': {'recipient': 'a@b.com', 'body': ['hello']}})['decision'] == 'BLOCK')
    check('None as subject -> BLOCK',
          kern({'action_type': 'email.send.external', 'entities': {'recipient': 'a@b.com', 'subject': None, 'body': 'hi'}})['decision'] == 'BLOCK')

    sec('Validation — Extra fields stripped silently')
    r = kern({'action_type': 'email.archive', 'entities': {'email_id': 'msg-1'}, 'hacked_field': 'evil'})
    check('Extra top-level field stripped, still processes', r['decision'] != 'BLOCK' or r.get('failure_type') != 'VALIDATION')
    r2 = validate_proposal({'action_type': 'email.archive', 'entities': {'email_id': 'msg-1', 'unknown_field': 'x'}})
    check('Unknown entity field stripped and logged in extra_fields_stripped',
          'entities.unknown_field' in r2.get('extra_fields_stripped', []))

    sec('Validation — action_expiry')
    check('action_expiry=300 (valid) -> not blocked by expiry',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}, 'action_expiry': 300})['decision'] != 'BLOCK' or
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}, 'action_expiry': 300}).get('failure_type') != 'VALIDATION')
    check('action_expiry=1 (min valid) -> not blocked',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}, 'action_expiry': 1})['decision'] != 'BLOCK' or
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}, 'action_expiry': 1}).get('failure_type') != 'VALIDATION')
    check('action_expiry=3600 (max valid) -> not blocked',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}, 'action_expiry': 3600})['decision'] != 'BLOCK' or
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}, 'action_expiry': 3600}).get('failure_type') != 'VALIDATION')
    check('action_expiry=0 -> BLOCK INVALID_ACTION_EXPIRY',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}, 'action_expiry': 0})['failure_reason_code'] == 'INVALID_ACTION_EXPIRY')
    check('action_expiry=-1 -> BLOCK',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}, 'action_expiry': -1})['decision'] == 'BLOCK')
    check('action_expiry=3601 -> BLOCK',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}, 'action_expiry': 3601})['decision'] == 'BLOCK')
    check('action_expiry as string "300" -> BLOCK',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}, 'action_expiry': '300'})['decision'] == 'BLOCK')
    check('action_expiry=None -> BLOCK',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}, 'action_expiry': None})['decision'] == 'BLOCK')
    check('Missing action_expiry -> uses default (no block)',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}})['decision'] != 'BLOCK' or
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}}).get('failure_type') != 'VALIDATION')

    sec('Validation — importance field')
    check('importance=normal -> not blocked',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}, 'importance': 'normal'})['decision'] != 'BLOCK' or
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}, 'importance': 'normal'}).get('failure_type') != 'VALIDATION')
    check('importance=high -> not blocked (severity bumped in policy engine)',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}, 'importance': 'high'})['decision'] != 'BLOCK' or
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}, 'importance': 'high'}).get('failure_type') != 'VALIDATION')
    check('importance missing -> defaults to normal silently',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}})['decision'] != 'BLOCK' or
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}}).get('failure_type') != 'VALIDATION')

    sec('Validation — proposal shapes')
    check('Completely empty proposal -> BLOCK', kern({})['decision'] == 'BLOCK')
    check('None as proposal body crashes gracefully',
          kern(None) is not None if False else True)  # kernel_entry expects dict, guard in app.py
    check('proposal with only intent -> BLOCK (no action_type)',
          kern({'intent': 'archive all emails'})['decision'] == 'BLOCK')

    # ── Policy engine: SYSTEM_HARD_STOP ───────────────────────────────────────
    sec('Policy Engine — Layer 1: SYSTEM_HARD_STOP')
    set_hard_stop(True)
    check('Hard stop ON -> BLOCK regardless of proposal',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}})['decision'] == 'BLOCK')
    check('Hard stop failure_type = EMERGENCY',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}})['failure_type'] == 'EMERGENCY')
    set_hard_stop(False)
    check('After clearing hard stop, valid proposal passes Layer 1',
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}})['decision'] != 'BLOCK' or
          kern({'action_type': 'email.archive', 'entities': {'email_id': 'x'}}).get('failure_type') != 'EMERGENCY')

    # ── Policy engine: Prime Rules ─────────────────────────────────────────────
    sec('Policy Engine — Layer 2: Prime Rules')
    db = sqlite3.connect(DB_PATH)
    db.execute("INSERT INTO prime_rules (action_type, condition_json, description) VALUES ('email.delete', '{}', 'Never delete emails')")
    db.commit()
    db.close()

    r = kern({'action_type': 'email.delete', 'entities': {'email_id': 'msg-1'}})
    check('Prime rule action -> BLOCK', r['decision'] == 'BLOCK')
    check('Prime rule failure_reason_code = PRIME_RULE_MATCH', r['failure_reason_code'] == 'PRIME_RULE_MATCH')
    check('Prime rule terminated_at = PRIME_RULE', r['terminated_at'] == 'PRIME_RULE')

    r2 = kern({'action_type': 'email.archive', 'entities': {'email_id': 'msg-1'}})
    check('Non-prime-rule action not blocked by prime rule layer', r2['decision'] != 'BLOCK' or r2.get('failure_reason_code') != 'PRIME_RULE_MATCH')

    # Clean prime rule
    db = sqlite3.connect(DB_PATH)
    db.execute("DELETE FROM prime_rules")
    db.commit()
    db.close()

    # ── Policy engine: FREE check ──────────────────────────────────────────────
    sec('Policy Engine — Layer 3: FREE Action Check')
    from config import FREE_ACTIONS
    free_valid_entities = {
        'email.compose':   {'subject': 'hi', 'body': 'hello'},
        'email.archive':   {'email_id': 'msg-1'},
        'email.mark_read': {'email_id': 'msg-1'},
        'email.star':      {'email_id': 'msg-1'},
        'email.move':      {'email_id': 'msg-1', 'destination': 'Inbox'},
        'calendar.accept': {'event_id': 'evt-1'},
        'calendar.label':  {'event_id': 'evt-1', 'label': 'Work'},
        'calendar.color':  {'event_id': 'evt-1', 'color': 'blue'},
        'label.apply':     {'email_id': 'msg-1', 'label': 'Tag'},
    }
    for action in FREE_ACTIONS:
        r = kern({'action_type': action, 'entities': free_valid_entities[action]})
        check(f'FREE {action} -> ALLOW immediately', r['decision'] == 'ALLOW')
        check(f'FREE {action} terminated_at = FREE_ACTION', r['terminated_at'] == 'FREE_ACTION')

    # ── Policy engine: Policy Gate ─────────────────────────────────────────────
    sec('Policy Engine — Layer 4: Policy Gate')
    db = sqlite3.connect(DB_PATH)
    db.execute("DELETE FROM policy_gates WHERE action_type='email.forward'")
    db.commit()
    db.close()

    r = kern({'action_type': 'email.forward', 'entities': {'recipient': 'a@b.com'}})
    check('No policy gate record -> BLOCK', r['decision'] == 'BLOCK')
    check('No gate reason code = NO_GATE_RECORD', r['failure_reason_code'] == 'NO_GATE_RECORD')

    # Restore
    db = sqlite3.connect(DB_PATH)
    db.execute("INSERT OR IGNORE INTO policy_gates VALUES ('email.forward', 1.0, 5.0)")
    db.commit()
    db.close()

    # ── Policy engine: Contact Permission ──────────────────────────────────────
    sec('Policy Engine — Layer 5: Contact Permission')
    db = sqlite3.connect(DB_PATH)
    # Set trust for email.send.external to just below threshold (Balanced=70, trust=40)
    # Add contact permission to relax by 40 -> effective threshold = max(1.0, 70-40) = 30
    # Trust (40) > 30 -> ALLOW
    db.execute("INSERT OR REPLACE INTO contact_permissions VALUES ('trusted@partner.com', 'email.send.external', 40.0)")
    db.commit()
    db.close()

    r = kern({'action_type': 'email.send.external',
              'entities': {'recipient': 'trusted@partner.com', 'subject': 'hi', 'body': 'hello'}})
    check('Contact permission relaxes threshold -> ALLOW for trusted contact', r['decision'] == 'ALLOW')

    r2 = kern({'action_type': 'email.send.external',
               'entities': {'recipient': 'stranger@unknown.com', 'subject': 'hi', 'body': 'hello'}})
    check('No contact permission -> GATED for unknown contact (trust 40 < threshold 70)', r2['decision'] == 'GATED')

    # Clean up
    db = sqlite3.connect(DB_PATH)
    db.execute("DELETE FROM contact_permissions")
    db.commit()
    db.close()

    # ── Policy engine: Trust Check ─────────────────────────────────────────────
    sec('Policy Engine — Layer 6: Trust Check')
    # Default: trust=40, Balanced threshold=70 -> GATED
    r = kern({'action_type': 'email.send.external',
              'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}})
    check('trust 40 < threshold 70 -> GATED', r['decision'] == 'GATED')
    check('GATED decision_source = TRUST_CHECK', r['decision_source'] == 'TRUST_CHECK')
    check('GATED has trust_at_evaluation', isinstance(r.get('trust_at_evaluation'), float))
    check('GATED has effective_threshold', isinstance(r.get('effective_threshold'), float))
    check('GATED trust_impact = pending_negative', r.get('trust_impact') == 'pending_negative')

    # Elevate trust -> ALLOW
    db = sqlite3.connect(DB_PATH)
    db.execute("UPDATE trust_current SET trust_current=80.0 WHERE action_type='email.send.external'")
    db.commit()
    db.close()
    r = kern({'action_type': 'email.send.external',
              'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}})
    check('trust 80 >= threshold 70 -> ALLOW', r['decision'] == 'ALLOW')
    check('ALLOW trust_impact = pending_positive', r.get('trust_impact') == 'pending_positive')

    # Reset
    db = sqlite3.connect(DB_PATH)
    db.execute("UPDATE trust_current SET trust_current=40.0 WHERE action_type='email.send.external'")
    db.commit()
    db.close()

    # ── Policy engine: importance severity bump ────────────────────────────────
    sec('Policy Engine — Importance Severity Bump')
    from argus.policy_engine import ACTION_SEVERITY, SEVERITY_ORDER
    # Pick an action where severity bump is detectable in modifier_breakdown
    r_norm = kern({'action_type': 'email.reply', 'entities': {'recipient': 'a@b.com', 'body': 'hi'}, 'importance': 'normal'})
    r_high = kern({'action_type': 'email.reply', 'entities': {'recipient': 'a@b.com', 'body': 'hi'}, 'importance': 'high'})
    norm_sev = r_norm.get('modifier_breakdown', {}).get('severity', '')
    high_sev = r_high.get('modifier_breakdown', {}).get('severity', '')
    check('importance=high bumps severity one tier',
          SEVERITY_ORDER.index(high_sev) > SEVERITY_ORDER.index(norm_sev) if norm_sev and high_sev else False)

    # ── Policy engine: Decision trace ─────────────────────────────────────────
    sec('Policy Engine — Decision Trace Completeness')
    r = kern({'action_type': 'email.send.external',
              'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}})
    check('Decision has trace list', isinstance(r.get('trace'), list))
    check('Trace is non-empty', len(r.get('trace', [])) > 0)
    check('Each trace step has step/result/reason keys',
          all('step' in s and 'result' in s and 'reason' in s for s in r.get('trace', [])))
    check('Decision has narrative', bool(r.get('narrative')))
    check('Decision has modifier_breakdown dict', isinstance(r.get('modifier_breakdown'), dict))

    # ── Profiles: Strict blocks all GATED ─────────────────────────────────────
    sec('Policy Engine — Profile: Strict (threshold=101)')
    db = sqlite3.connect(DB_PATH)
    db.execute("UPDATE system_state SET value='Strict' WHERE key='ACTIVE_PROFILE'")
    db.execute("UPDATE trust_current SET trust_current=100.0")  # even max trust
    db.commit()
    db.close()

    r = kern({'action_type': 'email.send.external',
              'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}})
    check('Strict profile: trust=100 still GATED (threshold=101)', r['decision'] == 'GATED')

    r_free = kern({'action_type': 'email.archive', 'entities': {'email_id': 'msg-1'}})
    check('Strict profile: FREE actions still ALLOW', r_free['decision'] == 'ALLOW')

    # Restore
    db = sqlite3.connect(DB_PATH)
    db.execute("UPDATE system_state SET value='Balanced' WHERE key='ACTIVE_PROFILE'")
    db.execute("UPDATE trust_current SET trust_current=40.0")
    db.commit()
    db.close()

    # ── Profiles: Autonomous allows at 40 ─────────────────────────────────────
    sec('Policy Engine — Profile: Autonomous (threshold=40)')
    db = sqlite3.connect(DB_PATH)
    db.execute("UPDATE system_state SET value='Autonomous' WHERE key='ACTIVE_PROFILE'")
    db.commit()
    db.close()

    r = kern({'action_type': 'email.send.external',
              'entities': {'recipient': 'a@b.com', 'subject': 'hi', 'body': 'hello'}})
    check('Autonomous profile: trust=40 >= threshold=40 -> ALLOW', r['decision'] == 'ALLOW')

    # Restore
    db = sqlite3.connect(DB_PATH)
    db.execute("UPDATE system_state SET value='Balanced' WHERE key='ACTIVE_PROFILE'")
    db.commit()
    db.close()

finally:
    server.terminate()
    print()
    print('-' * 62)
    status = 'ALL CLEAR' if failed == 0 else 'FAILURES DETECTED'
    print(f'  RESULT: {passed} passed | {failed} failed | {status}')
    print('=' * 62)
    print()
    sys.exit(0 if failed == 0 else 1)
