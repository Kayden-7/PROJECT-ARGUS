"""
Independent behavioral contract tests for argus.agent.
Authored by Codex (blind to the implementation's own tests) during the Phase 5-9
quality audit. Test logic unchanged; only run() summary aligned to RESULT format.
Run with: python tests/test_agent_independent.py
"""
import contextlib
import os
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

PROJECT_ROOT = os.environ.get("PROJECT_ARGUS_ROOT")
if not PROJECT_ROOT:
    PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from argus import agent
from argus import audit
from argus import templates

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def require(condition, message="assertion failed"):
    if not condition:
        raise AssertionError(message)


def require_equal(actual, expected, message=""):
    if actual != expected:
        detail = f"expected {expected!r}, got {actual!r}"
        raise AssertionError(f"{message}: {detail}" if message else detail)


def init_agent_schema(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE agent_proposals (
            id            TEXT PRIMARY KEY,
            proposal_json TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'PROPOSAL'
                          CHECK(status IN ('PROPOSAL','CONSUMED')),
            created_at    INTEGER NOT NULL
        );

        CREATE TABLE pending_executions (
            execution_id  TEXT PRIMARY KEY,
            approval_id   TEXT UNIQUE,
            action_type   TEXT NOT NULL,
            payload_json  TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'DRAFT_PENDING',
            draft_id      TEXT,
            message_id    TEXT,
            history_id    TEXT,
            owner_token   TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            status_reason TEXT,
            last_error    TEXT,
            approved_at   INTEGER NOT NULL,
            execute_after INTEGER NOT NULL,
            created_at    INTEGER NOT NULL DEFAULT 0,
            updated_at    INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE audit_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       INTEGER NOT NULL,
            event_type      TEXT NOT NULL,
            correlation_id  TEXT,
            action_type     TEXT,
            outcome         TEXT,
            reason          TEXT,
            idempotency_key TEXT UNIQUE,
            payload_json    TEXT NOT NULL,
            prev_entry_hash TEXT,
            entry_hash      TEXT NOT NULL
        );

        CREATE TABLE email_templates (
            id             TEXT PRIMARY KEY,
            contact        TEXT,
            action_type    TEXT,
            tone           TEXT NOT NULL,
            formality      TEXT NOT NULL,
            length_class   TEXT NOT NULL,
            greeting_style TEXT NOT NULL,
            signoff_style  TEXT NOT NULL,
            max_words      INTEGER NOT NULL,
            max_sentences  INTEGER NOT NULL,
            max_paragraphs INTEGER NOT NULL,
            avoid_phrases  TEXT,
            enabled        INTEGER NOT NULL DEFAULT 1,
            version        INTEGER NOT NULL DEFAULT 1,
            created_at     INTEGER NOT NULL,
            updated_at     INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


@contextlib.contextmanager
def isolated_db():
    modules = [agent, audit, templates]
    old_paths = {module: module.DATABASE for module in modules}
    with tempfile.TemporaryDirectory(prefix="argus-agent-independent-") as tmp:
        db_path = os.path.join(tmp, "argus-test.db")
        init_agent_schema(db_path)
        for module in modules:
            module.DATABASE = db_path
        try:
            yield db_path
        finally:
            for module, old_path in old_paths.items():
                module.DATABASE = old_path


@contextlib.contextmanager
def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def count_rows(db_path, table):
    with connect(db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


def assert_no_proposal_or_execution(db_path):
    require_equal(count_rows(db_path, "agent_proposals"), 0, "failure states must not store agent proposals")
    require_equal(count_rows(db_path, "pending_executions"), 0, "agent interpretation must not execute anything")


@contextlib.contextmanager
def patched_model(extract_output, draft_output="Thanks, I will take care of this."):
    old_extract = agent.extract_proposal
    old_draft = agent.draft_body

    def fake_extract(command):
        if isinstance(extract_output, Exception):
            raise extract_output
        return extract_output(command) if callable(extract_output) else extract_output

    def fake_draft(action_type, entities, style_block, intent):
        if isinstance(draft_output, Exception):
            raise draft_output
        return draft_output(action_type, entities, style_block, intent) if callable(draft_output) else draft_output

    agent.extract_proposal = fake_extract
    agent.draft_body = fake_draft
    try:
        yield
    finally:
        agent.extract_proposal = old_extract
        agent.draft_body = old_draft


@test
def internal_send_to_external_recipient_is_rederived_and_stored_as_external():
    extract = {
        "status": "PROPOSAL",
        "action_type": "email.send.internal",
        "entities": {"recipient": "outside@example.net", "subject": "Hello"},
        "intent": "send a short hello",
        "uncertainties": [],
    }
    with isolated_db() as db_path, patched_model(extract, draft_output="Hello, checking in as requested. Thanks."):
        result = agent.run_agent("Send outside@example.net a short hello")
        require_equal(result.get("agent_status"), "PROPOSAL")
        require_equal(result["proposal"]["action_type"], "email.send.external")
        require_equal(count_rows(db_path, "pending_executions"), 0, "agent must not execute stored proposals")

        stored = agent.load_proposal(result["agent_proposal_id"])
        require(stored is not None, "stored proposal should be loadable")
        require_equal(stored["action_type"], "email.send.external", "stored proposal should use code-derived externality")
        require_equal(stored["entities"]["recipient"], "outside@example.net")
        require("body" in stored["entities"], "draft body should be stored only after template validation")


@test
def needs_clarification_returns_agent_status_and_stores_nothing():
    extract = {
        "status": "NEEDS_CLARIFICATION",
        "uncertainties": ["recipient is ambiguous"],
    }
    with isolated_db() as db_path, patched_model(extract):
        result = agent.run_agent("Send that thing")
        require_equal(result.get("agent_status"), "AGENT_NEEDS_CLARIFICATION")
        require("agent_proposal_id" not in result, "clarification result must not expose executable proposal id")
        require("proposal" not in result, "clarification result must not expose executable proposal")
        assert_no_proposal_or_execution(db_path)


@test
def malformed_model_output_returns_output_invalid_and_stores_nothing():
    with isolated_db() as db_path, patched_model({"action_type": "email.send.external"}):
        result = agent.run_agent("Send an email")
        require_equal(result.get("agent_status"), "AGENT_OUTPUT_INVALID")
        require_equal(result.get("detail"), "malformed model output")
        require("agent_proposal_id" not in result)
        require("proposal" not in result)
        assert_no_proposal_or_execution(db_path)


@test
def unknown_action_type_returns_output_invalid_and_stores_nothing():
    extract = {
        "status": "PROPOSAL",
        "action_type": "email.teleport",
        "entities": {"recipient": "outside@example.net"},
        "intent": "impossible action",
        "uncertainties": [],
    }
    with isolated_db() as db_path, patched_model(extract):
        result = agent.run_agent("Teleport this email")
        require_equal(result.get("agent_status"), "AGENT_OUTPUT_INVALID")
        require_equal(result.get("detail"), "unknown action_type")
        require("agent_proposal_id" not in result)
        require("proposal" not in result)
        assert_no_proposal_or_execution(db_path)


@test
def drafted_body_that_violates_template_returns_needs_clarification():
    extract = {
        "status": "PROPOSAL",
        "action_type": "email.send.external",
        "entities": {"recipient": "outside@example.net", "subject": "Hello"},
        "intent": "send a concise hello",
        "uncertainties": [],
    }
    too_long_body = " ".join(f"word{i}" for i in range(121))
    with isolated_db() as db_path, patched_model(extract, draft_output=too_long_body):
        result = agent.run_agent("Send outside@example.net a concise hello")
        require_equal(result.get("agent_status"), "AGENT_NEEDS_CLARIFICATION")
        require_equal(result.get("detail"), "draft did not fit the template")
        require("exceeds max_words" in result.get("failures", []), result)
        require("agent_proposal_id" not in result)
        require("proposal" not in result)
        assert_no_proposal_or_execution(db_path)


def run():
    failures = []
    for fn in TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:
            failures.append(fn.__name__)
            print(f"FAIL {fn.__name__}: {exc}")
            traceback.print_exc()
    passed = len(TESTS) - len(failures)
    print(f"RESULT: {passed} passed | {len(failures)} failed | "
          f"{'ALL CLEAR' if not failures else 'FAILURES DETECTED'}")
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    run()
