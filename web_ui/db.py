import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "db" / "pollux.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    # optional: no-op if schema already exists
    return true
