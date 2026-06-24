import sqlite3
import os
from flask import g

DATABASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'argus.db')


# ── Evolving-schema DDL ───────────────────────────────────────────────────────
# approval_queue and pending_executions gain new status enum values in Phase 8.
# SQLite cannot ALTER a CHECK constraint, so these two tables are defined as
# constants reused by BOTH the fresh-create path and the rebuild migration
# (_migrate_check_constraints). Keeping one source of truth prevents a fresh DB
# and a migrated DB from drifting apart.

APPROVAL_QUEUE_DDL = '''
    CREATE TABLE IF NOT EXISTS approval_queue (
        id TEXT PRIMARY KEY,
        proposal_json TEXT NOT NULL,
        decision_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN (
            'PENDING','APPROVED','REJECTED','EXPIRED','MANUAL_REVIEW',
            'EXECUTED','CANCELLED',
            'HELD','MANUAL_REVIEW_TIMEOUT','TRANSITION_LOCKED'
        )),
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        approved_at INTEGER,
        updated_at INTEGER NOT NULL,
        status_reason TEXT CHECK(status_reason IS NULL OR length(status_reason) <= 500),
        execution_id TEXT,
        -- Phase 8 fail-safe columns:
        version INTEGER NOT NULL DEFAULT 0,                  -- CAS guard for transitions
        approval_epoch INTEGER NOT NULL DEFAULT 0,           -- hard-stop epoch stamped at APPROVE
        approval_generation INTEGER NOT NULL DEFAULT 0,      -- per-item, bumped each APPROVE
        manual_review_generation INTEGER NOT NULL DEFAULT 0, -- bumped each MANUAL_REVIEW entry
        manual_review_started_at INTEGER,                    -- for lazy 600s timeout
        transition_lock_reason TEXT CHECK(transition_lock_reason IS NULL OR length(transition_lock_reason) <= 500),
        transition_locked_at INTEGER
    );
'''

# Columns copied from an old approval_queue during rebuild (the pre-Phase-8 set).
_APPROVAL_QUEUE_OLD_COLS = (
    "id, proposal_json, decision_json, status, created_at, expires_at, "
    "approved_at, updated_at, status_reason, execution_id"
)

PENDING_EXECUTIONS_DDL = '''
    CREATE TABLE IF NOT EXISTS pending_executions (
        execution_id  TEXT PRIMARY KEY,
        approval_id   TEXT,                  -- NOT inline-UNIQUE; see composite UNIQUE below
        action_type   TEXT NOT NULL,
        payload_json  TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'DRAFT_PENDING'
                      CHECK(status IN (
                          'DRAFT_PENDING','DRAFT_READY','SENDING',
                          'COMPLETED','MANUAL_REVIEW','FAILED',
                          'HELD','SUPERSEDED'
                      )),
        draft_id      TEXT,                  -- Gmail draft id (durable pre-send checkpoint)
        message_id    TEXT,                  -- resulting sent message id
        history_id    TEXT,                  -- mailbox historyId saved before send
        owner_token   TEXT,                  -- claim token fencing all SENDING writes
        attempt_count INTEGER NOT NULL DEFAULT 0,
        status_reason TEXT CHECK(status_reason IS NULL OR length(status_reason) <= 500),
        last_error    TEXT,
        -- Phase 8: two-counter execution identity. UNIQUE(approval_id, approval_generation)
        -- lets a SUPERSEDED execution coexist with a freshly-approved one (next generation).
        approval_epoch      INTEGER NOT NULL DEFAULT 0,
        approval_generation INTEGER NOT NULL DEFAULT 0,
        approved_at   INTEGER NOT NULL,
        execute_after INTEGER NOT NULL,
        created_at    INTEGER NOT NULL DEFAULT 0,
        updated_at    INTEGER NOT NULL DEFAULT 0,
        UNIQUE(approval_id, approval_generation)
    );
'''

_PENDING_EXEC_OLD_COLS = (
    "execution_id, approval_id, action_type, payload_json, status, draft_id, "
    "message_id, history_id, owner_token, attempt_count, status_reason, "
    "last_error, approved_at, execute_after, created_at, updated_at"
)


