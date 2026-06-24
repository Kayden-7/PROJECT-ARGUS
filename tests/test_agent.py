"""
ARGUS Phase 9 Tests — GPT-4o Agent Layer
Run standalone: python tests/test_agent.py

The two model calls (extract_proposal / draft_body) are mocked, so the suite
never hits the live OpenAI API. Three-angle: Normal + Hacker + Strict.
"""
import os, sys, sqlite3

# These tests verify agent INTERPRETATION (mocked model), not Phase 8 admission.
# Disable dedup/rate admission so repeated/identical mocked proposals aren't
# suppressed or throttled. Admission has its own suite (test_phase_8_admission).
os.environ["ARGUS_ADMISSION_ENABLED"] = "0"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, 'instance', 'argus.db')
sys.path.insert(0, ROOT)

from argus.db import init_db
import argus.agent as agent
from config import ALL_ACTIONS

passed = 0
failed = 0
def sec(n): print(f'\n  [{n}]')
def check(n, cond, got=None):
    global passed, failed
    if cond: print(f'    [PASS] {n}'); passed += 1
    else:
        d = f' | got: {got}' if got is not None else ''
        print(f'    [FAIL] {n}{d}'); failed += 1

def mock_extract(result):
    agent.extract_proposal = lambda command: (result(command) if callable(result) else result)
def mock_draft(text):
    agent.draft_body = lambda action_type, entities, style, intent: text

def db_count(tbl):
    db = sqlite3.connect(DB_PATH); n = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]; db.close(); return n


