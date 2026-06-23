# -*- coding: utf-8 -*-
"""
ARGUS Phase 8 Tests — Part 3/7: Atomic admission (dedup + rate limit)
Run standalone: python tests/test_phase_8_admission.py

Exercises argus.admission.admit() and proposal_hash() directly against a
THROWAWAY temp DB. Three angles: Normal · Hacker (adversarial) · Strict Teacher.
"""
import os, sys, sqlite3, tempfile, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import argus.db as dbmod
import argus.admission as adm

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
    adm.DATABASE = path
    import argus.audit as audit
    audit.DATABASE = path
    _paths.append(path)
    return path

def cleanup():
    for p in _paths:
        for ext in ('', '-wal', '-shm'):
            try: os.remove(p + ext)
            except OSError: pass

def raw(path):
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row; return c

def P(action="email.archive", **entities):
    return {"action_type": action, "entities": entities, "intent": "x"}


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 1 — NORMAL
# ─────────────────────────────────────────────────────────────────────────────
def test_normal():
    sec('NORMAL — admit, duplicate, distinct, rate counting')
    path = fresh_db()

    r1 = adm.admit(P(email_id="m1"))
    check('first admit succeeds', r1["admitted"] is True and r1.get("proposal_id"), got=r1)
    # proposal actually stored
    c = raw(path)
    stored = c.execute("SELECT COUNT(*) FROM agent_proposals WHERE id=?", (r1["proposal_id"],)).fetchone()[0]
    check('proposal row stored', stored == 1)
    check('dedup row claimed', c.execute("SELECT COUNT(*) FROM proposal_dedup").fetchone()[0] == 1)
    check('rate slot reserved', c.execute("SELECT COALESCE(SUM(count),0) FROM rate_limits").fetchone()[0] == 1)
    c.close()

    r2 = adm.admit(P(email_id="m1"))  # identical within 60s
    check('identical re-admit -> DUPLICATE_SUPPRESSED', r2.get("reason") == "DUPLICATE_SUPPRESSED", got=r2)
    check('duplicate returns existing id', r2.get("existing_proposal_id") == r1["proposal_id"])

    c = raw(path)
    check('duplicate did NOT consume a rate slot (still 1)',
          c.execute("SELECT COALESCE(SUM(count),0) FROM rate_limits").fetchone()[0] == 1)
    c.close()

    r3 = adm.admit(P(email_id="m2"))  # distinct target
    check('distinct proposal admits', r3["admitted"] is True)
    c = raw(path)
    check('distinct admit consumed a second slot',
          c.execute("SELECT COALESCE(SUM(count),0) FROM rate_limits").fetchone()[0] == 2)
    c.close()


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 2 — HACKER
# ─────────────────────────────────────────────────────────────────────────────
def test_hacker():
    sec('HACKER — rate ceiling, hash robustness, audit-on-deny')
    path = fresh_db()

    # consume the whole budget with distinct proposals
    for i in range(adm.BUSINESS_LIMIT):
        r = adm.admit(P(email_id=f"x{i}"))
        check(f'admit {i} within budget', r["admitted"] is True) if i in (0, adm.BUSINESS_LIMIT - 1) else None
    over = adm.admit(P(email_id="over"))
    check('11th distinct action -> RATE_LIMIT_EXCEEDED', over.get("reason") == "RATE_LIMIT_EXCEEDED", got=over)
    check('rate response carries retry_at (int)', isinstance(over.get("retry_at"), int))

    c = raw(path)
    check('rate-limited request created NO new proposal (still LIMIT)',
          c.execute("SELECT COUNT(*) FROM agent_proposals").fetchone()[0] == adm.BUSINESS_LIMIT)
    check('rate-limited request created NO dedup claim for it',
          c.execute("SELECT COUNT(*) FROM proposal_dedup").fetchone()[0] == adm.BUSINESS_LIMIT)
    check('DUPLICATE/RATE denials are audited',
          c.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='RATE_LIMIT_EXCEEDED'").fetchone()[0] >= 1)
    c.close()

    # hash robustness
    sec('HACKER — proposal_hash canonicalization')
    h = adm.proposal_hash
    check('display-name vs bare address hash the same',
          h(P("email.reply", recipient="Bob <b@x.com>", body="hi")) ==
          h(P("email.reply", recipient="b@x.com", body="hi")))
    check('case/whitespace in recipient normalized',
          h(P("email.reply", recipient="B@X.COM", body="hi")) ==
          h(P("email.reply", recipient="b@x.com", body="hi")))
    check('different body -> different hash (exact, not semantic)',
          h(P("email.reply", recipient="b@x.com", body="hi")) !=
          h(P("email.reply", recipient="b@x.com", body="hello")))
    check('different action, same recipient -> different hash',
          h(P("email.reply", recipient="b@x.com")) != h(P("email.forward", recipient="b@x.com")))
    check('distinct non-body actions do NOT collapse (email_id differs)',
          h(P("email.archive", email_id="a")) != h(P("email.archive", email_id="b")))
    check('move with different destination -> different hash',
          h(P("email.move", email_id="a", destination="Work")) !=
          h(P("email.move", email_id="a", destination="Home")))


# ─────────────────────────────────────────────────────────────────────────────
# ANGLE 3 — STRICT TEACHER
# ─────────────────────────────────────────────────────────────────────────────
def test_strict_teacher():
    sec('STRICT — atomicity, expiry, DB clock, per-user scope')
    path = fresh_db()

    # expired dedup row must NOT suppress (and must be overwritten, not duplicated)
    c = raw(path)
    c.execute("INSERT INTO proposal_dedup (user_id, proposal_hash, proposal_id, created_at, expires_at) "
              "VALUES ('owner', ?, 'old', 0, 1)", (adm.proposal_hash(P(email_id="exp")),))
    c.commit(); c.close()
    r = adm.admit(P(email_id="exp"))
    check('expired dedup row does not suppress', r["admitted"] is True, got=r)
    c = raw(path)
    check('expired row overwritten, not duplicated (1 row for hash)',
          c.execute("SELECT COUNT(*) FROM proposal_dedup WHERE proposal_hash=?",
                    (adm.proposal_hash(P(email_id="exp")),)).fetchone()[0] == 1)
    c.close()

    # per-user category isolation
    fresh_db()
    adm.admit(P(email_id="u"), user_id="alice")
    c = raw(_paths[-1])
    cats = {row[0] for row in c.execute("SELECT DISTINCT action_category FROM rate_limits")}
    check('rate bucket keyed per user (business:alice)', "business:alice" in cats, got=cats)
    c.close()

    # DB-owned clock: admit takes no `now` argument (signature check)
    import inspect
    params = list(inspect.signature(adm.admit).parameters)
    check('admit() exposes no production `now` arg', "now" not in params, got=params)

    # atomicity: a forced audit failure rolls back the whole admission
    sec('STRICT — audit failure fails closed (full rollback)')
    path = fresh_db()
    import argus.audit as audit
    orig = audit.record
    audit.record = lambda *a, **k: {"recorded": False, "error": "forced"}
    try:
        r = adm.admit(P(email_id="boom"))
        check('audit failure -> not admitted (AUDIT_WRITE_FAILED)', r.get("reason") == "AUDIT_WRITE_FAILED", got=r)
        c = raw(path)
        check('rollback: no proposal stored', c.execute("SELECT COUNT(*) FROM agent_proposals").fetchone()[0] == 0)
        check('rollback: no dedup claim', c.execute("SELECT COUNT(*) FROM proposal_dedup").fetchone()[0] == 0)
        check('rollback: no rate slot', c.execute("SELECT COALESCE(SUM(count),0) FROM rate_limits").fetchone()[0] == 0)
        c.close()
    finally:
        audit.record = orig


if __name__ == '__main__':
    print('=' * 64)
    print('  ARGUS Phase 8 — Part 3/7: Atomic admission tests')
    print('=' * 64)
    try:
        test_normal()
        test_hacker()
        test_strict_teacher()
    finally:
        cleanup()
    print('\n' + '=' * 64)
    total = passed + failed
    rate = (passed / total * 100) if total else 0.0
    status = 'ALL PASS' if failed == 0 else 'FAILURES'
    print(f'  RESULT: {passed} passed | {failed} failed | {status} ({rate:.1f}%)')
    print('=' * 64)
    sys.exit(1 if failed else 0)
