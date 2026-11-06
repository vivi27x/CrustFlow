"""SQLite storage. One process and short statements, so one shared connection is enough."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signups (
    id TEXT PRIMARY KEY,
    email_raw TEXT NOT NULL,
    email_norm TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    person_id INTEGER,
    created_at REAL NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_signups_norm ON signups(email_norm);
CREATE INDEX IF NOT EXISTS idx_signups_person ON signups(person_id);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signup_id TEXT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    status INTEGER,
    latency_ms INTEGER,
    credits REAL,
    cached INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_calls_signup ON calls(signup_id);
CREATE TABLE IF NOT EXISTS human_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signup_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_claims (
    key TEXT PRIMARY KEY,
    signup_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signup_id TEXT NOT NULL,
    dataset TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    crustdata_id INTEGER,
    last_run_id INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    UNIQUE (dataset, subject)
);
CREATE INDEX IF NOT EXISTS idx_watches_signup ON watches(signup_id);
CREATE TABLE IF NOT EXISTS watch_events (
    uid TEXT PRIMARY KEY,
    watch_id INTEGER NOT NULL,
    signup_id TEXT NOT NULL,
    run_id INTEGER,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_signup ON watch_events(signup_id);
CREATE TABLE IF NOT EXISTS pill_runs (
    token TEXT NOT NULL,
    pill_id TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (token, pill_id)
);
"""

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def connect(path: str | None = None) -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    target = path or get_settings().db_path
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(_SCHEMA)
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def _execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        return connect().execute(sql, params)


def _row_to_signup(row: sqlite3.Row) -> dict[str, Any]:
    signup = dict(row)
    signup.update(json.loads(signup.pop("data")))
    return signup


def create_signup(email_raw: str, **fields: Any) -> dict[str, Any]:
    signup_id = uuid.uuid4().hex[:12]
    _execute(
        "INSERT INTO signups (id, email_raw, status, created_at) VALUES (?, ?, 'received', ?)",
        (signup_id, email_raw, time.time()),
    )
    if fields:
        update_signup(signup_id, **fields)
    return get_signup(signup_id)  # type: ignore[return-value]


def get_signup(signup_id: str) -> dict[str, Any] | None:
    row = _execute("SELECT * FROM signups WHERE id = ?", (signup_id,)).fetchone()
    return _row_to_signup(row) if row else None


