"""
Independent behavioral contract tests for argus.audit.
Authored by Codex (blind to the implementation's own tests) during the Phase 5-9
quality audit. Test logic unchanged; only run() summary aligned to RESULT format.
The privacy test caught a real gap (raw subject/body could be stored) which was
then fixed by adding defensive scrubbing in audit.record().
Run with: python tests/test_audit_independent.py
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

from argus import audit

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


def init_audit_schema(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
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

        CREATE TRIGGER audit_no_update
        BEFORE UPDATE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;

        CREATE TRIGGER audit_no_delete
        BEFORE DELETE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
        """
    )
    conn.commit()
    conn.close()


@contextlib.contextmanager
def isolated_db():
    old_path = audit.DATABASE
    with tempfile.TemporaryDirectory(prefix="argus-audit-independent-") as tmp:
        db_path = os.path.join(tmp, "argus-test.db")
        init_audit_schema(db_path)
        audit.DATABASE = db_path
        try:
            yield db_path
        finally:
            audit.DATABASE = old_path


@contextlib.contextmanager
def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def count_events(db_path):
    with connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM audit_events").fetchone()["c"]


@test
def record_appends_rows_and_update_delete_raise():
    with isolated_db() as db_path:
        first = audit.record("EVENT_ONE", idempotency_key="append-1", payload={"safe": "one"})
        second = audit.record("EVENT_TWO", idempotency_key="append-2", payload={"safe": "two"})
        require_equal(first.get("recorded"), True)
        require_equal(second.get("recorded"), True)
        require_equal(count_events(db_path), 2, "distinct records should append rows")

        with connect(db_path) as conn:
            try:
                conn.execute("UPDATE audit_events SET event_type='TAMPERED' WHERE id=1")
                conn.commit()
            except sqlite3.DatabaseError:
                pass
            else:
                raise AssertionError("UPDATE on audit_events should raise")

            try:
                conn.execute("DELETE FROM audit_events WHERE id=1")
                conn.commit()
            except sqlite3.DatabaseError:
                pass
            else:
                raise AssertionError("DELETE on audit_events should raise")

        require_equal(count_events(db_path), 2, "append-only trigger should preserve rows after rejected writes")


@test
def mutated_entry_makes_verify_chain_invalid():
    with isolated_db() as db_path:
        audit.record("EVENT_ONE", idempotency_key="chain-1", payload={"safe": "one"})
        audit.record("EVENT_TWO", idempotency_key="chain-2", payload={"safe": "two"})
        require_equal(audit.verify_chain().get("valid"), True)

        with connect(db_path) as conn:
            conn.execute("DROP TRIGGER audit_no_update")
            conn.execute("UPDATE audit_events SET payload_json=? WHERE id=1", ('{"safe":"tampered"}',))
            conn.commit()

        result = audit.verify_chain()
        require_equal(result.get("valid"), False, "tampering with a retained row should break the chain")
        require_equal(result.get("broken_at_id"), 1)


@test
def duplicate_idempotency_key_does_not_create_second_row():
    with isolated_db() as db_path:
        first = audit.record("EVENT_ONE", idempotency_key="same-key", payload={"safe": "one"})
        second = audit.record("EVENT_ONE", idempotency_key="same-key", payload={"safe": "two"})
        require_equal(first.get("recorded"), True)
        require_equal(second.get("recorded"), False)
        require_equal(second.get("reason"), "duplicate")
        require_equal(count_events(db_path), 1)


@test
def payloads_do_not_store_raw_email_body_or_subject_text():
    raw_subject = "Confidential acquisition subject line"
    raw_body = "This is the private raw email body that should not be retained."
    with isolated_db() as db_path:
        result = audit.record(
            "DECISION_EVALUATED",
            idempotency_key="privacy-1",
            action_type="email.send.external",
            outcome="GATED",
            payload={
                "subject": raw_subject,
                "body": raw_body,
                "entities": {"subject": raw_subject, "body": raw_body},
                "safe_summary": "metadata only",
            },
        )
        require_equal(result.get("recorded"), True)
        with connect(db_path) as conn:
            stored = conn.execute("SELECT payload_json FROM audit_events WHERE idempotency_key='privacy-1'").fetchone()["payload_json"]

        require(raw_subject not in stored, f"raw subject leaked into audit payload: {stored}")
        require(raw_body not in stored, f"raw body leaked into audit payload: {stored}")


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
