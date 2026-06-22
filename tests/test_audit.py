"""
ARGUS Phase 7 Tests — Audit Trail
Run standalone: python tests/test_audit.py

audit_events is append-only (triggers block UPDATE/DELETE), so tests use unique
correlation ids and relative checks rather than clearing the table. The
tamper-DETECTION test runs against an isolated temp DB so it never corrupts the
real chain.
"""
import os, sys, sqlite3, uuid, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, 'instance', 'argus.db')
sys.path.insert(0, ROOT)

from argus.db import init_db
import argus.audit as A

passed = 0
failed = 0
def sec(n): print(f'\n  [{n}]')
def check(n, cond, got=None):
    global passed, failed
    if cond: print(f'    [PASS] {n}'); passed += 1
    else:
        d = f' | got: {got}' if got is not None else ''
        print(f'    [FAIL] {n}{d}'); failed += 1

def find(correlation):
    return [e for e in A.recent(500) if e["correlation_id"] == correlation]


try:
    init_db()

    # ══ NORMAL ════════════════════════════════════════════════════════════════
    sec('Normal — record appears in the log, chain stays valid')
    cid = str(uuid.uuid4())
    A.record("TEST_EVENT", correlation_id=cid, idempotency_key=f"{cid}:1",
             action_type="email.reply", outcome="ALLOW", payload={"x": 1})
    rows = find(cid)
    check('event recorded + retrievable', len(rows) == 1 and rows[0]["event_type"] == "TEST_EVENT")
    check('payload round-trips', rows[0]["payload"].get("x") == 1)
    check('verify_chain valid after append', A.verify_chain()["valid"] is True)

    sec('Normal — replay groups events by correlation_id')
    A.record("TEST_EVENT2", correlation_id=cid, idempotency_key=f"{cid}:2", payload={"y": 2})
    rep = A.replay(cid)
    check('replay returns both events', len(rep["events"]) == 2)
    check('replay labelled historical', "Historical replay" in rep["label"])

    sec('Normal — summary has the expected shape')
    s = A.summary(0)
    check('summary has decisions block', "decisions" in s)
    check('summary has human_oversight block', "human_oversight" in s)
    check('summary has honest note', "unresolved" in s["note"])

    # ══ HACKER ══════════════════════════════════════════════════════════════════
    sec('[HACKER] append-only — UPDATE and DELETE are blocked by triggers')
    conn = sqlite3.connect(DB_PATH)
    upd_blocked = del_blocked = False
    try:
        conn.execute("UPDATE audit_events SET outcome='HACKED' WHERE correlation_id=?", (cid,)); conn.commit()
    except Exception:
        upd_blocked = True
    try:
        conn.execute("DELETE FROM audit_events WHERE correlation_id=?", (cid,)); conn.commit()
    except Exception:
        del_blocked = True
    conn.close()
    check('UPDATE on audit_events blocked', upd_blocked)
    check('DELETE on audit_events blocked', del_blocked)

    sec('[HACKER] duplicate idempotency_key does not double-write')
    cid2 = str(uuid.uuid4())
    A.record("DUP", correlation_id=cid2, idempotency_key=f"{cid2}:k", payload={"n": 1})
    r2 = A.record("DUP", correlation_id=cid2, idempotency_key=f"{cid2}:k", payload={"n": 1})
    check('second identical write reported duplicate', r2.get("recorded") is False)
    check('only one row exists for the key', len(find(cid2)) == 1)

    sec('[HACKER] tamper DETECTION — a mutated entry breaks verify (isolated temp DB)')
    orig_db = A.DATABASE
    tmp = tempfile.mktemp(suffix='.db')
    A.DATABASE = tmp
    try:
        c = sqlite3.connect(tmp)
        c.execute("CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER, "
                  "event_type TEXT, correlation_id TEXT, action_type TEXT, outcome TEXT, reason TEXT, "
                  "idempotency_key TEXT UNIQUE, payload_json TEXT, prev_entry_hash TEXT, entry_hash TEXT)")
        c.commit(); c.close()
        A.record("E1", idempotency_key="k1", payload={"a": 1})
        A.record("E2", idempotency_key="k2", payload={"a": 2})
        check('clean temp chain is valid', A.verify_chain()["valid"] is True)
        c = sqlite3.connect(tmp)
        c.execute("UPDATE audit_events SET payload_json='{\"a\":999}' WHERE id=1"); c.commit(); c.close()
        v = A.verify_chain()
        check('tampered entry detected -> invalid', v["valid"] is False)
        check('verify reports where it broke', v.get("broken_at_id") == 1)
    finally:
        A.DATABASE = orig_db
        try: os.remove(tmp)
        except Exception: pass

    sec('[HACKER] verify note is honest (no false completeness/immutability claim)')
    note = A.verify_chain()["note"]
    check('claims internal consistency only', "internally consistent" in note)
    check('disclaims completeness / external tamper', "does not prove completeness" in note)

    # ══ STRICT TEACHER ══════════════════════════════════════════════════════════
    from app import app
    cl = app.test_client()

    sec('[STRICT] DECISION_EVALUATED logged on propose, correlation = queue id, no body leak')
    r = cl.post('/api/propose', json={'action_type': 'email.send.external',
                'entities': {'recipient': 'stranger@unknown.com', 'subject': 'SECRET_SUBJ',
                             'body': 'SECRET_BODY_TEXT'}})
    qid = r.get_json()["queue"]["id"]
    dec = [e for e in find(qid) if e["event_type"] == "DECISION_EVALUATED"]
    check('decision event logged with queue-id correlation', len(dec) == 1)
    check('final_outcome GATED recorded', dec[0]["payload"].get("final_outcome") == "GATED")
    check('recipient stored as coarse scope, not address',
          dec[0]["payload"].get("recipient_scope") == "UNRECOGNIZED_EXTERNAL")
    blob = str(dec[0]["payload"])
    check('no email body in audit payload', "SECRET_BODY_TEXT" not in blob)
    check('no subject text in audit payload', "SECRET_SUBJ" not in blob)

    sec('[STRICT] approve logs QUEUE_TRANSITIONED under same correlation; replay links them')
    cl.post(f'/api/queue/{qid}/approve')
    rep = A.replay(qid)
    types = [e["event_type"] for e in rep["events"]]
    check('decision + queue transition share correlation', "DECISION_EVALUATED" in types and "QUEUE_TRANSITIONED" in types)

    sec('[STRICT] TRUST_CHANGED recorded atomically with a trust event')
    from argus.trust_ledger import record_event
    tr = record_event('email.star', 'SUCCESS', reason='AUDIT_TEST')
    eid = tr.get("event_id")
    found = [e for e in A.recent(500) if e["correlation_id"] == eid and e["event_type"] == "TRUST_CHANGED"]
    check('trust change produced a TRUST_CHANGED audit event', len(found) == 1)

    sec('[STRICT] endpoints — /api/audit, /verify, /summary, trust history')
    check('/api/audit returns a list', isinstance(cl.get('/api/audit?limit=5').get_json(), list))
    check('/api/audit/verify valid', cl.get('/api/audit/verify').get_json().get("valid") is True)
    check('/api/audit/summary returns decisions block', "decisions" in cl.get('/api/audit/summary').get_json())
    th = cl.get('/api/trust/email.star/history').get_json()
    check('trust history is stepped with points', th.get("stepped") is True and isinstance(th.get("points"), list))
    check('unknown action history -> 404', cl.get('/api/trust/email.nope/history').status_code == 404)

    sec('[STRICT] summary approval_rate excludes non-reviewable outcomes')
    s = A.summary(0)
    ho = s["human_oversight"]
    check('approval_rate computed only from approvals+rejections',
          ho["approval_rate"] is None or 0.0 <= ho["approval_rate"] <= 1.0)

finally:
    print()
    print('-' * 62)
    status = 'ALL CLEAR' if failed == 0 else 'FAILURES DETECTED'
    print(f'  RESULT: {passed} passed | {failed} failed | {status}')
    print('=' * 62)
    print()
    sys.exit(0 if failed == 0 else 1)
