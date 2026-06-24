"""
ARGUS Phase 1 Tests — Flask Skeleton & Database
Run standalone: python tests/test_phase_1.py
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
print('  ARGUS PHASE 1 — Flask Skeleton & Database')
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
from argus.kernel import kernel_entry, set_hard_stop, is_hard_stop
init_db()

try:
    # ── Health endpoint ────────────────────────────────────────────────────────
    sec('Health Endpoint')
    r = requests.get(f'{BASE}/health')
    check('GET /health returns 200', r.status_code == 200)
    d = r.json()
    check('Body: status = ok',      d.get('status')  == 'ok')
    check('Body: system = ARGUS',   d.get('system')  == 'ARGUS')
    check('Body: version = 1.0',    d.get('version') == '1.0')
    check('Body has no extra keys', set(d.keys()) == {'status', 'system', 'version'})

    r = requests.post(f'{BASE}/health')
    check('POST /health returns 405', r.status_code == 405)
    r = requests.delete(f'{BASE}/health')
    check('DELETE /health returns 405', r.status_code == 405)
    r = requests.put(f'{BASE}/health')
    check('PUT /health returns 405', r.status_code == 405)

    # ── Database schema ────────────────────────────────────────────────────────
    sec('Database Schema — All Tables')
    db = sqlite3.connect(DB_PATH)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in ['system_state', 'permission_profiles', 'prime_rules', 'policy_gates',
              'contact_permissions', 'approval_queue', 'trust_current', 'trust_events',
              'audit_log', 'rate_limits', 'pending_executions', 'demo_emails']:
        check(f'Table exists: {t}', t in tables)

    # ── Column types ───────────────────────────────────────────────────────────
    sec('Column Schema Correctness')
    tc_cols = {r[1]: r[2] for r in db.execute("PRAGMA table_info(trust_current)").fetchall()}
    check('trust_current.action_type is TEXT',       tc_cols.get('action_type')       == 'TEXT')
    check('trust_current.trust_current is REAL',     tc_cols.get('trust_current')     == 'REAL')
    check('trust_current.damping_remaining exists',  'damping_remaining' in tc_cols)
    check('trust_current.damping_streak exists',     'damping_streak'    in tc_cols)

    te_cols = {r[1]: r[2] for r in db.execute("PRAGMA table_info(trust_events)").fetchall()}
    check('trust_events.delta is REAL (not INTEGER)', te_cols.get('delta') == 'REAL')
    check('trust_events.resulting_trust is REAL',     te_cols.get('resulting_trust') == 'REAL')

    aq_cols = {r[1] for r in db.execute("PRAGMA table_info(approval_queue)").fetchall()}
    for col in ['id', 'proposal_json', 'decision_json', 'status', 'created_at',
                'expires_at', 'approved_at', 'updated_at', 'status_reason', 'execution_id']:
        check(f'approval_queue.{col} exists', col in aq_cols)

    # ── Database seeds ─────────────────────────────────────────────────────────
    sec('Database Seeds — system_state')
    def sv(key):
        row = db.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    check('SYSTEM_HARD_STOP = 0',          sv('SYSTEM_HARD_STOP')        == '0')
    check('ACTIVE_PROFILE = Balanced',     sv('ACTIVE_PROFILE')          == 'Balanced')
    check('OVERALL_TRUST_MODIFIER = 1.0',  sv('OVERALL_TRUST_MODIFIER')  == '1.0')
    check('UNDO_WINDOW_SECONDS = 60',      sv('UNDO_WINDOW_SECONDS')     == '60')

    sec('Database Seeds — permission_profiles')
    profs = {r[0]: r[1] for r in db.execute("SELECT profile_name, active FROM permission_profiles").fetchall()}
    check('Balanced profile exists and is active',    profs.get('Balanced')   == 1)
    check('Strict profile exists and is inactive',    profs.get('Strict')     == 0)
    check('Autonomous profile exists and is inactive',profs.get('Autonomous') == 0)
    check('Exactly 3 profiles',                       len(profs) == 3)

    sec('Database Seeds — trust_current')
    trust_rows = db.execute("SELECT action_type, trust_current, damping_remaining, damping_streak FROM trust_current").fetchall()
    check('trust_current has exactly 20 rows',            len(trust_rows) == 20)
    check('All trust scores seeded at 40.0',              all(r[1] == 40.0 for r in trust_rows))
    check('All damping_remaining seeded at 0',            all(r[2] == 0    for r in trust_rows))
    check('All damping_streak seeded at 0',               all(r[3] == 0    for r in trust_rows))

    sec('Database Seeds — policy_gates')
    gates = db.execute("SELECT action_type FROM policy_gates").fetchall()
    check('policy_gates has 11 rows (GATED actions only)', len(gates) == 11)
    free_in_gates = db.execute(
        "SELECT COUNT(*) FROM policy_gates WHERE action_type IN "
        "('email.archive','email.star','email.compose','email.mark_read','email.move',"
        "'calendar.accept','calendar.label','calendar.color','label.apply')"
    ).fetchone()[0]
    check('FREE actions are NOT in policy_gates', free_in_gates == 0)
    db.close()

    # ── Kernel hard stop ───────────────────────────────────────────────────────
    sec('Kernel — Hard Stop Mechanics')
    check('is_hard_stop() is False by default', is_hard_stop() is False)

    set_hard_stop(True)
    check('set_hard_stop(True) sets flag to True', is_hard_stop() is True)

    r = kernel_entry({'action_type': 'email.archive', 'entities': {'email_id': 'msg-1'}})
    check('kernel_entry blocked when hard stop ON',        r['decision']            == 'BLOCK')
    check('Hard stop block failure_type = EMERGENCY',      r['failure_type']        == 'EMERGENCY')
    check('Hard stop block reason = SYSTEM_HARD_STOP',     r['failure_reason_code'] == 'SYSTEM_HARD_STOP')
    check('Hard stop block action_expiry = 0',             r['action_expiry']       == 0)
    check('Hard stop ignores any proposal content',
          kernel_entry({'action_type': 'email.delete', 'entities': {'email_id': 'x'}})['decision'] == 'BLOCK')
    check('Hard stop blocks even empty proposal',
          kernel_entry({})['decision'] == 'BLOCK')

    set_hard_stop(False)
    check('set_hard_stop(False) clears flag', is_hard_stop() is False)

    r = kernel_entry({'action_type': 'email.archive', 'entities': {'email_id': 'msg-1'}})
    check('kernel_entry not EMERGENCY-blocked when hard stop OFF',
          not (r['decision'] == 'BLOCK' and r.get('failure_type') == 'EMERGENCY'))

    # ── Double init safety ─────────────────────────────────────────────────────
    sec('Double Init Safety')
    init_db()
    db2 = sqlite3.connect(DB_PATH)
    check('Double init: trust_current still 20 rows',
          db2.execute("SELECT COUNT(*) FROM trust_current").fetchone()[0] == 20)
    check('Double init: system_state HARD_STOP still 0',
          db2.execute("SELECT value FROM system_state WHERE key='SYSTEM_HARD_STOP'").fetchone()[0] == '0')
    check('Double init: profiles still 3',
          db2.execute("SELECT COUNT(*) FROM permission_profiles").fetchone()[0] == 3)
    check('Double init: policy_gates still 11',
          db2.execute("SELECT COUNT(*) FROM policy_gates").fetchone()[0] == 11)
    db2.close()

    # ── 404 on unknown routes ──────────────────────────────────────────────────
    sec('Unknown Routes')
    check('GET /nonexistent returns 404', requests.get(f'{BASE}/nonexistent').status_code == 404)
    check('GET /api/nonexistent returns 404', requests.get(f'{BASE}/api/nonexistent').status_code == 404)
    check('POST /api/nonexistent returns 404', requests.post(f'{BASE}/api/nonexistent').status_code == 404)

finally:
    server.terminate()
    print()
    print('-' * 62)
    status = 'ALL CLEAR' if failed == 0 else f'FAILURES DETECTED'
    print(f'  RESULT: {passed} passed | {failed} failed | {status}')
    print('=' * 62)
    print()
    sys.exit(0 if failed == 0 else 1)