def list_signups(limit: int = 200) -> list[dict[str, Any]]:
    rows = _execute("SELECT * FROM signups ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_signup(r) for r in rows]


_COLUMNS = {"email_norm", "domain", "status", "person_id"}


def update_signup(signup_id: str, **fields: Any) -> None:
    """Columns go to columns; everything else merges into the JSON `data` blob."""
    columns = {k: v for k, v in fields.items() if k in _COLUMNS}
    extras = {k: v for k, v in fields.items() if k not in _COLUMNS}
    with _lock:
        conn = connect()
        if columns:
            assignments = ", ".join(f"{k} = ?" for k in columns)
            conn.execute(f"UPDATE signups SET {assignments} WHERE id = ?", (*columns.values(), signup_id))
        if extras:
            row = conn.execute("SELECT data FROM signups WHERE id = ?", (signup_id,)).fetchone()
            if row:
                data = json.loads(row["data"])
                data.update(extras)
                conn.execute("UPDATE signups SET data = ? WHERE id = ?", (json.dumps(data, default=str), signup_id))


def find_signup_by_token(key: str, token: str) -> dict[str, Any] | None:
    row = _execute(
        "SELECT * FROM signups WHERE json_extract(data, ?) = ?", (f"$.{key}", token)
    ).fetchone()
    return _row_to_signup(row) if row else None


def claim_identity(key: str, signup_id: str) -> str:
    """Atomically claims an email or person for a signup and returns whoever holds it.

    Signups run concurrently, so two emails for one person can both finish identify before
    either is stored. A lookup would let both through; a claim lets exactly one win.
    """
    with _lock:
        conn = connect()
        conn.execute("INSERT OR IGNORE INTO identity_claims (key, signup_id) VALUES (?, ?)", (key, signup_id))
        row = conn.execute("SELECT signup_id FROM identity_claims WHERE key = ?", (key,)).fetchone()
        return row["signup_id"]


def log_call(
    *,
    signup_id: str | None,
    kind: str,
    endpoint: str,
    status: int | None = None,
    latency_ms: int | None = None,
    credits: float | None = None,
    cached: bool = False,
    note: str = "",
) -> dict[str, Any]:
    ts = time.time()
    cursor = _execute(
        "INSERT INTO calls (signup_id, ts, kind, endpoint, status, latency_ms, credits, cached, note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (signup_id, ts, kind, endpoint, status, latency_ms, credits, int(cached), note),
    )
    return {
        "id": cursor.lastrowid,
        "signup_id": signup_id,
        "ts": ts,
        "kind": kind,
        "endpoint": endpoint,
        "status": status,
        "latency_ms": latency_ms,
        "credits": credits,
        "cached": cached,
        "note": note,
    }


def list_calls(signup_id: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
    if signup_id:
        rows = _execute(
            "SELECT * FROM calls WHERE signup_id = ? ORDER BY id DESC LIMIT ?", (signup_id, limit)
        ).fetchall()
    else:
        rows = _execute("SELECT * FROM calls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) | {"cached": bool(r["cached"])} for r in rows]


def credits_for(signup_id: str, *, kind: str = "crustdata") -> float:
    row = _execute(
        "SELECT COALESCE(SUM(credits), 0) AS total FROM calls WHERE signup_id = ? AND kind = ?",
        (signup_id, kind),
    ).fetchone()
    return float(row["total"])


def total_credits() -> float:
    row = _execute(
        "SELECT COALESCE(SUM(credits), 0) AS total FROM calls WHERE kind IN ('crustdata', 'sandbox')"
    ).fetchone()
    return float(row["total"])


def add_human_item(signup_id: str, reason: str, detail: str = "") -> None:
    _execute(
        "INSERT INTO human_queue (signup_id, reason, detail, created_at) VALUES (?, ?, ?, ?)",
        (signup_id, reason, detail, time.time()),
    )


def list_human_items() -> list[dict[str, Any]]:
    rows = _execute(
        "SELECT h.*, s.email_raw FROM human_queue h JOIN signups s ON s.id = h.signup_id"
        " ORDER BY h.created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def cache_get(key: str) -> Any | None:
    row = _execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def cache_keys() -> list[str]:
    return [row["key"] for row in _execute("SELECT key FROM cache").fetchall()]


def cache_set(key: str, value: Any) -> None:
    _execute(
        "INSERT OR REPLACE INTO cache (key, value, created_at) VALUES (?, ?, ?)",
        (key, json.dumps(value, default=str), time.time()),
    )


def get_pill_run(token: str, pill_id: str) -> dict[str, Any] | None:
    row = _execute(
        "SELECT result FROM pill_runs WHERE token = ? AND pill_id = ?", (token, pill_id)
    ).fetchone()
    return json.loads(row["result"]) if row else None


def count_pill_runs(token: str) -> int:
    row = _execute("SELECT COUNT(*) AS n FROM pill_runs WHERE token = ?", (token,)).fetchone()
    return int(row["n"])


def save_pill_run(token: str, pill_id: str, result: dict[str, Any]) -> None:
    _execute(
        "INSERT OR REPLACE INTO pill_runs (token, pill_id, result, created_at) VALUES (?, ?, ?, ?)",
        (token, pill_id, json.dumps(result, default=str), time.time()),
    )


# ---- Founder watches ----


def _row_with_data(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item.update(json.loads(item.pop("data")))
    return item


def claim_watch(signup_id: str, dataset: str, subject: str, **fields: Any) -> dict[str, Any] | None:
    """One watch per person or company, whoever signs up for it. None if it's already watched."""
    cursor = _execute(
        "INSERT OR IGNORE INTO watches (signup_id, dataset, subject, status, created_at, data)"
        " VALUES (?, ?, ?, 'creating', ?, ?)",
        (signup_id, dataset, subject, time.time(), json.dumps(fields, default=str)),
    )
    if not cursor.rowcount:
        return None
    return get_watch(cursor.lastrowid)


def get_watch(watch_id: int) -> dict[str, Any] | None:
    row = _execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
    return _row_with_data(row) if row else None


def find_watch(dataset: str, subject: str) -> dict[str, Any] | None:
    row = _execute("SELECT * FROM watches WHERE dataset = ? AND subject = ?", (dataset, subject)).fetchone()
    return _row_with_data(row) if row else None


def list_watches(*, signup_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    sql, params = "SELECT * FROM watches WHERE 1 = 1", []
    if signup_id:
        sql, params = f"{sql} AND signup_id = ?", [*params, signup_id]
    if status:
        sql, params = f"{sql} AND status = ?", [*params, status]
    return [_row_with_data(r) for r in _execute(f"{sql} ORDER BY id", tuple(params)).fetchall()]


_WATCH_COLUMNS = {"status", "crustdata_id", "last_run_id"}


def update_watch(watch_id: int, **fields: Any) -> None:
    columns = {k: v for k, v in fields.items() if k in _WATCH_COLUMNS}
    extras = {k: v for k, v in fields.items() if k not in _WATCH_COLUMNS}
    with _lock:
        conn = connect()
        if columns:
            assignments = ", ".join(f"{k} = ?" for k in columns)
            conn.execute(f"UPDATE watches SET {assignments} WHERE id = ?", (*columns.values(), watch_id))
        if extras:
            row = conn.execute("SELECT data FROM watches WHERE id = ?", (watch_id,)).fetchone()
            if row:
                data = json.loads(row["data"]) | extras
                conn.execute("UPDATE watches SET data = ? WHERE id = ?", (json.dumps(data, default=str), watch_id))


def add_watch_event(uid: str, watch_id: int, signup_id: str, run_id: int | None, **fields: Any) -> bool:
    """False if we've seen this notification before, so a re-read run never acts twice."""
    cursor = _execute(
        "INSERT OR IGNORE INTO watch_events (uid, watch_id, signup_id, run_id, status, created_at, data)"
        " VALUES (?, ?, ?, ?, 'new', ?, ?)",
        (uid, watch_id, signup_id, run_id, time.time(), json.dumps(fields, default=str)),
    )
    return bool(cursor.rowcount)


def get_watch_event(uid: str) -> dict[str, Any] | None:
    row = _execute("SELECT * FROM watch_events WHERE uid = ?", (uid,)).fetchone()
    return _row_with_data(row) if row else None


def list_watch_events(*, signup_id: str | None = None, statuses: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    sql, params = "SELECT * FROM watch_events WHERE 1 = 1", []
    if signup_id:
        sql, params = f"{sql} AND signup_id = ?", [*params, signup_id]
    if statuses:
        sql, params = f"{sql} AND status IN ({', '.join('?' for _ in statuses)})", [*params, *statuses]
    return [_row_with_data(r) for r in _execute(f"{sql} ORDER BY created_at, uid", tuple(params)).fetchall()]


def update_watch_event(uid: str, **fields: Any) -> None:
    with _lock:
        conn = connect()
        if "status" in fields:
            conn.execute("UPDATE watch_events SET status = ? WHERE uid = ?", (fields.pop("status"), uid))
        if fields:
            row = conn.execute("SELECT data FROM watch_events WHERE uid = ?", (uid,)).fetchone()
            if row:
                data = json.loads(row["data"]) | fields
                conn.execute("UPDATE watch_events SET data = ? WHERE uid = ?", (json.dumps(data, default=str), uid))


def claim_event_email(uid: str) -> bool:
    """Atomically marks the event as sending. Only one caller ever gets True, so nobody is emailed twice."""
    with _lock:
        conn = connect()
        row = conn.execute("SELECT data FROM watch_events WHERE uid = ?", (uid,)).fetchone()
        if row is None:
            return False
        data = json.loads(row["data"])
        if data.get("email_status"):
            return False
        data["email_status"] = "sending"
        conn.execute("UPDATE watch_events SET data = ? WHERE uid = ?", (json.dumps(data, default=str), uid))
        return True


def reset_demo(*, clear_cache: bool = False) -> None:
    """Clears signups and logs. The response cache stays unless asked, so live reruns are free."""
    tables = ["signups", "calls", "human_queue", "pill_runs", "identity_claims", "watches", "watch_events"]
    if clear_cache:
        tables.append("cache")
    with _lock:
        conn = connect()
        for table in tables:
            conn.execute(f"DELETE FROM {table}")