try:
    init_db()

    # ══ NORMAL ════════════════════════════════════════════════════════════════
    sec('Normal — FREE action command -> PROPOSAL, then confirm -> ALLOW')
    mock_extract({"status": "PROPOSAL", "action_type": "email.archive",
                  "entities": {"email_id": "m1"}, "intent": "archive it"})
    r = agent.run_agent("archive this email")
    check('agent_status PROPOSAL', r["agent_status"] == "PROPOSAL")
    check('returns agent_proposal_id', bool(r.get("agent_proposal_id")))
    check('proposal action_type correct', r["proposal"]["action_type"] == "email.archive")
    check('versions present', bool(r.get("agent_prompt_version")) and bool(r.get("taxonomy_version")))

    from app import app
    cl = app.test_client()
    rc = cl.post('/api/agent/confirm', json={"agent_proposal_id": r["agent_proposal_id"]})
    check('confirm routes to a decision', rc.get_json().get("decision") in ("ALLOW", "GATED", "BLOCK"))
    check('archive (FREE) confirmed -> ALLOW', rc.get_json().get("decision") == "ALLOW")

    sec('Normal — two-pass email drafting fills the body')
    mock_extract({"status": "PROPOSAL", "action_type": "email.send.external",
                  "entities": {"recipient": "x@unknown.com", "subject": "Hi"}, "intent": "say hi"})
    mock_draft("Hi there, thanks for your message.")
    r = agent.run_agent("email x@unknown.com saying hi")
    check('PROPOSAL produced', r["agent_status"] == "PROPOSAL")
    check('body filled by pass 2', r["proposal"]["entities"].get("body") == "Hi there, thanks for your message.")

    sec('Normal — taxonomy registry matches the API validator')
    reg = agent.action_registry()
    check('every registry action is a known action', all(a in ALL_ACTIONS for a in reg))
    check('every known action is in the registry', all(a in reg for a in ALL_ACTIONS))

    # ══ HACKER ══════════════════════════════════════════════════════════════════
    sec('[HACKER] NEEDS_CLARIFICATION carries no executable fields, stores nothing')
    before = db_count('agent_proposals')
    mock_extract({"status": "NEEDS_CLARIFICATION", "uncertainties": [{"field": "recipient"}]})
    r = agent.run_agent("reply to him")
    check('status AGENT_NEEDS_CLARIFICATION', r["agent_status"] == "AGENT_NEEDS_CLARIFICATION")
    check('no agent_proposal_id', "agent_proposal_id" not in r)
    check('no proposal field', "proposal" not in r)
    check('nothing persisted', db_count('agent_proposals') == before)

    sec('[HACKER] model failure / malformed output -> distinct non-executable states')
    def boom(cmd): raise RuntimeError("api down")
    agent.extract_proposal = boom
    check('API failure -> AGENT_UNAVAILABLE', agent.run_agent("do x")["agent_status"] == "AGENT_UNAVAILABLE")
    mock_extract({"action_type": "email.archive"})  # no "status"
    check('malformed output -> AGENT_OUTPUT_INVALID', agent.run_agent("x")["agent_status"] == "AGENT_OUTPUT_INVALID")
    mock_extract({"status": "PROPOSAL", "action_type": "email.nuke", "entities": {}})
    check('hallucinated action_type -> AGENT_OUTPUT_INVALID', agent.run_agent("x")["agent_status"] == "AGENT_OUTPUT_INVALID")

    sec('[HACKER] GPT internal/external label is NOT trusted (code re-derives)')
    mock_extract({"status": "PROPOSAL", "action_type": "email.send.internal",
                  "entities": {"recipient": "stranger@unknown.com", "subject": "S"}, "intent": "hi"})
    mock_draft("Short note.")
    r = agent.run_agent("send internal to stranger@unknown.com")
    check('mislabelled internal re-derived to external',
          r["proposal"]["action_type"] == "email.send.external", got=r["proposal"]["action_type"])

    sec('[HACKER] drafted body that violates the template -> clarification, not a proposal')
    mock_extract({"status": "PROPOSAL", "action_type": "email.send.external",
                  "entities": {"recipient": "x@unknown.com", "subject": "S"}, "intent": "hi"})
    mock_draft("word " * 400)  # far over the conservative default word limit
    r = agent.run_agent("send a long email")
    check('over-limit body -> AGENT_NEEDS_CLARIFICATION', r["agent_status"] == "AGENT_NEEDS_CLARIFICATION")

    sec('[HACKER] confirm with unknown / already-consumed id never executes')
    cl = app.test_client()
    check('unknown id -> 404', cl.post('/api/agent/confirm', json={"agent_proposal_id": "nope"}).status_code == 404)
    mock_extract({"status": "PROPOSAL", "action_type": "email.archive", "entities": {"email_id": "m1"}})
    pid = agent.run_agent("archive")["agent_proposal_id"]
    first = cl.post('/api/agent/confirm', json={"agent_proposal_id": pid})
    second = cl.post('/api/agent/confirm', json={"agent_proposal_id": pid})
    check('first confirm works', first.status_code == 200)
    check('second confirm (consumed) -> 404', second.status_code == 404)

    sec('[HACKER] run_agent never executes (no queue / execution side effects)')
    q_before, e_before = db_count('approval_queue'), db_count('pending_executions')
    mock_extract({"status": "PROPOSAL", "action_type": "email.delete", "entities": {"email_id": "m9"}})
    agent.run_agent("delete that email")
    check('no queue row from run_agent', db_count('approval_queue') == q_before)
    check('no execution row from run_agent', db_count('pending_executions') == e_before)

    # ══ STRICT TEACHER ══════════════════════════════════════════════════════════
    sec('[STRICT] versions present on every response shape')
    for r in [agent.run_agent(""),  # invalid
              (mock_extract({"status": "NEEDS_CLARIFICATION"}) or agent.run_agent("x"))]:
        check('response carries agent_prompt_version', r.get("agent_prompt_version") == agent.AGENT_PROMPT_VERSION)

    sec('[STRICT] empty command -> AGENT_OUTPUT_INVALID')
    check('empty command rejected', agent.run_agent("")["agent_status"] == "AGENT_OUTPUT_INVALID")
    check('whitespace command rejected', agent.run_agent("   ")["agent_status"] == "AGENT_OUTPUT_INVALID")

    sec('[STRICT] canonical proposal lifecycle PROPOSAL -> CONSUMED')
    mock_extract({"status": "PROPOSAL", "action_type": "email.archive", "entities": {"email_id": "m1"}})
    pid = agent.run_agent("archive")["agent_proposal_id"]
    db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
    st = db.execute("SELECT status FROM agent_proposals WHERE id=?", (pid,)).fetchone()["status"]; db.close()
    check('stored as PROPOSAL', st == "PROPOSAL")
    cl.post('/api/agent/confirm', json={"agent_proposal_id": pid})
    db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
    st2 = db.execute("SELECT status FROM agent_proposals WHERE id=?", (pid,)).fetchone()["status"]; db.close()
    check('marked CONSUMED after confirm', st2 == "CONSUMED")

    sec('[STRICT] GATED proposal confirmed -> GATED + queued')
    mock_extract({"status": "PROPOSAL", "action_type": "email.send.external",
                  "entities": {"recipient": "x@unknown.com", "subject": "S"}, "intent": "hi"})
    mock_draft("Short.")
    pid = agent.run_agent("send")["agent_proposal_id"]
    jr = cl.post('/api/agent/confirm', json={"agent_proposal_id": pid}).get_json()
    check('send to unknown domain confirmed -> GATED', jr["decision"] == "GATED")
    check('GATED response has a queue entry', jr.get("queue") is not None)

    sec('[STRICT] /demo/reset fails closed when not in demo mode')
    check('demo reset disabled by default -> 403', cl.post('/demo/reset').status_code == 403)
    from argus.demo import reset_demo
    # Seed some state that reset must actually clear, then reset.
    _seed = sqlite3.connect(DB_PATH)
    _seed.execute("INSERT INTO audit_events (timestamp,event_type,payload_json,entry_hash) "
                  "VALUES (1,'TRUST_CHANGED','{}','seed-hash')")
    _seed.execute("UPDATE system_state SET value='5' WHERE key='HARD_STOP_EPOCH'")
    _seed.commit(); _seed.close()
    out = reset_demo()
    check('reset_demo() returns demo_run_id', bool(out.get("demo_run_id")))
    check('reset clears agent_proposals', db_count('agent_proposals') == 0)
    # Regression: reset must wipe the LIVE audit table (audit_events), not the
    # dead audit_log; must clear Phase 8 admission/queue tables; must reset the
    # hard-stop epoch; and must leave audit_events append-only again.
    # Part 7: reset restarts the hash chain with ONE genesis event
    # (DEMO_RESET_COMPLETED) — so the prior chain is gone and exactly one row remains.
    _ae = sqlite3.connect(DB_PATH); _ae.row_factory = sqlite3.Row
    _ae_rows = _ae.execute("SELECT event_type FROM audit_events").fetchall(); _ae.close()
    check('reset restarts audit_events with one genesis row', len(_ae_rows) == 1, got=len(_ae_rows))
    check('genesis row is DEMO_RESET_COMPLETED',
          _ae_rows and _ae_rows[0]["event_type"] == "DEMO_RESET_COMPLETED")
    check('reset clears proposal_dedup', db_count('proposal_dedup') == 0)
    check('reset clears rate_limits', db_count('rate_limits') == 0)
    check('reset clears queue_transition_attempts', db_count('queue_transition_attempts') == 0)
    _chk = sqlite3.connect(DB_PATH)
    _epoch = _chk.execute("SELECT value FROM system_state WHERE key='HARD_STOP_EPOCH'").fetchone()[0]
    check('reset returns HARD_STOP_EPOCH to 0', _epoch == '0', got=_epoch)
    # BEFORE DELETE fires per-row, so the guard needs a row present to trip.
    _chk.execute("INSERT INTO audit_events (timestamp,event_type,payload_json,entry_hash) "
                 "VALUES (1,'X','{}','guard-probe')")
    _chk.commit()
    _append_only = False
    try:
        _chk.execute("DELETE FROM audit_events")
    except sqlite3.Error:
        _append_only = True
    _chk.rollback(); _chk.close()
    check('audit_events append-only guard restored after reset', _append_only)
    # The guard-probe row above has a non-chained entry_hash; wipe it so the
    # shared instance DB is left with a clean, verifiable audit chain for any
    # suite that runs after this one (run_tests.py shares one DB file).
    reset_demo()

finally:
    print()
    print('-' * 62)
    status = 'ALL CLEAR' if failed == 0 else 'FAILURES DETECTED'
    print(f'  RESULT: {passed} passed | {failed} failed | {status}')
    print('=' * 62)
    print()
    sys.exit(0 if failed == 0 else 1)
