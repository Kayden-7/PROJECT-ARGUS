"""
Independent behavioral contract tests for argus.executor.
Authored by Codex (blind to the implementation's own tests) during the Phase 5-9
quality audit. Test logic unchanged; only run() summary aligned to RESULT format.
Run with: python tests/test_executor_independent.py
"""
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import types
import time
import traceback
from pathlib import Path

PROJECT_ROOT = os.environ.get("PROJECT_ARGUS_ROOT")
if not PROJECT_ROOT:
    PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from argus import audit
from argus import executor
from argus import trust_ledger

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def require_equal(actual, expected, message=""):
    if actual != expected:
        detail = f"expected {expected!r}, got {actual!r}"
        raise AssertionError(f"{message}: {detail}" if message else detail)


def init_executor_schema(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE system_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE approval_queue (
            id TEXT PRIMARY KEY,
            proposal_json TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED','EXPIRED','MANUAL_REVIEW','EXECUTED','CANCELLED')),
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            approved_at INTEGER,
            updated_at INTEGER NOT NULL,
            status_reason TEXT,
            execution_id TEXT
        );

        CREATE TABLE pending_executions (
            execution_id  TEXT PRIMARY KEY,
            approval_id   TEXT UNIQUE,
            action_type   TEXT NOT NULL,
            payload_json  TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'DRAFT_PENDING'
                          CHECK(status IN ('DRAFT_PENDING','DRAFT_READY','SENDING','COMPLETED','MANUAL_REVIEW','FAILED')),
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

        CREATE TABLE trust_current (
            action_type       TEXT PRIMARY KEY,
            trust_current     REAL NOT NULL CHECK(trust_current >= 0 AND trust_current <= 100),
            damping_remaining INTEGER NOT NULL DEFAULT 0,
            damping_streak    INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE trust_events (
            event_id        TEXT PRIMARY KEY,
            timestamp       INTEGER NOT NULL,
            action_type     TEXT NOT NULL,
            delta           REAL NOT NULL,
            reason          TEXT NOT NULL,
            resulting_trust REAL NOT NULL
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
        """
    )
    conn.execute("INSERT INTO system_state VALUES ('UNDO_WINDOW_SECONDS', '60')")
    conn.execute("INSERT INTO system_state VALUES ('OVERALL_TRUST_MODIFIER', '1.0')")
    conn.execute("INSERT INTO system_state VALUES ('ACTIVE_PROFILE', 'Balanced')")
    conn.execute(
        "INSERT INTO trust_current (action_type, trust_current, damping_remaining, damping_streak) VALUES (?, 40.0, 0, 0)",
        ("email.send.external",),
    )
    conn.commit()
    conn.close()


@contextlib.contextmanager
def isolated_db():
    modules = [executor, audit, trust_ledger]
    old_paths = {module: module.DATABASE for module in modules}
    with tempfile.TemporaryDirectory(prefix="argus-executor-independent-") as tmp:
        db_path = os.path.join(tmp, "argus-test.db")
        init_executor_schema(db_path)
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


def base_proposal(recipient="person@example.net"):
    return {
        "action_type": "email.send.external",
        "entities": {
            "recipient": recipient,
            "subject": "Contract test",
            "body": "Short body for the independent executor test.",
        },
    }


def insert_approved(db_path, approval_id="approval-1", proposal=None, approved_seconds_ago=60):
    now = int(time.time())
    proposal = proposal or base_proposal()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO approval_queue
                (id, proposal_json, decision_json, status, created_at, expires_at,
                 approved_at, updated_at, status_reason, execution_id)
            VALUES (?, ?, ?, 'APPROVED', ?, ?, ?, ?, NULL, NULL)
            """,
            (
                approval_id,
                json.dumps(proposal),
                json.dumps({"decision": "GATED"}),
                now - approved_seconds_ago,
                now + 300,
                now - approved_seconds_ago,
                now - approved_seconds_ago,
            ),
        )
        conn.commit()
    return approval_id


def insert_pending(db_path, execution_id="exec-1", approval_id="approval-1", status="DRAFT_READY",
                    draft_id="draft-1", owner_token=None):
    now = int(time.time())
    proposal = base_proposal()
    # Production rows always carry an owner_token by the time they reach
    # SENDING (set atomically by the DRAFT_READY claim) — default one here so
    # tests that inject a SENDING row directly match that real invariant.
    if owner_token is None and status == "SENDING":
        owner_token = f"token-{execution_id}"
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO pending_executions
                (execution_id, approval_id, action_type, payload_json, status, draft_id,
                 owner_token, attempt_count, approved_at, execute_after, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                approval_id,
                proposal["action_type"],
                json.dumps(proposal),
                status,
                draft_id,
                owner_token,
                1,
                now - 60,
                now - 30,
                now - 60,
                now - 60,
            ),
        )
        conn.commit()
    return execution_id


def pending_rows(db_path):
    with connect(db_path) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM pending_executions ORDER BY created_at, execution_id")]


def count_rows(db_path, table):
    with connect(db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


def queue_status(db_path, approval_id):
    with connect(db_path) as conn:
        row = conn.execute("SELECT status, execution_id FROM approval_queue WHERE id=?", (approval_id,)).fetchone()
        return dict(row) if row else None


class FakeGmail:
    def __init__(self, send_raises=False, draft_exists_returns=True, draft_exists_raises=False):
        self.created = []
        self.sent = []
        self.trashed = []
        self.recipients = {}
        self.send_raises = send_raises
        self.draft_exists_returns = draft_exists_returns
        self.draft_exists_raises = draft_exists_raises
        self.draft_exists_calls = []

    def get_history_id(self):
        return "history-1"

    def create_draft(self, to, subject, body, thread_id=None, in_reply_to=None):
        draft_id = f"draft-{len(self.created) + 1}"
        self.created.append({
            "to": to,
            "subject": subject,
            "body": body,
            "thread_id": thread_id,
            "in_reply_to": in_reply_to,
        })
        self.recipients[draft_id] = {"to": [to], "cc": [], "bcc": []}
        return draft_id

    def get_draft_recipients(self, draft_id):
        if draft_id not in self.recipients:
            raise AssertionError(f"test did not configure recipients for {draft_id}")
        return self.recipients[draft_id]

    def send_draft(self, draft_id):
        self.sent.append(draft_id)
        if self.send_raises:
            raise RuntimeError("simulated send uncertainty")
        return {"message_id": f"message-{len(self.sent)}"}

    def trash_message(self, message_id):
        self.trashed.append(message_id)
        return {"trashed": True}

    def classify_gmail_error(self, phase, exc):
        return {"class": "TEST_ERROR", "sub_reason": type(exc).__name__}

    def draft_exists(self, draft_id):
        self.draft_exists_calls.append(draft_id)
        if self.draft_exists_raises:
            raise RuntimeError("simulated drafts.get failure")
        return self.draft_exists_returns


@contextlib.contextmanager
def patched_gmail(fake):
    import argus

    fake_module = types.SimpleNamespace(
        get_history_id=fake.get_history_id,
        create_draft=fake.create_draft,
        get_draft_recipients=fake.get_draft_recipients,
        send_draft=fake.send_draft,
        trash_message=fake.trash_message,
        classify_gmail_error=fake.classify_gmail_error,
        draft_exists=fake.draft_exists,
    )
    sentinel = object()
    old_module = sys.modules.get("argus.gmail_client", sentinel)
    old_attr = getattr(argus, "gmail_client", sentinel)
    sys.modules["argus.gmail_client"] = fake_module
    setattr(argus, "gmail_client", fake_module)
    try:
        yield fake
    finally:
        if old_module is sentinel:
            sys.modules.pop("argus.gmail_client", None)
        else:
            sys.modules["argus.gmail_client"] = old_module
        if old_attr is sentinel:
            try:
                delattr(argus, "gmail_client")
            except AttributeError:
                pass
        else:
            setattr(argus, "gmail_client", old_attr)


@test
def approved_past_undo_window_promotes_to_exactly_one_pending_execution():
    with isolated_db() as db_path:
        approval_id = insert_approved(db_path)
        executor.promote_approved()
        executor.promote_approved()
        rows = pending_rows(db_path)
        require_equal(len(rows), 1, "promotion should be idempotent per approval")
        require_equal(rows[0]["approval_id"], approval_id)
        require_equal(rows[0]["status"], "DRAFT_PENDING")


@test
def happy_path_advances_draft_pending_to_ready_to_completed_with_one_send():
    with isolated_db() as db_path, patched_gmail(FakeGmail()) as fake:
        approval_id = insert_approved(db_path)

        executor.reconcile()
        first = pending_rows(db_path)[0]
        require_equal(first["status"], "DRAFT_READY")
        require_equal(len(fake.created), 1, "first reconcile should create exactly one draft")
        require_equal(len(fake.sent), 0, "first reconcile should not send yet")

        executor.reconcile()
        second = pending_rows(db_path)[0]
        require_equal(second["status"], "COMPLETED")
        require_equal(second["message_id"], "message-1")
        require_equal(len(fake.sent), 1, "second reconcile should send exactly once")
        require_equal(count_rows(db_path, "trust_events"), 1, "successful execution should write one trust event")
        require_equal(queue_status(db_path, approval_id)["status"], "EXECUTED")


@test
def sending_state_with_draft_gone_recovers_as_completed_without_resend():
    # Recovery from a crash mid-send must not GUESS — it checks. If Gmail has
    # already consumed the draft (gone == sent), that's a confirmed success,
    # not an unverified "crash": no false alarm, no re-send, trust written once.
    with isolated_db() as db_path, patched_gmail(FakeGmail(draft_exists_returns=False)) as fake:
        # approved_seconds_ago=0 keeps this APPROVED row's undo window from
        # having elapsed yet, so promote_approved() won't ALSO create a second,
        # unrelated pending_execution for it during this same reconcile() call.
        approval_id = insert_approved(db_path, approval_id="approval-recovered", approved_seconds_ago=0)
        insert_pending(db_path, approval_id=approval_id, status="SENDING", draft_id="draft-already-sent")
        executor.reconcile()
        row = pending_rows(db_path)[0]
        require_equal(row["status"], "COMPLETED")
        require_equal(fake.draft_exists_calls, ["draft-already-sent"])
        require_equal(len(fake.sent), 0, "must never call send_draft during recovery")
        require_equal(count_rows(db_path, "trust_events"), 1, "confirmed send should write exactly one trust event")
        require_equal(queue_status(db_path, approval_id)["status"], "EXECUTED")
        executor.reconcile()
        require_equal(count_rows(db_path, "trust_events"), 1, "recovered completion must not double-write trust")


@test
def sending_state_with_draft_still_present_resumes_and_completes_normally():
    # If the draft is still there, it genuinely never sent — safe to resume
    # from DRAFT_READY (never a double-send, since nothing went out yet).
    with isolated_db() as db_path, patched_gmail(FakeGmail(draft_exists_returns=True)) as fake:
        fake.recipients["draft-never-sent"] = {"to": ["person@example.net"], "cc": [], "bcc": []}
        insert_pending(db_path, status="SENDING", draft_id="draft-never-sent")
        executor.reconcile()
        row = pending_rows(db_path)[0]
        require_equal(row["status"], "DRAFT_READY", "must resume, not park in manual review")
        require_equal(len(fake.sent), 0, "must not send during the recovery check itself")

        executor.reconcile()
        row = pending_rows(db_path)[0]
        require_equal(row["status"], "COMPLETED")
        require_equal(len(fake.sent), 1, "the resumed row should send exactly once")


@test
def sending_state_verification_failure_falls_back_to_manual_review():
    # If we can't even confirm one way or the other (the read itself fails),
    # fail closed exactly as before — never assumed, never resent.
    with isolated_db() as db_path, patched_gmail(FakeGmail(draft_exists_raises=True)) as fake:
        execution_id = insert_pending(db_path, status="SENDING", draft_id="draft-crashed")
        executor.reconcile()
        row = pending_rows(db_path)[0]
        require_equal(row["execution_id"], execution_id)
        require_equal(row["status"], "MANUAL_REVIEW")
        require("Crashed during send" in (row["status_reason"] or ""), row["status_reason"])
        require_equal(len(fake.sent), 0, "SENDING recovery must not invoke send_draft")
        executor.reconcile()
        require_equal(len(fake.sent), 0, "manual review row must not be retried")


@test
def send_exception_goes_to_manual_review_without_trust_event_or_retry():
    with isolated_db() as db_path, patched_gmail(FakeGmail(send_raises=True)) as fake:
        fake.recipients["draft-fail"] = {"to": ["person@example.net"], "cc": [], "bcc": []}
        insert_pending(db_path, status="DRAFT_READY", draft_id="draft-fail")

        executor.reconcile()
        row = pending_rows(db_path)[0]
        require_equal(row["status"], "MANUAL_REVIEW")
        require("Send outcome unknown" in (row["status_reason"] or ""), row["status_reason"])
        require_equal(len(fake.sent), 1, "one send attempt should have occurred")
        require_equal(count_rows(db_path, "trust_events"), 0, "failed/unknown send must not write trust")

        executor.reconcile()
        require_equal(len(fake.sent), 1, "manual review row must not be retried")
        require_equal(count_rows(db_path, "trust_events"), 0, "retry must not create trust")


@test
def completed_execution_is_not_sent_or_trust_written_again_on_reconcile():
    with isolated_db() as db_path, patched_gmail(FakeGmail()) as fake:
        insert_approved(db_path)
        executor.reconcile()
        executor.reconcile()
        require_equal(pending_rows(db_path)[0]["status"], "COMPLETED")
        require_equal(len(fake.sent), 1)
        require_equal(count_rows(db_path, "trust_events"), 1)

        executor.reconcile()
        executor.reconcile()
        require_equal(len(fake.sent), 1, "completed rows should not be sent again")
        require_equal(count_rows(db_path, "trust_events"), 1, "completed rows should not double-write trust")


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
