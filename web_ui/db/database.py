import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "pollux.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plan TEXT DEFAULT 'free',
            generation_count INTEGER DEFAULT 0,
            credits INTEGER DEFAULT 50,  -- New: Initial free credits
            is_premium BOOLEAN DEFAULT FALSE, -- New: Premium status
            created_at TEXT
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            model TEXT NOT NULL,
            text_snippet TEXT,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS failed_login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            attempt_time TEXT NOT NULL,
            ip_address TEXT
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS locked_accounts (
            email TEXT PRIMARY KEY,
            locked_until TEXT NOT NULL
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS cloned_voices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            reference_filename TEXT NOT NULL,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            generation_id INTEGER,
            error_message TEXT,
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """
    )

    # Migrate: add audio_data and audio_format columns if not present
    for col_def in [
        ("audio_data", "BLOB"),
        ("audio_format", "TEXT DEFAULT ''"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE generations ADD COLUMN {col_def[0]} {col_def[1]}")
        except Exception:
            pass  # column already exists

    conn.commit()
    conn.close()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
