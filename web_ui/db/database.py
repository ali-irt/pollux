"""
db/database.py
PostgreSQL connection pool and schema init.

Uses a _PgConn wrapper that exposes the same .execute() / .fetchone() /
.fetchall() / .commit() / .close() API as the old sqlite3 connection, so
route files need no changes.  Parameter placeholders are auto-converted
from SQLite's '?' to PostgreSQL's '%s'.
"""
import os
import threading
from pathlib import Path

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor

# ---------------------------------------------------------------------------
# Config — read from env, fall back to a localhost default for local dev
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://pollux:pollux@localhost:5432/pollux",
)

# Kept for backwards-compat with anything that imported DB_PATH from db.
DB_PATH = Path(DATABASE_URL)

# ---------------------------------------------------------------------------
# Connection pool (created once at first use)
# ---------------------------------------------------------------------------

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=2,
                maxconn=20,
                dsn=DATABASE_URL,
            )
    return _pool


# ---------------------------------------------------------------------------
# _PgConn — thin wrapper that matches the sqlite3 connection API
# ---------------------------------------------------------------------------

class _PgConn:
    """Wraps a pooled psycopg2 connection to match the SQLite API used
    throughout the app.  Call .close() to return the connection to the pool.
    """

    def __init__(self, raw_conn, pool: psycopg2.pool.ThreadedConnectionPool):
        self._conn = raw_conn
        self._pool = pool
        self._cur: psycopg2.extensions.cursor | None = None

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def execute(self, query: str, params=()):
        """Execute a query.  '?' placeholders are auto-mapped to '%s'."""
        self._cur = self._conn.cursor(cursor_factory=RealDictCursor)
        pg_query = query.replace("?", "%s")
        self._cur.execute(pg_query, params or ())
        return self  # allow chaining: conn.execute(...).fetchone()

    def fetchone(self):
        if self._cur is None:
            return None
        row = self._cur.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        if self._cur is None:
            return []
        return [dict(r) for r in self._cur.fetchall()]

    @property
    def lastrowid(self) -> int | None:
        """Return the id from the most recent INSERT … RETURNING id."""
        if self._cur is None:
            return None
        row = self._cur.fetchone()
        if row is None:
            return None
        return row.get("id") or row.get(0)

    def commit(self):
        self._conn.commit()

    def close(self):
        if self._cur and not self._cur.closed:
            self._cur.close()
        self._pool.putconn(self._conn)

    # ------------------------------------------------------------------
    # Context manager support (optional but handy)
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        self.close()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_db() -> _PgConn:
    """Return a pooled connection wrapped in the SQLite-compatible API."""
    pool = _get_pool()
    raw = pool.getconn()
    raw.autocommit = False
    return _PgConn(raw, pool)


def init_db():
    """Create tables if they do not already exist."""
    with get_db() as conn:
        # users
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id                SERIAL PRIMARY KEY,
                email             TEXT UNIQUE NOT NULL,
                password_hash     TEXT NOT NULL,
                plan              TEXT DEFAULT 'free',
                generation_count  INTEGER DEFAULT 0,
                credits           INTEGER DEFAULT 50,
                is_premium        BOOLEAN DEFAULT FALSE,
                created_at        TEXT
            )
        """)

        # generations (metadata only — audio is streamed directly to the client)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS generations (
                id            SERIAL PRIMARY KEY,
                user_id       INTEGER NOT NULL REFERENCES users(id),
                filename      TEXT NOT NULL,
                model         TEXT NOT NULL,
                text_snippet  TEXT,
                created_at    TEXT
            )
        """)

        # failed_login_attempts
        conn.execute("""
            CREATE TABLE IF NOT EXISTS failed_login_attempts (
                id           SERIAL PRIMARY KEY,
                email        TEXT NOT NULL,
                attempt_time TEXT NOT NULL,
                ip_address   TEXT
            )
        """)

        # locked_accounts
        conn.execute("""
            CREATE TABLE IF NOT EXISTS locked_accounts (
                email        TEXT PRIMARY KEY,
                locked_until TEXT NOT NULL
            )
        """)

        # cloned_voices
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloned_voices (
                id                 SERIAL PRIMARY KEY,
                user_id            INTEGER NOT NULL REFERENCES users(id),
                name               TEXT NOT NULL,
                reference_filename TEXT NOT NULL,
                created_at         TEXT
            )
        """)

        # jobs
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id            TEXT PRIMARY KEY,
                user_id       INTEGER NOT NULL REFERENCES users(id),
                type          TEXT NOT NULL,
                status        TEXT DEFAULT 'pending',
                generation_id INTEGER,
                result_path   TEXT,
                error_message TEXT,
                created_at    TEXT,
                updated_at    TEXT
            )
        """)
        # Migration: add result_path to existing tables that predate this column
        conn.execute("""
            ALTER TABLE jobs ADD COLUMN IF NOT EXISTS result_path TEXT
        """)

        # indexes for hot query paths
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_user_status
                ON jobs (user_id, status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_generations_user
                ON generations (user_id, created_at DESC)
        """)
