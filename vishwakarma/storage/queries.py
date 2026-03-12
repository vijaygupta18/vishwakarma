"""
SQLite query functions — save, fetch, search, dedup.
"""
import hashlib
import json
import logging
import time
from typing import Any

from vishwakarma.storage.db import _get_conn, _lock

log = logging.getLogger(__name__)


# ── Incidents ─────────────────────────────────────────────────────────────────

def save_incident(
    incident_id: str,
    title: str,
    question: str,
    analysis: str,
    source: str = "",
    severity: str = "info",
    labels: dict | None = None,
    tool_outputs: list | None = None,
    meta: dict | None = None,
    slack_ts: str | None = None,
    pdf_path: str | None = None,
) -> str:
    """Insert or update an incident record. Returns the incident_id."""
    now = time.time()
    conn = _get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO incidents
              (id, title, source, severity, status, question, analysis,
               tool_outputs, meta, created_at, updated_at, labels, slack_ts, pdf_path)
            VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              analysis    = excluded.analysis,
              tool_outputs = excluded.tool_outputs,
              meta        = excluded.meta,
              updated_at  = excluded.updated_at,
              slack_ts    = COALESCE(excluded.slack_ts, incidents.slack_ts),
              pdf_path    = COALESCE(excluded.pdf_path, incidents.pdf_path)
            """,
            (
                incident_id,
                title,
                source,
                severity,
                question,
                analysis,
                json.dumps(tool_outputs or []),
                json.dumps(meta or {}),
                now,
                now,
                json.dumps(labels or {}),
                slack_ts,
                pdf_path,
            ),
        )
        conn.commit()
    return incident_id


def update_incident_status(incident_id: str, status: str) -> bool:
    conn = _get_conn()
    now = time.time()
    with _lock:
        cur = conn.execute(
            "UPDATE incidents SET status=?, updated_at=?, resolved_at=? WHERE id=?",
            (status, now, now if status == "resolved" else None, incident_id),
        )
        conn.commit()
    return cur.rowcount > 0


def get_incident(incident_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM incidents WHERE id=?", (incident_id,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def list_incidents(
    source: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    conn = _get_conn()
    where = []
    params: list[Any] = []
    if source:
        where.append("source=?")
        params.append(source)
    if status:
        where.append("status=?")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params += [limit, offset]
    rows = conn.execute(
        f"SELECT * FROM incidents {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def search_incidents(query: str, limit: int = 20) -> list[dict]:
    """Full-text search on title + analysis."""
    conn = _get_conn()
    pattern = f"%{query}%"
    rows = conn.execute(
        """
        SELECT * FROM incidents
        WHERE title LIKE ? OR analysis LIKE ? OR question LIKE ?
        ORDER BY created_at DESC LIMIT ?
        """,
        (pattern, pattern, pattern, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_stats() -> dict:
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    by_status = {
        row[0]: row[1]
        for row in conn.execute("SELECT status, COUNT(*) FROM incidents GROUP BY status").fetchall()
    }
    by_source = {
        row[0]: row[1]
        for row in conn.execute("SELECT source, COUNT(*) FROM incidents GROUP BY source").fetchall()
    }
    return {"total": total, "by_status": by_status, "by_source": by_source}


# ── Oracle Sessions ───────────────────────────────────────────────────────────

def save_oracle_session(session_id: str, messages: list, title: str = "") -> None:
    """Persist oracle session messages to SQLite after each turn."""
    conn = _get_conn()
    now = time.time()
    if not title:
        # Auto-title from first user message
        for m in messages:
            if m.get("role") == "user":
                title = str(m.get("content", ""))[:100]
                break
    with _lock:
        conn.execute(
            """
            INSERT INTO oracle_sessions (id, title, messages, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              messages   = excluded.messages,
              updated_at = excluded.updated_at
            """,
            (session_id, title or session_id, json.dumps(messages), now, now),
        )
        conn.commit()


def load_oracle_session(session_id: str) -> list | None:
    """Load oracle session messages. Returns None if not found."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT messages FROM oracle_sessions WHERE id=?", (session_id,)
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def list_oracle_sessions(limit: int = 20) -> list[dict]:
    """List recent oracle sessions (most recent first)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, title, created_at, updated_at FROM oracle_sessions ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Deduplication ─────────────────────────────────────────────────────────────

def check_dedup(fingerprint: str) -> str | None:
    """Return incident_id if a non-expired dedup entry exists for this fingerprint."""
    conn = _get_conn()
    now = time.time()
    row = conn.execute(
        "SELECT incident_id FROM dedup_state WHERE fingerprint=? AND expires_at > ?",
        (fingerprint, now),
    ).fetchone()
    return row[0] if row else None


def set_dedup(fingerprint: str, incident_id: str, window_seconds: int = 300) -> None:
    """Set a dedup window for a fingerprint."""
    conn = _get_conn()
    expires = time.time() + window_seconds
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO dedup_state (fingerprint, incident_id, expires_at) VALUES (?,?,?)",
            (fingerprint, incident_id, expires),
        )
        conn.commit()


def clear_expired_dedup() -> int:
    """Remove expired dedup entries. Returns count removed."""
    conn = _get_conn()
    now = time.time()
    with _lock:
        cur = conn.execute("DELETE FROM dedup_state WHERE expires_at <= ?", (now,))
        conn.commit()
    return cur.rowcount


def alert_fingerprint(labels: dict) -> str:
    """Build a stable fingerprint from alert labels for deduplication."""
    key_labels = ["alertname", "namespace", "service", "job", "instance"]
    parts = ":".join(str(labels.get(k, "")) for k in key_labels)
    return hashlib.md5(parts.encode()).hexdigest()


# ── Internal ──────────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    d = dict(row)
    for field in ("tool_outputs", "meta", "labels"):
        if isinstance(d.get(field), str):
            try:
                d[field] = json.loads(d[field])
            except Exception:
                pass
    return d
