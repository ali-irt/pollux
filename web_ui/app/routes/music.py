"""
app/routes/music.py
Unified music generation endpoint — instrumental (MusicGen) or song with vocals (edge-tts + MusicGen).
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import MAX_MUSIC_PROMPT_LENGTH, MAX_SONG_LYRICS_LENGTH, BARK_VOICE_PRESETS, SONG_STYLE_PROMPTS
from app.security import get_current_user
from app.jobs import _create_job, _job_executor
from app.ai.workers import _job_generate_music, _job_generate_song

logger = logging.getLogger(__name__)

router = APIRouter()


class MusicGenerateRequest(BaseModel):
    prompt: str
    duration: int = 10
    # Song mode — provide lyrics to get vocals + background music
    lyrics: Optional[str] = None
    voice_preset: str = "en_singer_3"
    style: str = "pop"
    quality: str = "small"


@router.post("/api/generate_music", summary="Generate music or AI song (returns job_id immediately)")
def generate_music(request: MusicGenerateRequest, user=Depends(get_current_user)):
    if not request.prompt or not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty.")
    if len(request.prompt) > MAX_MUSIC_PROMPT_LENGTH:
        raise HTTPException(status_code=400, detail=f"Prompt exceeds {MAX_MUSIC_PROMPT_LENGTH} characters.")

    # Song mode — lyrics provided
    if request.lyrics and request.lyrics.strip():
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
        return {
            "job_id": job_id,
            "status": "pending",
            "mode": "song",
            "poll_url": f"/api/jobs/{job_id}",
        }

    # Instrumental mode
    job_id = _create_job(user["id"], "music")
    _job_executor.submit(_job_generate_music, job_id, user["id"], request.prompt, request.duration)
    logger.info(f"Music job {job_id} queued for user {user['email']}")
    return {
        "job_id": job_id,
        "status": "pending",
        "mode": "instrumental",
        "poll_url": f"/api/jobs/{job_id}",
    }


@router.post("/api/generate_song", summary="Generate AI song with vocals from lyrics (alias for /api/generate_music with lyrics)")
def generate_song_alias(request: MusicGenerateRequest, user=Depends(get_current_user)):
    """Backwards-compatible alias — prefer /api/generate_music with a lyrics field."""
    return generate_music(request, user)


@router.get("/api/music/options", summary="List available voice presets and styles for song generation")
def list_music_options(user=Depends(get_current_user)):
    return {
        "voice_presets": [
            {"id": k, "label": k.replace("_", " ").title()}
            for k in BARK_VOICE_PRESETS
        ],
        "styles": [
            {"id": k, "label": k.replace("_", " ").title()}
            for k in SONG_STYLE_PROMPTS
        ],
        "modes": [
            {"id": "instrumental", "label": "Instrumental", "icon": "🎵", "description": "Background music from a text prompt"},
            {"id": "song", "label": "AI Song", "icon": "🎤", "description": "Vocals + background music from lyrics"},
        ],
    }


@router.post("/api/generate_music_fal", summary="Generate music via FAL.AI stable-audio (not yet implemented)")
def generate_music_fal(request: MusicGenerateRequest, user=Depends(get_current_user)):
    raise HTTPException(status_code=501, detail="FAL.AI music generation is not yet implemented.")
