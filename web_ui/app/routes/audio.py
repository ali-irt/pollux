"""
app/routes/audio.py
Audio download endpoint for completed background jobs.
Audio is stored on disk temporarily and deleted after the client downloads it.
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from app.security import get_current_user
from db import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/jobs/{job_id}/download", summary="Download audio for a completed job (one-time, deleted after)")
def download_job_audio(job_id: str, user=Depends(get_current_user)):
    conn = get_db()
    job = conn.execute(
        "SELECT status, result_path FROM jobs WHERE id = ? AND user_id = ?",
        (job_id, user["id"]),
    ).fetchone()
    conn.close()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail=f"Job is not complete (status: {job['status']}).")
    if not job["result_path"]:
        raise HTTPException(status_code=404, detail="No audio file associated with this job.")

    path = Path(job["result_path"])
    if not path.exists():
        raise HTTPException(status_code=410, detail="Audio already downloaded or expired.")

    audio_bytes = path.read_bytes()
    try:
        path.unlink()
    except Exception:
        pass

    logger.info("Job %s audio downloaded by user %s", job_id, user["id"])
    return Response(
        content=audio_bytes,
        media_type="audio/wav",
        headers={"Content-Disposition": f"attachment; filename={path.name}"},
    )
