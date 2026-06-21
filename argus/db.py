import sqlite3
import os
from flask import g

DATABASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'argus.db')


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

        CREATE TABLE IF NOT EXISTS approval_queue (
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

        CREATE TABLE IF NOT EXISTS pending_executions (
            execution_id  TEXT PRIMARY KEY,
            approval_id   TEXT UNIQUE,        -- queue item id; UNIQUE = one execution per approval
            action_type   TEXT NOT NULL,
            payload_json  TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'DRAFT_PENDING'
                          CHECK(status IN ('DRAFT_PENDING','DRAFT_READY','SENDING',
                                           'COMPLETED','MANUAL_REVIEW','FAILED')),
            draft_id      TEXT,               -- Gmail draft id (durable pre-send checkpoint)
            message_id    TEXT,               -- resulting sent message id
            history_id    TEXT,               -- mailbox historyId saved before send (review evidence)
            owner_token   TEXT,               -- claim token fencing all SENDING writes
            attempt_count INTEGER NOT NULL DEFAULT 0,
            status_reason TEXT,               -- why it's in MANUAL_REVIEW / FAILED
            last_error    TEXT,
            approved_at   INTEGER NOT NULL,
            execute_after INTEGER NOT NULL,
            created_at    INTEGER NOT NULL DEFAULT 0,
            updated_at    INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS demo_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL
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
    ''')

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

    # Migration: Phase 5 Part 2 execution-state columns on pending_executions
    for col, definition in [
        ("approval_id",   "TEXT"),
        ("status",        "TEXT NOT NULL DEFAULT 'DRAFT_PENDING'"),
        ("draft_id",      "TEXT"),
        ("message_id",    "TEXT"),
        ("history_id",    "TEXT"),
        ("owner_token",   "TEXT"),
        ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ("status_reason", "TEXT"),
        ("last_error",    "TEXT"),
        ("created_at",    "INTEGER NOT NULL DEFAULT 0"),
        ("updated_at",    "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            db.execute(f"ALTER TABLE pending_executions ADD COLUMN {col} {definition}")
        except Exception:
            pass  # column already exists
    # UNIQUE index on approval_id (ALTER can't add UNIQUE inline on existing tables)
    try:
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_approval ON pending_executions(approval_id)"
        )
    except Exception:
        pass

    db.execute("INSERT OR IGNORE INTO system_state VALUES ('SYSTEM_HARD_STOP', '0')")
    db.execute("INSERT OR IGNORE INTO system_state VALUES ('ACTIVE_PROFILE', 'Balanced')")
    db.execute("INSERT OR IGNORE INTO system_state VALUES ('OVERALL_TRUST_MODIFIER', '1.0')")
    db.execute("INSERT OR IGNORE INTO system_state VALUES ('UNDO_WINDOW_SECONDS', '30')")

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
