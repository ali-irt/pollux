"""
app/routes/audio.py
Audio stream, download, and delete endpoints.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from app.security import get_current_user, get_current_user_audio
from db import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/audio/{generation_id}", summary="Stream audio from DB")
def get_audio(generation_id: int, user=Depends(get_current_user_audio)):
    conn = get_db()
    row = conn.execute(
        "SELECT audio_data, audio_format FROM generations WHERE id = ? AND user_id = ?",
        (generation_id, user["id"]),
    ).fetchone()
    conn.close()

    if not row or not row["audio_data"]:
        raise HTTPException(status_code=404, detail="Audio not found.")

    fmt = row["audio_format"] or "wav"
    media_type = "audio/mpeg" if fmt == "mp3" else "audio/wav"
    return Response(
        content=bytes(row["audio_data"]),
        media_type=media_type,
        headers={"Content-Disposition": f"inline; filename=output.{fmt}"},
    )


@router.get("/api/audio/{generation_id}/download", summary="Download audio from DB and remove from storage")
def download_audio(generation_id: int, user=Depends(get_current_user_audio)):
    conn = get_db()
    row = conn.execute(
        "SELECT id, audio_data, audio_format FROM generations WHERE id = ? AND user_id = ?",
        (generation_id, user["id"]),
    ).fetchone()

    if not row or not row["audio_data"]:
        conn.close()
        raise HTTPException(status_code=404, detail="Audio not found.")

    audio_bytes = bytes(row["audio_data"])
    fmt = row["audio_format"] or "wav"
    media_type = "audio/mpeg" if fmt == "mp3" else "audio/wav"

    # Delete from DB once exported to the client
    conn.execute("DELETE FROM generations WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()

    logger.info(f"Audio {generation_id} exported and removed from DB for user {user['email']}")
    return Response(
        content=audio_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename=output_{generation_id}.{fmt}"},
    )


@router.delete("/api/audio/{generation_id}", summary="Delete audio from DB")
def delete_audio(generation_id: int, user=Depends(get_current_user)):
    conn = get_db()
    generation = conn.execute(
        "SELECT id FROM generations WHERE id = ? AND user_id = ?",
        (generation_id, user["id"]),
    ).fetchone()

    if not generation:
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied.")

    conn.execute("DELETE FROM generations WHERE id = ?", (generation["id"],))
    conn.commit()
    conn.close()

    logger.info(f"Audio {generation_id} deleted for user {user['email']}")
    return {"success": True, "deleted_id": generation["id"]}
