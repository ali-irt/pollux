"""
app/routes/history.py
Generation history endpoints.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.security import get_current_user
from db import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/history", summary="Paginated generation history")
def get_history(
    user=Depends(get_current_user),
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),
    per_page: int = Query(default=20, ge=1, le=100, description="Items per page"),
):
    offset = (page - 1) * per_page
    conn = get_db()
    total = conn.execute(
        "SELECT COUNT(*) as cnt FROM generations WHERE user_id = ? AND model NOT IN ('__batch_zip__')",
        (user["id"],),
    ).fetchone()["cnt"]
    rows = conn.execute(
        "SELECT id, user_id, filename, model, text_snippet, created_at"
        " FROM generations WHERE user_id = ? AND model NOT IN ('__batch_zip__')"
        " ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (user["id"], per_page, offset),
    ).fetchall()
    conn.close()
    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": max(1, -(-total // per_page)),
        "items": [dict(r) for r in rows],
    }


@router.delete("/api/history/{entry_id}", summary="Delete a history entry")
def delete_history_entry(entry_id: int, user=Depends(get_current_user)):
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM generations WHERE id = ? AND user_id = ?", (entry_id, user["id"])
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="History entry not found.")
    conn.execute("DELETE FROM generations WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    logger.info(f"History entry {entry_id} deleted for user {user['email']}")
    return {"success": True, "deleted_id": entry_id}


@router.delete("/api/history", summary="Clear all history for current user")
def clear_history(user=Depends(get_current_user)):
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) as cnt FROM generations WHERE user_id = ?", (user["id"],)
    ).fetchone()["cnt"]
    conn.execute("DELETE FROM generations WHERE user_id = ?", (user["id"],))
    conn.commit()
    conn.close()
    logger.info(f"All history cleared for user {user['email']}: {count} items")
    return {"success": True, "deleted_count": count}
