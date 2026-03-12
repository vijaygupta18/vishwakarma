"""
SQLite storage — incidents, investigations, dedup state.

Schema:
  incidents        — all investigation records
  dedup_state      — active dedup windows (alert fingerprint → investigation_id)

Stored at VK_DB_PATH (default /data/vishwakarma.db).
Single-writer model: all writes serialized via Python thread lock.
"""
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_db_path: str = "/data/vishwakarma.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    source         TEXT,
    severity       TEXT,
    status         TEXT DEFAULT 'open',
    question       TEXT,
    analysis       TEXT,
    tool_outputs   TEXT,       -- JSON array
    meta           TEXT,       -- JSON object (cost, tokens, duration)
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    resolved_at    REAL,
    labels         TEXT,       -- JSON object
    slack_ts       TEXT,       -- Slack message ts for threading
    pdf_path       TEXT
);

CREATE TABLE IF NOT EXISTS dedup_state (
    fingerprint    TEXT PRIMARY KEY,
    incident_id    TEXT NOT NULL,
    expires_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oracle_sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,           -- first question (auto-title)
    messages    TEXT NOT NULL,           -- JSON array of full message history
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_incidents_source ON incidents(source);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_incidents_created ON incidents(created_at);
CREATE INDEX IF NOT EXISTS idx_oracle_sessions_updated ON oracle_sessions(updated_at);
"""


def init_db(db_path: str | None = None) -> None:
    """Initialize database and create schema."""
    global _db_path, _conn
    if db_path:
        _db_path = db_path

    # Create parent directory if needed
    Path(_db_path).parent.mkdir(parents=True, exist_ok=True)

    with _lock:
        _conn = sqlite3.connect(_db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(SCHEMA)
        _conn.commit()
        log.info(f"Database initialized at {_db_path}")


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        init_db()
    return _conn  # type: ignore
