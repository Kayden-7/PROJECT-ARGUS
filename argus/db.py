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
            status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED','EXPIRED','MANUAL_REVIEW')),
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trust_current (
            action_type TEXT PRIMARY KEY,
            trust_current REAL NOT NULL CHECK(trust_current >= 0 AND trust_current <= 100)
        );

        CREATE TABLE IF NOT EXISTS trust_events (
            event_id TEXT PRIMARY KEY,
            timestamp INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            delta INTEGER NOT NULL,
            reason TEXT NOT NULL,
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
            execution_id TEXT PRIMARY KEY,
            action_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            approved_at INTEGER NOT NULL,
            execute_after INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS demo_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL
        );
    ''')

    db.execute("INSERT OR IGNORE INTO system_state VALUES ('SYSTEM_HARD_STOP', '0')")
    db.execute("INSERT OR IGNORE INTO system_state VALUES ('ACTIVE_PROFILE', 'Balanced')")
    db.execute("INSERT OR IGNORE INTO system_state VALUES ('OVERALL_TRUST_MODIFIER', '1.0')")

    db.execute("INSERT OR IGNORE INTO permission_profiles VALUES ('Balanced', 1)")
    db.execute("INSERT OR IGNORE INTO permission_profiles VALUES ('Strict', 0)")
    db.execute("INSERT OR IGNORE INTO permission_profiles VALUES ('Autonomous', 0)")

    from config import FREE_ACTIONS, GATED_ACTIONS
    for action in FREE_ACTIONS + GATED_ACTIONS:
        db.execute("INSERT OR IGNORE INTO trust_current VALUES (?, 40.0)", (action,))

    for action in GATED_ACTIONS:
        db.execute("INSERT OR IGNORE INTO policy_gates VALUES (?, 1.0, 5.0)", (action,))

    db.commit()
    db.close()
    print("[ARGUS] Database initialised.")
