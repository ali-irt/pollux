"""
app/routes/song.py
Song generation endpoint (alias router, not registered in main.py — see music.py).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import (
    MAX_SONG_LYRICS_LENGTH,
    ACE_STEP_STYLE_TAGS,
    ACE_STEP_QUALITY_PRESETS,
    ACE_STEP_MAX_DURATION,
)
from app.security import get_current_user
from app.jobs import _create_job, _job_executor
from app.ai.workers import _job_generate_song

logger = logging.getLogger(__name__)

router = APIRouter()


class SongGenerateRequest(BaseModel):
    lyrics: str
    style: str = "pop"
    audio_duration: int = 60
    quality: str = "balanced"
    language: str = "english"  # e.g. english, urdu, hindi, spanish, french


@router.post("/api/generate_song", summary="Generate a full AI song with vocals via ACE-Step 1.5")
def generate_song(request: SongGenerateRequest, user=Depends(get_current_user)):
    if not request.lyrics or not request.lyrics.strip():
        raise HTTPException(status_code=400, detail="Lyrics cannot be empty.")
    if len(request.lyrics) > MAX_SONG_LYRICS_LENGTH:
        raise HTTPException(status_code=400, detail=f"Lyrics exceed {MAX_SONG_LYRICS_LENGTH} characters.")
    if request.style not in ACE_STEP_STYLE_TAGS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown style '{request.style}'. Choose from: {list(ACE_STEP_STYLE_TAGS.keys())}",
        )
    if request.quality not in ACE_STEP_QUALITY_PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown quality '{request.quality}'. Choose from: {list(ACE_STEP_QUALITY_PRESETS.keys())}",
        )
    if not (10 <= request.audio_duration <= ACE_STEP_MAX_DURATION):
        raise HTTPException(status_code=400, detail=f"audio_duration must be 10–{ACE_STEP_MAX_DURATION} seconds.")

    job_id = _create_job(user["id"], "song")
    _job_executor.submit(
        _job_generate_song,
        job_id, user["id"], request.lyrics, request.style,
        request.audio_duration, request.quality, request.language,
    )
    logger.info(
        "Song job %s queued for user %s (style=%s, quality=%s, duration=%ds)",
        job_id, user["email"], request.style, request.quality, request.audio_duration,
    )
    return {"job_id": job_id, "status": "pending", "poll_url": f"/api/jobs/{job_id}"}


@router.get("/api/song/styles", summary="List available styles for song generation")
def list_song_styles(user=Depends(get_current_user)):
    return {
        "model": "ace-step-1.5",
        "styles": [
            {"id": k, "label": k.replace("_", " ").title()}
            for k in ACE_STEP_STYLE_TAGS
        ],
    }
