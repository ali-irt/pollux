"""
app/routes/song.py
Song generation endpoints (edge-tts + MusicGen, fully local).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import (
    MAX_SONG_LYRICS_LENGTH,
    BARK_VOICE_PRESETS,
    SONG_STYLE_PROMPTS,
)
from app.security import get_current_user
from app.jobs import _create_job, _job_executor
from app.ai.workers import _job_generate_song

logger = logging.getLogger(__name__)

router = APIRouter()


class SongGenerateRequest(BaseModel):
    lyrics: str
    voice_preset: str = "en_singer_3"
    style: str = "pop"
    quality: str = "small"


@router.post("/api/generate_song", summary="Generate a full AI song with vocals from lyrics (returns job_id immediately)")
def generate_song(request: SongGenerateRequest, user=Depends(get_current_user)):
    if not request.lyrics or not request.lyrics.strip():
        raise HTTPException(status_code=400, detail="Lyrics cannot be empty.")
    if len(request.lyrics) > MAX_SONG_LYRICS_LENGTH:
        raise HTTPException(status_code=400, detail=f"Lyrics exceed {MAX_SONG_LYRICS_LENGTH} characters.")
    if request.voice_preset not in BARK_VOICE_PRESETS:
        raise HTTPException(status_code=400, detail=f"Unknown voice preset. Choose from: {list(BARK_VOICE_PRESETS.keys())}")
    if request.style not in SONG_STYLE_PROMPTS:
        raise HTTPException(status_code=400, detail=f"Unknown style. Choose from: {list(SONG_STYLE_PROMPTS.keys())}")
    if request.quality not in {"small", "large"}:
        raise HTTPException(status_code=400, detail="quality must be 'small' or 'large'.")

    job_id = _create_job(user["id"], "song")
    _job_executor.submit(
        _job_generate_song, job_id, user["id"],
        request.lyrics, request.voice_preset, request.style, request.quality,
    )
    logger.info(f"Song job {job_id} queued for user {user['email']}")
    return {"job_id": job_id, "status": "pending", "poll_url": f"/api/jobs/{job_id}"}


@router.get("/api/song/voices", summary="List available Bark voice presets for song generation")
def list_song_voices(user=Depends(get_current_user)):
    return {
        "voices": [
            {"id": k, "bark_preset": v, "label": k.replace("_", " ").title()}
            for k, v in BARK_VOICE_PRESETS.items()
        ],
        "styles": [
            {"id": k, "label": k.replace("_", " ").title()}
            for k in SONG_STYLE_PROMPTS
        ],
    }
