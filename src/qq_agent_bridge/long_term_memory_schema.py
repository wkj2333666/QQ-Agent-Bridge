"""SQLite schema and migration helpers for long-term memory."""
from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 1

SCHEMA_DDL = f"""
CREATE TABLE IF NOT EXISTS memory_scopes (
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('group', 'private')),
    scope_id TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(scope_kind, scope_id)
);

CREATE TABLE IF NOT EXISTS review_buffer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('group', 'private')),
    scope_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    text TEXT NOT NULL,
    message_timestamp INTEGER NOT NULL,
    mentioned_ids_json TEXT NOT NULL DEFAULT '[]',
    quoted_sender_id TEXT,
    is_reply INTEGER NOT NULL DEFAULT 0 CHECK(is_reply IN (0, 1)),
    direct_interaction INTEGER NOT NULL DEFAULT 0
        CHECK(direct_interaction IN (0, 1)),
    command_class TEXT,
    collection_reason TEXT NOT NULL,
    explicit_source INTEGER NOT NULL DEFAULT 0
        CHECK(explicit_source IN (0, 1)),
    review_state TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    UNIQUE(scope_kind, scope_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_review_buffer_pending
    ON review_buffer(scope_kind, scope_id, review_state,
                     next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_review_buffer_ttl
    ON review_buffer(created_at);

CREATE TABLE IF NOT EXISTS memory_items (
    id TEXT PRIMARY KEY,
    short_id TEXT NOT NULL UNIQUE,
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('group', 'private')),
    scope_id TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN (
        'preference', 'identity', 'project', 'relationship',
        'group_norm', 'recurring_topic'
    )),
    content TEXT NOT NULL,
    base_confidence REAL NOT NULL CHECK(base_confidence BETWEEN 0 AND 1),
    effective_score REAL NOT NULL CHECK(effective_score BETWEEN 0 AND 1),
    status TEXT NOT NULL CHECK(status IN (
        'candidate', 'active', 'dormant', 'contradicted', 'rejected'
    )),
    sensitivity TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_count INTEGER NOT NULL DEFAULT 1,
    explicit_memory INTEGER NOT NULL DEFAULT 0
        CHECK(explicit_memory IN (0, 1)),
    decay_exempt INTEGER NOT NULL DEFAULT 0
        CHECK(decay_exempt IN (0, 1)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_supported_at INTEGER NOT NULL,
    expires_at INTEGER,
    dormant_at INTEGER,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_memory_items_scope_status
    ON memory_items(scope_kind, scope_id, status, effective_score);
CREATE INDEX IF NOT EXISTS idx_memory_items_subject
    ON memory_items(scope_kind, scope_id, subject_kind, subject_id, status);

CREATE TABLE IF NOT EXISTS memory_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT REFERENCES memory_items(id) ON DELETE SET NULL,
    operation TEXT NOT NULL,
    actor_class TEXT NOT NULL,
    before_summary TEXT,
    after_summary TEXT,
    evidence_excerpt TEXT,
    deleted_item_hash TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_revisions_item
    ON memory_revisions(item_id, created_at);

CREATE TABLE IF NOT EXISTS review_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_hash TEXT NOT NULL,
    trigger_class TEXT NOT NULL,
    source_count INTEGER NOT NULL,
    proposed_count INTEGER NOT NULL,
    accepted_count INTEGER NOT NULL,
    candidate_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    retry_count INTEGER NOT NULL,
    error_class TEXT,
    started_at INTEGER NOT NULL,
    finished_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_runs_scope
    ON review_runs(scope_hash, finished_at);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    item_id UNINDEXED,
    content,
    tokenize = 'unicode61'
);
PRAGMA user_version = {SCHEMA_VERSION};
"""


def migrate(conn: sqlite3.Connection) -> None:
    """Migrate an initialized connection to the supported schema version."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"memory database schema {version} is newer than supported {SCHEMA_VERSION}"
        )
    try:
        conn.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA_DDL}\nCOMMIT;")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


__all__ = ["SCHEMA_VERSION", "migrate"]
