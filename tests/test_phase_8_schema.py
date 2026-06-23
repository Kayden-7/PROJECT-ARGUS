# -*- coding: utf-8 -*-
"""
ARGUS Phase 8 Tests — Part 1/7: Fail-safe schema migration
Run standalone: python tests/test_phase_8_schema.py

Covers the schema layer only (db.py): new tables, evolved status CHECKs, the
two-counter columns, the UNIQUE(approval_id, approval_generation) swap, the
500-char reason caps, HARD_STOP_EPOCH seed, and the rebuild migration of an
existing pre-Phase-8 DB (atomicity / idempotency / data preservation).

All tests run against THROWAWAY temp DBs — the real instance/argus.db is never
touched. Three angles: Normal · Hacker (adversarial) · Strict Teacher (nitpick).
"""
import os, sys, sqlite3, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import argus.db as dbmod
from argus.db import APPROVAL_QUEUE_DDL, PENDING_EXECUTIONS_DDL

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

_paths = {}

def fresh_db():
    """Build a fresh Phase-8 DB at a temp path and return a connection to it."""
    fd, path = tempfile.mkstemp(suffix='.db'); os.close(fd); os.remove(path)
    dbmod.DATABASE = path
    dbmod.init_db()
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row
    _paths[id(c)] = path
    return c

def drop(c):
    p = _paths.pop(id(c), None); c.close()
    if not p: return
    for ext in ('', '-wal', '-shm'):
        try: os.remove(p + ext)
        except OSError: pass

def old_schema_db_with_data():
    """A synthetic PRE-Phase-8 DB (old status CHECKs, old single-col UNIQUE) with
    one approval_queue + one pending_executions row, to exercise the rebuild."""
    fd, path = tempfile.mkstemp(suffix='.db'); os.close(fd)
    o = sqlite3.connect(path)
    o.executescript('''
      CREATE TABLE approval_queue (
         id TEXT PRIMARY KEY, proposal_json TEXT NOT NULL, decision_json TEXT NOT NULL,
         status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED','EXPIRED','MANUAL_REVIEW','EXECUTED','CANCELLED')),
         created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, approved_at INTEGER,
         updated_at INTEGER NOT NULL, status_reason TEXT, execution_id TEXT);
      CREATE TABLE pending_executions (
         execution_id TEXT PRIMARY KEY, approval_id TEXT UNIQUE, action_type TEXT NOT NULL,
         payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'DRAFT_PENDING'
           CHECK(status IN ('DRAFT_PENDING','DRAFT_READY','SENDING','COMPLETED','MANUAL_REVIEW','FAILED')),
         draft_id TEXT, message_id TEXT, history_id TEXT, owner_token TEXT,
         attempt_count INTEGER NOT NULL DEFAULT 0, status_reason TEXT, last_error TEXT,
         approved_at INTEGER NOT NULL, execute_after INTEGER NOT NULL,
         created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0);
      CREATE UNIQUE INDEX idx_pending_approval ON pending_executions(approval_id);
    ''')
    o.execute("INSERT INTO approval_queue (id,proposal_json,decision_json,status,created_at,expires_at,updated_at,status_reason) "
              "VALUES ('q1','{}','{}','APPROVED',1,2,3,'keep me')")
    o.execute("INSERT INTO pending_executions (execution_id,approval_id,action_type,payload_json,status,approved_at,execute_after) "
              "VALUES ('x1','q1','email.send','{}','COMPLETED',1,2)")
    o.commit(); o.close()
    return path


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 1 — NORMAL: the happy-path schema exists and behaves
# ─────────────────────────────────────────────────────────────────────────────
def test_normal():
    sec('NORMAL — fresh schema shape & basic inserts')
    c = fresh_db()
    try:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ('private_contacts', 'proposal_dedup', 'queue_transition_attempts'):
            check(f'new table exists: {t}', t in tables)

        aq_sql = c.execute("SELECT sql FROM sqlite_master WHERE name='approval_queue'").fetchone()[0]
        for s in ('HELD', 'MANUAL_REVIEW_TIMEOUT', 'TRANSITION_LOCKED'):
            check(f'approval_queue CHECK accepts {s}', s in aq_sql)
        pe_sql = c.execute("SELECT sql FROM sqlite_master WHERE name='pending_executions'").fetchone()[0]
        for s in ('HELD', 'SUPERSEDED'):
            check(f'pending_executions CHECK accepts {s}', s in pe_sql)

        epoch = c.execute("SELECT value FROM system_state WHERE key='HARD_STOP_EPOCH'").fetchone()
        check('HARD_STOP_EPOCH seeded to 0', epoch is not None and epoch[0] == '0', got=epoch and epoch[0])
        hs = c.execute("SELECT value FROM system_state WHERE key='SYSTEM_HARD_STOP'").fetchone()
        check('SYSTEM_HARD_STOP seeded to 0', hs is not None and hs[0] == '0')

        # insert a row in each new status — must be accepted
        c.execute("INSERT INTO approval_queue (id,proposal_json,decision_json,status,created_at,expires_at,updated_at) "
                  "VALUES ('a','{}','{}','HELD',0,0,0)")
        check('insert approval_queue status=HELD ok', True)
        c.execute("INSERT INTO private_contacts (normalized_email,created_at,updated_at) VALUES ('boss@corp.com',0,0)")
        check('insert private_contact ok', True)
        c.execute("INSERT INTO proposal_dedup (user_id,proposal_hash,proposal_id,created_at,expires_at) VALUES ('u','h','p',0,60)")
        check('insert proposal_dedup ok', True)
        c.commit()
    finally:
        drop(c)

    sec('NORMAL — supersede coexistence (the two-counter invariant)')
    c = fresh_db()
    try:
        c.execute("INSERT INTO pending_executions (execution_id,approval_id,action_type,payload_json,status,approval_generation,approved_at,execute_after) "
                  "VALUES ('e0','ap1','email.send','{}','SUPERSEDED',0,0,0)")
        c.execute("INSERT INTO pending_executions (execution_id,approval_id,action_type,payload_json,status,approval_generation,approved_at,execute_after) "
                  "VALUES ('e1','ap1','email.send','{}','DRAFT_PENDING',1,0,0)")
        c.commit()
        n = c.execute("SELECT COUNT(*) FROM pending_executions WHERE approval_id='ap1'").fetchone()[0]
        check('SUPERSEDED gen0 + fresh gen1 coexist for same approval_id', n == 2, got=n)
    finally:
        drop(c)


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 2 — HACKER: adversarial inputs must be rejected by constraints
# ─────────────────────────────────────────────────────────────────────────────
def test_hacker():
    sec('HACKER — constraints reject malformed / abusive writes')
    c = fresh_db()
    try:
        def rejected(label, sql, params=()):
            try:
                c.execute(sql, params); c.commit()
                check(label, False, got='insert SUCCEEDED (should have failed)')
            except sqlite3.IntegrityError:
                check(label, True)
            except sqlite3.OperationalError as e:
                check(label, True, got=str(e))
            finally:
                c.rollback()

        rejected('reject bogus approval_queue status',
                 "INSERT INTO approval_queue (id,proposal_json,decision_json,status,created_at,expires_at,updated_at) "
                 "VALUES ('h1','{}','{}','PWNED',0,0,0)")
        rejected('reject bogus pending_executions status',
                 "INSERT INTO pending_executions (execution_id,approval_id,action_type,payload_json,status,approved_at,execute_after) "
                 "VALUES ('h2','a','x','{}','HACKED',0,0)")
        rejected('reject status_reason > 500 chars (approval_queue)',
                 "INSERT INTO approval_queue (id,proposal_json,decision_json,status,created_at,expires_at,updated_at,status_reason) "
                 "VALUES ('h3','{}','{}','REJECTED',0,0,0,?)", ('x' * 501,))
        rejected('reject transition_lock_reason > 500 chars',
                 "INSERT INTO approval_queue (id,proposal_json,decision_json,status,created_at,expires_at,updated_at,transition_lock_reason) "
                 "VALUES ('h4','{}','{}','TRANSITION_LOCKED',0,0,0,?)", ('y' * 501,))
        rejected('reject private_contacts.enabled outside 0/1',
                 "INSERT INTO private_contacts (normalized_email,enabled,created_at,updated_at) VALUES ('z@z.com',7,0,0)")

        # duplicate (approval_id, approval_generation) must be blocked by composite UNIQUE
        c.execute("INSERT INTO pending_executions (execution_id,approval_id,action_type,payload_json,status,approval_generation,approved_at,execute_after) "
                  "VALUES ('ok','dup','x','{}','DRAFT_PENDING',0,0,0)"); c.commit()
        rejected('reject duplicate (approval_id, generation)',
                 "INSERT INTO pending_executions (execution_id,approval_id,action_type,payload_json,status,approval_generation,approved_at,execute_after) "
                 "VALUES ('ok2','dup','x','{}','DRAFT_PENDING',0,0,0)")
        # but SAME approval_id with a DIFFERENT generation must still be allowed
        try:
            c.execute("INSERT INTO pending_executions (execution_id,approval_id,action_type,payload_json,status,approval_generation,approved_at,execute_after) "
                      "VALUES ('ok3','dup','x','{}','DRAFT_PENDING',1,0,0)"); c.commit()
            check('same approval_id, different generation still allowed', True)
        except Exception as e:
            check('same approval_id, different generation still allowed', False, got=str(e)); c.rollback()

        # first insert succeeds; the second identical address must be rejected by UNIQUE
        c.execute("INSERT INTO private_contacts (normalized_email,created_at,updated_at) VALUES ('once@x.com',0,0)"); c.commit()
        rejected('reject duplicate private_contacts.normalized_email',
                 "INSERT INTO private_contacts (normalized_email,created_at,updated_at) VALUES ('once@x.com',0,0)")
    finally:
        drop(c)


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 3 — STRICT TEACHER: exact structure + migration correctness, nitpicked
# ─────────────────────────────────────────────────────────────────────────────
def test_strict_teacher():
    sec('STRICT — exact columns, indexes, keys')
    c = fresh_db()
    try:
        aq_cols = {r[1] for r in c.execute("PRAGMA table_info(approval_queue)")}
        for col in ('version', 'approval_epoch', 'approval_generation',
                    'manual_review_generation', 'manual_review_started_at',
                    'transition_lock_reason', 'transition_locked_at'):
            check(f'approval_queue has column {col}', col in aq_cols)

        pe_cols = {r[1] for r in c.execute("PRAGMA table_info(pending_executions)")}
        check('pending_executions has approval_epoch', 'approval_epoch' in pe_cols)
        check('pending_executions has approval_generation', 'approval_generation' in pe_cols)

        ss_cols = {r[1] for r in c.execute("PRAGMA table_info(system_state)")}
        for col in ('updated_at', 'updated_by', 'reason'):
            check(f'system_state has metadata column {col}', col in ss_cols)

        pe_sql = c.execute("SELECT sql FROM sqlite_master WHERE name='pending_executions'").fetchone()[0]
        check('pending_executions declares composite UNIQUE(approval_id, approval_generation)',
              'UNIQUE(approval_id, approval_generation)' in pe_sql)
        idx = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        check('old idx_pending_approval is absent on fresh DB', 'idx_pending_approval' not in idx)

        # proposal_dedup PK exactly (user_id, proposal_hash)
        pk = [r[1] for r in c.execute("PRAGMA table_info(proposal_dedup)") if r[5] > 0]
        check('proposal_dedup PK == (user_id, proposal_hash)', set(pk) == {'user_id', 'proposal_hash'}, got=pk)

        # DDL constants are the single source (used by both fresh + rebuild)
        check('APPROVAL_QUEUE_DDL constant carries new statuses',
              all(s in APPROVAL_QUEUE_DDL for s in ('HELD', 'MANUAL_REVIEW_TIMEOUT', 'TRANSITION_LOCKED')))
        check('PENDING_EXECUTIONS_DDL constant carries SUPERSEDED + composite UNIQUE',
              'SUPERSEDED' in PENDING_EXECUTIONS_DDL and 'UNIQUE(approval_id, approval_generation)' in PENDING_EXECUTIONS_DDL)
    finally:
        drop(c)

    sec('STRICT — rebuild migration: data preserved, atomic, idempotent')
    path = old_schema_db_with_data()
    try:
        dbmod.DATABASE = path
        dbmod.init_db()   # run 1 — rebuild
        dbmod.init_db()   # run 2 — must be a no-op (marker guard)
        d = sqlite3.connect(path); d.row_factory = sqlite3.Row

        aq_n = d.execute("SELECT COUNT(*) FROM approval_queue").fetchone()[0]
        pe_n = d.execute("SELECT COUNT(*) FROM pending_executions").fetchone()[0]
        check('migration preserved approval_queue row', aq_n == 1, got=aq_n)
        check('migration preserved pending_executions row', pe_n == 1, got=pe_n)

        row = d.execute("SELECT status,status_reason FROM approval_queue WHERE id='q1'").fetchone()
        check('migration preserved old data verbatim', tuple(row) == ('APPROVED', 'keep me'), got=tuple(row))
        defaults = d.execute("SELECT version,approval_epoch,approval_generation FROM approval_queue WHERE id='q1'").fetchone()
        check('migration applied new-column DEFAULTs (0,0,0)', tuple(defaults) == (0, 0, 0), got=tuple(defaults))

        aq_sql = d.execute("SELECT sql FROM sqlite_master WHERE name='approval_queue'").fetchone()[0]
        pe_sql = d.execute("SELECT sql FROM sqlite_master WHERE name='pending_executions'").fetchone()[0]
        check('migrated approval_queue CHECK upgraded', 'TRANSITION_LOCKED' in aq_sql)
        check('migrated pending_executions CHECK upgraded', 'SUPERSEDED' in pe_sql)

        idx = {r[0] for r in d.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        check('old single-col idx_pending_approval dropped by migration', 'idx_pending_approval' not in idx)

        stray = [r[0] for r in d.execute("SELECT name FROM sqlite_master WHERE name LIKE '%\\_old' ESCAPE '\\'")]
        check('no stray *_old tables (atomic + idempotent)', not stray, got=stray)
        d.close()
    finally:
        for ext in ('', '-wal', '-shm'):
            try: os.remove(path + ext)
            except OSError: pass


if __name__ == '__main__':
    print('=' * 64)
    print('  ARGUS Phase 8 — Part 1/7: Schema migration tests')
    print('=' * 64)
    test_normal()
    test_hacker()
    test_strict_teacher()
    print('\n' + '=' * 64)
    total = passed + failed
    rate = (passed / total * 100) if total else 0.0
    status = 'ALL PASS' if failed == 0 else 'FAILURES'
    print(f'  RESULT: {passed} passed | {failed} failed | {status} ({rate:.1f}%)')
    print('=' * 64)
    sys.exit(1 if failed else 0)