def get_db():
    if 'db' not in g:
        os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
        g.db = sqlite3.connect(DATABASE, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def _migrate_check_constraints(db):
    """Rebuild approval_queue / pending_executions on an existing DB whose status
    CHECK predates Phase 8. Guarded by a marker string so it runs at most once.
    Preserves all existing rows; new columns take their DEFAULTs.

    The rebuild (RENAME -> CREATE -> copy -> DROP) runs inside ONE explicit
    transaction: SQLite DDL is transactional, so a crash mid-rebuild rolls back
    cleanly and never strands a half-renamed *_old table holding live data."""
    def _needs(table, marker):
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return bool(row and row[0] and marker not in row[0])

    aq_needs = _needs('approval_queue', 'TRANSITION_LOCKED')
    pe_needs = _needs('pending_executions', 'SUPERSEDED')
    if not (aq_needs or pe_needs):
        return

    # Take explicit control of the transaction. executescript() would force an
    # implicit COMMIT, so use execute() on the single-statement DDL constants.
    prev_iso = db.isolation_level
    db.isolation_level = None
    try:
        db.execute("BEGIN")
        if aq_needs:
            db.execute("ALTER TABLE approval_queue RENAME TO approval_queue_old")
            db.execute(APPROVAL_QUEUE_DDL)
            db.execute(
                f"INSERT INTO approval_queue ({_APPROVAL_QUEUE_OLD_COLS}) "
                f"SELECT {_APPROVAL_QUEUE_OLD_COLS} FROM approval_queue_old"
            )
            db.execute("DROP TABLE approval_queue_old")
        if pe_needs:
            # Drop the old single-column UNIQUE index (idx_pending_approval) which
            # would block supersede + re-approve under the new composite UNIQUE.
            db.execute("DROP INDEX IF EXISTS idx_pending_approval")
            db.execute("ALTER TABLE pending_executions RENAME TO pending_executions_old")
            db.execute(PENDING_EXECUTIONS_DDL)
            db.execute(
                f"INSERT INTO pending_executions ({_PENDING_EXEC_OLD_COLS}) "
                f"SELECT {_PENDING_EXEC_OLD_COLS} FROM pending_executions_old"
            )
            db.execute("DROP TABLE pending_executions_old")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    finally:
        db.isolation_level = prev_iso


def init_db():
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row

    db.executescript('''
        CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS permission_profiles (
            profile_name TEXT PRIMARY KEY CHECK(profile_name IN ('Strict','Balanced','Autonomous')),
            active INTEGER NOT NULL CHECK(active IN (0,1))
        );

        CREATE TABLE IF NOT EXISTS prime_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            condition_json TEXT NOT NULL,
            description TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS policy_gates (
            action_type TEXT PRIMARY KEY,
            min_threshold REAL NOT NULL DEFAULT 1.0,
            base_threshold REAL NOT NULL DEFAULT 5.0
        );

        CREATE TABLE IF NOT EXISTS contact_permissions (
            contact TEXT NOT NULL,
            action_type TEXT NOT NULL,
            relax_amount REAL NOT NULL DEFAULT 0.0,
            PRIMARY KEY(contact, action_type)
        );

        CREATE TABLE IF NOT EXISTS trust_current (
            action_type       TEXT PRIMARY KEY,
            trust_current     REAL NOT NULL CHECK(trust_current >= 0 AND trust_current <= 100),
            damping_remaining INTEGER NOT NULL DEFAULT 0,
            damping_streak    INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS trust_events (
            event_id        TEXT PRIMARY KEY,
            timestamp       INTEGER NOT NULL,
            action_type     TEXT NOT NULL,
            delta           REAL NOT NULL,
            reason          TEXT NOT NULL,
            resulting_trust REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            free_gated TEXT NOT NULL CHECK(free_gated IN ('FREE','GATED')),
            outcome TEXT NOT NULL,
            trust_reason TEXT,
            before_state TEXT,
            after_state TEXT,
            decision_trace TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rate_limits (
            action_category TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY(action_category, window_start)
        );

        CREATE TABLE IF NOT EXISTS demo_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,  -- monotonic ordering key
            timestamp       INTEGER NOT NULL,
            event_type      TEXT NOT NULL,
            correlation_id  TEXT,
            action_type     TEXT,
            outcome         TEXT,
            reason          TEXT,
            idempotency_key TEXT UNIQUE,   -- source-event key; prevents duplicate mirrors
            payload_json    TEXT NOT NULL, -- canonical, code-owned (no user/model text)
            prev_entry_hash TEXT,
            entry_hash      TEXT NOT NULL
        );

        -- Append-only + tamper-evident: reject any UPDATE/DELETE on the audit log.
        CREATE TRIGGER IF NOT EXISTS audit_no_update
        BEFORE UPDATE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;

        CREATE TRIGGER IF NOT EXISTS audit_no_delete
        BEFORE DELETE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;

        CREATE TABLE IF NOT EXISTS agent_proposals (
            id            TEXT PRIMARY KEY,
            proposal_json TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'PROPOSAL'
                          CHECK(status IN ('PROPOSAL','CONSUMED')),
            created_at    INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS email_templates (
            id             TEXT PRIMARY KEY,
            contact        TEXT,          -- canonical recipient email; NULL = any
            action_type    TEXT,          -- ARGUS email action; NULL = any
            tone           TEXT NOT NULL,
            formality      TEXT NOT NULL,
            length_class   TEXT NOT NULL,
            greeting_style TEXT NOT NULL,
            signoff_style  TEXT NOT NULL,
            max_words      INTEGER NOT NULL,
            max_sentences  INTEGER NOT NULL,
            max_paragraphs INTEGER NOT NULL,
            avoid_phrases  TEXT,          -- JSON array; VALIDATOR-ONLY, never in the model prompt
            enabled        INTEGER NOT NULL DEFAULT 1,
            version        INTEGER NOT NULL DEFAULT 1,
            created_at     INTEGER NOT NULL,
            updated_at     INTEGER NOT NULL,
            CHECK (tone IN ('warm','neutral','direct','formal','friendly')),
            CHECK (formality IN ('casual','professional','formal')),
            CHECK (length_class IN ('brief','standard','detailed')),
            CHECK (greeting_style IN ('none','first_name','formal_name')),
            CHECK (signoff_style IN ('none','thanks','regards','best')),
            CHECK (max_words BETWEEN 10 AND 1000),
            CHECK (max_sentences BETWEEN 1 AND 30),
            CHECK (max_paragraphs BETWEEN 1 AND 8)
        );

        -- ── Phase 8 fail-safe tables ──────────────────────────────────────────
        -- Private-contact protection: relationships excluded from AI processing.
        -- Match is EXACT normalized address only (no +tag strip, no name match).
        CREATE TABLE IF NOT EXISTS private_contacts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            normalized_email TEXT NOT NULL UNIQUE,
            display_label    TEXT,
            enabled          INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            created_at       INTEGER NOT NULL,
            updated_at       INTEGER NOT NULL
        );

        -- Duplicate detection: exact-canonical proposal hash, short TTL window.
        CREATE TABLE IF NOT EXISTS proposal_dedup (
            user_id       TEXT NOT NULL,
            proposal_hash TEXT NOT NULL,
            proposal_id   TEXT NOT NULL,
            created_at    INTEGER NOT NULL,
            expires_at    INTEGER NOT NULL,
            PRIMARY KEY (user_id, proposal_hash)
        );

        -- Invalid-transition rate limiting: evidence trail per queue item.
        CREATE TABLE IF NOT EXISTS queue_transition_attempts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_id       TEXT NOT NULL,
            attempted_from TEXT,
            attempted_to   TEXT,
            valid          INTEGER NOT NULL CHECK(valid IN (0,1)),
            created_at     INTEGER NOT NULL
        );
    ''')

    # approval_queue + pending_executions: single-source DDL (also used by rebuild).
    db.executescript(APPROVAL_QUEUE_DDL)
    db.executescript(PENDING_EXECUTIONS_DDL)

    # Phase 5 Part 3: scope-uniqueness for templates (one row per scope rank).
    # Partial unique indexes are the backstop; code-side upsert is the normal path.
    for idx_sql in [
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tmpl_exact ON email_templates(contact, action_type) "
        "WHERE contact IS NOT NULL AND action_type IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tmpl_contact ON email_templates(contact) "
        "WHERE contact IS NOT NULL AND action_type IS NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tmpl_action ON email_templates(action_type) "
        "WHERE contact IS NULL AND action_type IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tmpl_global "
        "ON email_templates((contact IS NULL AND action_type IS NULL)) "
        "WHERE contact IS NULL AND action_type IS NULL",
    ]:
        try:
            db.execute(idx_sql)
        except Exception:
            pass

    # Migration: add damping columns to trust_current if upgrading an existing DB
    for col, definition in [
        ("damping_remaining", "INTEGER NOT NULL DEFAULT 0"),
        ("damping_streak",    "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            db.execute(f"ALTER TABLE trust_current ADD COLUMN {col} {definition}")
        except Exception:
            pass  # column already exists

    # Migration: per-row metadata on system_state (Phase 8 control-plane audit).
    for col, definition in [
        ("updated_at", "INTEGER"),
        ("updated_by", "TEXT"),
        ("reason",     "TEXT"),
    ]:
        try:
            db.execute(f"ALTER TABLE system_state ADD COLUMN {col} {definition}")
        except Exception:
            pass  # column already exists

    # Migration: rebuild evolving tables on an existing pre-Phase-8 DB so the new
    # status enum values are accepted and the composite UNIQUE replaces the old one.
    _migrate_check_constraints(db)

    db.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('SYSTEM_HARD_STOP', '0')")
    db.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('HARD_STOP_EPOCH', '0')")
    db.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('ACTIVE_PROFILE', 'Balanced')")
    db.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('OVERALL_TRUST_MODIFIER', '1.0')")
    db.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('UNDO_WINDOW_SECONDS', '60')")

    db.execute("INSERT OR IGNORE INTO permission_profiles VALUES ('Balanced', 1)")
    db.execute("INSERT OR IGNORE INTO permission_profiles VALUES ('Strict', 0)")
    db.execute("INSERT OR IGNORE INTO permission_profiles VALUES ('Autonomous', 0)")

    from config import FREE_ACTIONS, GATED_ACTIONS
    for action in FREE_ACTIONS + GATED_ACTIONS:
        db.execute(
            "INSERT OR IGNORE INTO trust_current (action_type, trust_current) VALUES (?, 40.0)",
            (action,)
        )

    for action in GATED_ACTIONS:
        db.execute("INSERT OR IGNORE INTO policy_gates VALUES (?, 1.0, 5.0)", (action,))

    db.commit()
    db.close()
    print("[ARGUS] Database initialised.")
