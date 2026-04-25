"""
app/routes/music.py
Music generation endpoints (MusicGen local + FAL.AI stub).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import MAX_MUSIC_PROMPT_LENGTH
from app.security import get_current_user
from app.jobs import _create_job, _job_executor
from app.ai.workers import _job_generate_music

logger = logging.getLogger(__name__)

router = APIRouter()


class MusicGenerateRequest(BaseModel):
    prompt: str
    duration: int = 10


@router.post("/api/generate_music", summary="Generate music from a text prompt (returns job_id immediately)")
def generate_music(request: MusicGenerateRequest, user=Depends(get_current_user)):
    if not request.prompt or not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty.")
    if len(request.prompt) > MAX_MUSIC_PROMPT_LENGTH:
        raise HTTPException(status_code=400, detail=f"Prompt exceeds {MAX_MUSIC_PROMPT_LENGTH} characters.")

    job_id = _create_job(user["id"], "music")
    _job_executor.submit(_job_generate_music, job_id, user["id"], request.prompt, request.duration)
    logger.info(f"Music job {job_id} queued for user {user['email']}")
    return {"job_id": job_id, "status": "pending", "poll_url": f"/api/jobs/{job_id}"}


@router.post("/api/generate_music_fal", summary="Generate music via FAL.AI stable-audio")
def generate_music_fal(request: MusicGenerateRequest, user=Depends(get_current_user)):
    raise HTTPException(status_code=501, detail="FAL.AI music generation is not yet implemented.")
