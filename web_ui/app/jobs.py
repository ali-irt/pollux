"""
app/jobs.py
ThreadPoolExecutor, job lifecycle helpers, generation persistence, and
thread-based timeout wrapper (Windows + Linux compatible).
"""
import concurrent.futures
import logging
import secrets
import threading
from datetime import datetime

from app.config import JOB_TIMEOUT_SONG, JOB_TIMEOUT_VOICE_CLONE  # noqa: F401
from db import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Executor — max 3 heavy AI jobs running concurrently
# ---------------------------------------------------------------------------

_job_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="pollux_job"
)

# ---------------------------------------------------------------------------
# Job lifecycle helpers
# ---------------------------------------------------------------------------


def _count_active_jobs(user_id: int, job_type: str | None = None) -> int:
    """Return the number of pending/processing jobs for a user (optionally filtered by type)."""
    conn = get_db()
    if job_type:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE user_id=? AND type=? AND status IN ('pending','processing')",
            (user_id, job_type),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE user_id=? AND status IN ('pending','processing')",
            (user_id,),
        ).fetchone()
    conn.close()
    return row["count"] if row else 0


def _create_job(user_id: int, job_type: str) -> str:
    job_id = secrets.token_hex(8)
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO jobs (id, user_id, type, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (job_id, user_id, job_type, "pending", now, now),
    )
    conn.commit()
    conn.close()
    return job_id


def _update_job(job_id: str, status: str, generation_id: int = None,
                result_path: str = None, error_message: str = None):
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE jobs SET status=?, generation_id=?, result_path=?, error_message=?, updated_at=? WHERE id=?",
        (status, generation_id, result_path, error_message, now, job_id),
    )
    conn.commit()
    conn.close()


def _save_generation(user_id: int, model_label: str, snippet: str) -> int:
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute("UPDATE users SET generation_count = generation_count + 1 WHERE id = ?", (user_id,))
    cur = conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at)"
        " VALUES (?,?,?,?,?)",
        (user_id, "", model_label, snippet[:100], now),
    )
    gen_id = cur.lastrowid
    conn.commit()
    conn.close()
    return gen_id


# ---------------------------------------------------------------------------
# Thread-based timeout (works on Windows where signal.alarm is unavailable)
# ---------------------------------------------------------------------------


def _run_with_timeout(fn, timeout_secs: int, *args, **kwargs):
    """Run fn(*args, **kwargs) in a worker thread.
    Raises TimeoutError if it does not complete within timeout_secs."""
    result_box, error_box = [None], [None]

    def _target():
        try:
            result_box[0] = fn(*args, **kwargs)
        except Exception as exc:
            error_box[0] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout_secs)
    if t.is_alive():
        raise TimeoutError(f"Operation timed out after {timeout_secs}s")
    if error_box[0]:
        raise error_box[0]
    return result_box[0]
