"""
db/database.py
SQLite connection helper.

Exposes the same .execute() / .fetchone() / .fetchall() / .commit() / .close()
API as before so no route files need changing.
"""
import os
import sqlite3
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent

DB_PATH: Path = Path(os.environ.get("DB_PATH", str(BASE_DIR / "pollux.db")))

# Kept for backwards-compat with anything that imported DATABASE_URL from db.
DATABASE_URL: str = f"sqlite:///{DB_PATH}"

# One connection per thread; SQLite is not safe to share across threads.
_local = threading.local()

_pool = None  # unused for SQLite; kept so main.py shutdown cleanup doesn't crash


# ---------------------------------------------------------------------------
# _SQLiteConn — thin wrapper matching the MySQL connection API used app-wide
# ---------------------------------------------------------------------------

class _SQLiteConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._cur: sqlite3.Cursor | None = None

    def execute(self, query: str, params=()):
        # MySQL used %s placeholders; SQLite uses ? — normalise both
        query = query.replace("%s", "?")
        self._cur = self._conn.execute(query, params or ())
        return self

    def fetchone(self):
        if self._cur is None:
            return None
        row = self._cur.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        if self._cur is None:
            return []
        return [dict(r) for r in self._cur.fetchall()]

    @property
    def lastrowid(self) -> int | None:
        return self._cur.lastrowid if self._cur else None

    def commit(self):
        self._conn.commit()

    def close(self):
        # Return thread-local connection to idle state (don't actually close;
        # reuse it on the next request in this thread).
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def _get_raw_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def get_db() -> _SQLiteConn:
    return _SQLiteConn(_get_raw_conn())


def _try_execute(conn: _SQLiteConn, sql: str) -> None:
    try:
        conn.execute(sql)
    except Exception:
        pass


def init_db():
    """Create tables if they do not already exist."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                email             TEXT UNIQUE NOT NULL,
                password_hash     TEXT NOT NULL,
                plan              TEXT DEFAULT 'free',
                generation_count  INTEGER DEFAULT 0,
                credits           INTEGER DEFAULT 50,
                is_premium        INTEGER DEFAULT 0,
                created_at        TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS generations (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                filename      TEXT NOT NULL,
                model         TEXT NOT NULL,
                text_snippet  TEXT,
                created_at    TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS failed_login_attempts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                email        TEXT NOT NULL,
                attempt_time TEXT NOT NULL,
                ip_address   TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS locked_accounts (
                email        TEXT PRIMARY KEY,
                locked_until TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloned_voices (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id            INTEGER NOT NULL,
                name               TEXT NOT NULL,
                reference_filename TEXT NOT NULL,
                created_at         TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id            TEXT PRIMARY KEY,
                user_id       INTEGER NOT NULL,
                type          TEXT NOT NULL,
                status        TEXT DEFAULT 'pending',
                generation_id INTEGER,
                result_path   TEXT,
                error_message TEXT,
                created_at    TEXT,
                updated_at    TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        _try_execute(conn, "ALTER TABLE jobs ADD COLUMN result_path TEXT")

        _try_execute(conn, "CREATE INDEX IF NOT EXISTS idx_jobs_user_status ON jobs (user_id, status)")
        _try_execute(conn, "CREATE INDEX IF NOT EXISTS idx_generations_user ON generations (user_id, created_at)")
