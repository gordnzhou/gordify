"""
SQLite-backed job queue.
"""

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.environ.get("JOB_DB_PATH")
_STATUSES = ("queued", "running", "finished", "failed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _connect() as conn:
        # WAL for concurrent read/writes, allowing for decoupled task enqueueing/popping ops
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                query TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                logs TEXT,
                error TEXT,
                rescan_triggered INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_username ON jobs(username, created_at)")


def complete_job(job_id: str, *, 
    status: str, 
    logs: str = "", 
    error: str | None = None, 
    rescan_triggered: bool = False
) -> None:
    """
    Updates job in DB with the given fields. NOTE: `status` must be in ("finished", "failed")
    """
    assert status in ("finished", "failed")
    with _connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status=?, finished_at=?, logs=?, error=?, rescan_triggered=?
            WHERE id=?
            """,
            (status, _now(), logs, error, int(rescan_triggered), job_id),
        )


def claim_next_job() -> dict | None:
    """
    Atomically grab the oldest queued job and mark it running. Returns
    None if there's nothing to do. Safe to call from multiple worker
    processes concurrently.
    """
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT id, username, query FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        
        cur = conn.execute(
            "UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
            (_now(), row["id"]),
        )
        if cur.rowcount == 0:
            return None # another worker beat us to it between the SELECT and UPDATE
        
        return dict(row)


def create_job(username: str, query: str) -> str:
    job_id = uuid.uuid4().hex

    with _connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id, username, query, status, created_at) VALUES (?, ?, ?, 'queued', ?)",
            (job_id, username, query, _now()),
        )
        
    return job_id


def delete_jobs_by_temp_users() -> int:
    """Deletes all jobs in DB created by usernames that are prefixed 'temp_'. Returns number of rows deleted"""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM jobs WHERE username LIKE 'temp_%'")
        return cursor.rowcount


def job_already_exists(username: str, query: str) -> bool:
    """
    Returns True if a job with same query and username is already running
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('queued', 'running') AND username=? AND query=?", (username, query)
        ).fetchone()
        return row is not None


def get_job_for_user(job_id: str, username: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id=? AND username=?", (job_id, username)
        ).fetchone()
        return dict(row) if row else None


def fetch_jobs(username: str = None, filter: str = None, sort: str = None, limit: int = 20) -> list[dict]:
    query_str = "SELECT *, COALESCE(unixepoch(finished_at) - unixepoch(started_at), 0) AS duration FROM jobs "
    params = []

    if username:
        query_str += "WHERE username=? "
        params.append(username)
        if filter in _STATUSES:
            query_str += f"AND status=? "
            params.append(filter)
    elif filter in _STATUSES:
        query_str += f"WHERE status=? "
        params.append(filter)

    if sort == "duration_asc":
        query_str += "ORDER BY duration "
    elif sort == "duration_desc":
        query_str += "ORDER BY duration DESC "
    elif sort == "date_asc":
        query_str += "ORDER BY created_at "
    else:
        query_str += "ORDER BY created_at DESC "

    query_str += "LIMIT ? "
    params.append(limit)

    with _connect() as conn:
        rows = conn.execute(query_str, params).fetchall()
        return [dict(r) for r in rows]