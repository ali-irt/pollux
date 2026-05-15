"""
db/database.py
MySQL connection pool and schema init.

Uses a _MySQLConn wrapper that exposes the same .execute() / .fetchone() /
.fetchall() / .commit() / .close() API as the old sqlite3 connection, so
route files need no changes.  Parameter placeholders ('?') are auto-converted
to MySQL's '%s'.
"""
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import mysql.connector
import mysql.connector.pooling

# ---------------------------------------------------------------------------
# Config — read from env, fall back to a localhost default for local dev
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "mysql://pollux:pollux@localhost:3306/pollux",
)

# Kept for backwards-compat with anything that imported DB_PATH from db.
DB_PATH = Path(DATABASE_URL)


def _parse_url(url: str) -> dict:
    p = urlparse(url)
    return {
        "host": p.hostname or "localhost",
        "port": p.port or 3306,
        "user": p.username or "pollux",
        "password": p.password or "pollux",
        "database": (p.path or "/pollux").lstrip("/"),
    }


# ---------------------------------------------------------------------------
# Connection pool (created once at first use)
# ---------------------------------------------------------------------------

_pool: mysql.connector.pooling.MySQLConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> mysql.connector.pooling.MySQLConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            cfg = _parse_url(DATABASE_URL)
            retries, delay = 5, 2
            for attempt in range(retries):
                try:
                    _pool = mysql.connector.pooling.MySQLConnectionPool(
                        pool_name="pollux",
                        pool_size=20,
                        pool_reset_session=True,
                        autocommit=False,
                        **cfg,
                    )
                    break
                except mysql.connector.Error:
                    if attempt == retries - 1:
                        raise
                    time.sleep(delay * (attempt + 1))
    return _pool


# ---------------------------------------------------------------------------
# _MySQLConn — thin wrapper that matches the sqlite3 connection API
# ---------------------------------------------------------------------------

class _MySQLConn:
    """Wraps a pooled mysql-connector connection to match the SQLite API used
    throughout the app.  Call .close() to return the connection to the pool.
    """

    def __init__(self, raw_conn):
        self._conn = raw_conn
        self._cur = None

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def execute(self, query: str, params=()):
        """Execute a query.  '?' placeholders are auto-mapped to '%s'."""
        self._cur = self._conn.cursor(dictionary=True)
        self._cur.execute(query.replace("?", "%s"), params or ())
        return self

    def fetchone(self):
        if self._cur is None:
            return None
        return self._cur.fetchone()

    def fetchall(self):
        if self._cur is None:
            return []
        return self._cur.fetchall() or []

    @property
    def lastrowid(self) -> int | None:
        if self._cur is None:
            return None
        return self._cur.lastrowid

    def commit(self):
        self._conn.commit()

    def close(self):
        if self._cur:
            try:
                self._cur.close()
            except Exception:
                pass
        self._conn.close()  # returns connection to pool

    # ------------------------------------------------------------------
    # Context manager support
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

def get_db() -> _MySQLConn:
    """Return a pooled connection wrapped in the SQLite-compatible API."""
    pool = _get_pool()
    return _MySQLConn(pool.get_connection())


def _try_execute(conn: _MySQLConn, sql: str) -> None:
    """Run a DDL statement, ignoring duplicate-column / duplicate-index errors."""
    try:
        conn.execute(sql)
    except mysql.connector.Error:
        pass


def init_db():
    """Create tables if they do not already exist."""
    with get_db() as conn:
        # users
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id                INT AUTO_INCREMENT PRIMARY KEY,
                email             VARCHAR(255) UNIQUE NOT NULL,
                password_hash     TEXT NOT NULL,
                plan              VARCHAR(50) DEFAULT 'free',
                generation_count  INT DEFAULT 0,
                credits           INT DEFAULT 50,
                is_premium        TINYINT(1) DEFAULT 0,
                created_at        TEXT
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        # generations
        conn.execute("""
            CREATE TABLE IF NOT EXISTS generations (
                id            INT AUTO_INCREMENT PRIMARY KEY,
                user_id       INT NOT NULL,
                filename      TEXT NOT NULL,
                model         TEXT NOT NULL,
                text_snippet  TEXT,
                created_at    TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        # failed_login_attempts
        conn.execute("""
            CREATE TABLE IF NOT EXISTS failed_login_attempts (
                id           INT AUTO_INCREMENT PRIMARY KEY,
                email        VARCHAR(255) NOT NULL,
                attempt_time TEXT NOT NULL,
                ip_address   TEXT
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        # locked_accounts
        conn.execute("""
            CREATE TABLE IF NOT EXISTS locked_accounts (
                email        VARCHAR(255) PRIMARY KEY,
                locked_until TEXT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        # cloned_voices
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloned_voices (
                id                 INT AUTO_INCREMENT PRIMARY KEY,
                user_id            INT NOT NULL,
                name               TEXT NOT NULL,
                reference_filename TEXT NOT NULL,
                created_at         TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        # jobs
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id            VARCHAR(191) PRIMARY KEY,
                user_id       INT NOT NULL,
                type          TEXT NOT NULL,
                status        VARCHAR(50) DEFAULT 'pending',
                generation_id INT,
                result_path   TEXT,
                error_message TEXT,
                created_at    TEXT,
                updated_at    TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        # Migration: add result_path for tables predating this column
        _try_execute(conn, "ALTER TABLE jobs ADD COLUMN result_path TEXT")

        # Indexes
        _try_execute(conn, "CREATE INDEX idx_jobs_user_status ON jobs (user_id, status)")
        _try_execute(conn, "CREATE INDEX idx_generations_user ON generations (user_id, created_at(100))")
