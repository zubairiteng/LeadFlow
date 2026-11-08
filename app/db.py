"""SQLite access layer (no ORM, zero extra dependencies)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    public_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    postal_address TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    daily_limit INTEGER NOT NULL DEFAULT 50,
    window_start INTEGER NOT NULL DEFAULT 9,
    window_end INTEGER NOT NULL DEFAULT 17,
    weekdays_only INTEGER NOT NULL DEFAULT 1,
    ai_provider TEXT NOT NULL DEFAULT '',
    ai_model TEXT NOT NULL DEFAULT '',
    ai_key_enc TEXT NOT NULL DEFAULT '',
    email_mode TEXT NOT NULL DEFAULT 'none',          -- none | demo | gmail | smtp
    email_address TEXT NOT NULL DEFAULT '',
    gmail_refresh_enc TEXT NOT NULL DEFAULT '',
    gmail_access_enc TEXT NOT NULL DEFAULT '',
    gmail_expiry TEXT,
    smtp_host TEXT NOT NULL DEFAULT '',
    smtp_port INTEGER NOT NULL DEFAULT 587,
    smtp_user TEXT NOT NULL DEFAULT '',
    smtp_pass_enc TEXT NOT NULL DEFAULT '',
    imap_host TEXT NOT NULL DEFAULT '',
    imap_port INTEGER NOT NULL DEFAULT 993,
    email_connected_at TEXT,
    next_send_at TEXT,
    paused_until TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    last_reply_check TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'researcher',          -- admin | manager | researcher
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    product TEXT NOT NULL DEFAULT '',
    target_market TEXT NOT NULL DEFAULT '',
    created_by INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    keyword TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    company_type TEXT NOT NULL DEFAULT '',
    target INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'Queued',
    found INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    excluded_skipped INTEGER NOT NULL DEFAULT 0,
    progress TEXT NOT NULL DEFAULT '{}',
    message TEXT NOT NULL DEFAULT '',
    log TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    company_name TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    email_status TEXT NOT NULL DEFAULT 'None',        -- None | Unverified | Risky | Invalid | Verified
    extra_emails TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'Manual',
    score INTEGER NOT NULL DEFAULT 0,
    grade TEXT NOT NULL DEFAULT 'Cold',
    excluded INTEGER NOT NULL DEFAULT 0,
    enrich_state TEXT NOT NULL DEFAULT 'done',        -- pending | done | failed
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(org_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_cust_campaign ON customers(org_id, campaign_id, excluded);
CREATE INDEX IF NOT EXISTS idx_cust_enrich ON customers(enrich_state);

CREATE TABLE IF NOT EXISTS excluded_domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    dedup_key TEXT NOT NULL,
    customer_id INTEGER,
    company_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by INTEGER,
    UNIQUE(org_id, dedup_key)
);

CREATE TABLE IF NOT EXISTS dnc (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT 'unsubscribed',
    created_at TEXT NOT NULL,
    UNIQUE(org_id, email)
);

CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    followup_days TEXT NOT NULL DEFAULT '3,7',
    followup_bodies TEXT NOT NULL DEFAULT '',
    ignore_after_days INTEGER NOT NULL DEFAULT 3,
    daily_limit INTEGER,
    audience TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'Draft',             -- Draft | Approved | Sending | Paused | Finished
    start_date TEXT,
    test_hash TEXT NOT NULL DEFAULT '',
    frozen_hash TEXT NOT NULL DEFAULT '',
    approved_by INTEGER,
    approved_at TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(campaign_id, number)
);

CREATE TABLE IF NOT EXISTS recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    round_id INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'Pending',  -- Pending|Sent|Replied|Ignored|Bounced|Unsubscribed|Skipped
    steps_sent INTEGER NOT NULL DEFAULT 0,
    first_sent_at TEXT,
    last_sent_at TEXT,
    thread_id TEXT NOT NULL DEFAULT '',
    first_message_id TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(round_id, customer_id)
);
CREATE INDEX IF NOT EXISTS idx_rec_status ON recipients(org_id, status);

CREATE TABLE IF NOT EXISTS sent_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    recipient_id INTEGER REFERENCES recipients(id) ON DELETE CASCADE,
    round_id INTEGER,
    step INTEGER NOT NULL DEFAULT 0,
    message_id TEXT NOT NULL DEFAULT '',
    message_id_alt TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    to_email TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'sent',              -- sent | failed
    error TEXT NOT NULL DEFAULT '',
    is_test INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sent_org_time ON sent_emails(org_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_sent_msgid ON sent_emails(message_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    customer_id INTEGER,
    recipient_id INTEGER,
    round_id INTEGER,
    type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_org ON events(org_id, created_at);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    customer_id INTEGER,
    round_id INTEGER,
    recipient_id INTEGER,
    kind TEXT NOT NULL DEFAULT 'reply',
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Open',
    created_at TEXT NOT NULL,
    done_at TEXT
);

CREATE TABLE IF NOT EXISTS processed_inbound (
    org_id INTEGER NOT NULL,
    msg_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (org_id, msg_key)
);

CREATE TABLE IF NOT EXISTS demo_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    raw BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limits (
    k TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate ON rate_limits(k, created_at);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self.con = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        if path != ":memory:":
            self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.execute("PRAGMA busy_timeout=30000")
        self._depth = 0

    def q(self, sql, args=()):
        return self.con.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.con.execute(sql, args).fetchone()

    def x(self, sql, args=()):
        """Execute and return lastrowid."""
        return self.con.execute(sql, args).lastrowid

    def n(self, sql, args=()):
        """Execute and return affected rows."""
        return self.con.execute(sql, args).rowcount

    @contextmanager
    def tx(self):
        if self._depth > 0:
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
            return
        self.con.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0

    def init_schema(self):
        self.con.executescript(SCHEMA)

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass
