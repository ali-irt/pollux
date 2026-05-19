"""
app/routes/music.py
Song generation endpoint powered by ACE-Step 1.5.
Produces full songs with real vocals — no external APIs required.
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
from app.jobs import _create_job, _job_executor, _count_active_jobs
from app.ai.workers import _job_generate_song

MAX_ACTIVE_SONG_JOBS_PER_USER = 1

logger = logging.getLogger(__name__)

router = APIRouter()


class SongGenerateRequest(BaseModel):
    lyrics: str
    style: str = "pop"
    audio_duration: int = 30   # seconds (10–240); keep short on CPU
    quality: str = "fast"      # turbo | fast | balanced | best
    language: str = "english"  # e.g. english, urdu, hindi, spanish, french


@router.post("/api/generate_music", summary="Generate a full AI song with vocals via ACE-Step 1.5 (returns job_id)")
def generate_music(request: SongGenerateRequest, user=Depends(get_current_user)):
    if not user["is_premium"]:
        raise HTTPException(
            status_code=403,
            detail="Song generation is a premium feature. Upgrade at /api/payments/create-checkout.",
        )

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

    if _count_active_jobs(user["id"], "song") >= MAX_ACTIVE_SONG_JOBS_PER_USER:
        raise HTTPException(status_code=429, detail="You already have a song generation in progress. Please wait for it to complete.")

    job_id = _create_job(user["id"], "song")
    _job_executor.submit(
        _job_generate_song,
        job_id, user["id"], request.lyrics, request.style,
        request.audio_duration, request.quality, request.language,
    )
    logger.info(
        "Song job %s queued for user %s (style=%s, quality=%s, duration=%ds, lang=%s)",
        job_id, user["email"], request.style, request.quality, request.audio_duration, request.language,
    )
    return {"job_id": job_id, "status": "pending", "poll_url": f"/api/jobs/{job_id}"}


@router.post("/api/generate_song", summary="Alias for /api/generate_music")
def generate_song_alias(request: SongGenerateRequest, user=Depends(get_current_user)):
    return generate_music(request, user)


@router.get("/api/music/options", summary="List available styles and quality presets for song generation")
def list_music_options(user=Depends(get_current_user)):
    return {
        "model": "ace-step-1.5",
        "max_duration": ACE_STEP_MAX_DURATION,
        "styles": [
            {"id": k, "label": k.replace("_", " ").title(), "tags": v}
            for k, v in ACE_STEP_STYLE_TAGS.items()
        ],
        "quality_presets": [
            {
                "id": k,
                "label": k.title(),
                "infer_steps": v[0],
                "scheduler": v[1],
                "guidance_scale": v[2],
            }
            for k, v in ACE_STEP_QUALITY_PRESETS.items()
        ],
    }
