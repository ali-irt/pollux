"""
app/routes/voxcpm.py
VoxCPM-Demo voice cloning microservice proxy routes.
Forwards requests to the XTTS-v2 server running on port 8008.
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import (
    MAX_TEXT_LENGTH,
    VOXCPM_ALLOWED_EXT,
    VOXCPM_MAX_AUDIO_BYTES,
)
from app.security import get_current_user

logger = logging.getLogger(__name__)
limiter = Limiter(key_func=get_remote_address)

router = APIRouter(prefix="/voice", tags=["Voice Cloning (VoxCPM)"])


@router.get("/health", summary="Check if the VoxCPM voice cloning backend is reachable")
def voice_health():
    from app.voice_cloning_client import is_available
    if not is_available():
        raise HTTPException(status_code=503, detail="Voice cloning service unavailable")
    return {"status": "ok", "service": "voxcpm"}


@router.post("/clone", summary="Clone a voice and synthesise speech (returns MP3)")
@limiter.limit("5/minute")
async def voice_clone(
    request: Request,
    text: str = Form(..., description="Text to synthesise"),
    reference: UploadFile = File(..., description="Reference WAV/MP3 clip (3–30 s)"),
    user=Depends(get_current_user),
):
    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")
    if len(text) > MAX_TEXT_LENGTH:
        raise HTTPException(status_code=400, detail=f"Text exceeds {MAX_TEXT_LENGTH} character limit.")

    ext = Path(reference.filename or "").suffix.lower()
    if ext not in VOXCPM_ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Unsupported format '{ext}'. Allowed: {sorted(VOXCPM_ALLOWED_EXT)}")

    audio_bytes = await reference.read()
    if len(audio_bytes) > VOXCPM_MAX_AUDIO_BYTES:
        raise HTTPException(status_code=400, detail="Reference audio exceeds 25 MB.")

    from app.voice_cloning_client import clone_voice
    try:
        mp3 = clone_voice(text=text.strip(), ref_audio_bytes=audio_bytes, ref_audio_format=ext.lstrip(".") or "wav")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Voice cloning failed: {exc}")

    logger.info("VoxCPM clone: %d chars for user %s", len(text), user["id"])
    return Response(content=mp3, media_type="audio/mpeg")


@router.post("/transcribe", summary="Transcribe audio to text via VoxCPM")
@limiter.limit("10/minute")
async def voice_transcribe(
    request: Request,
    audio: UploadFile = File(..., description="Audio file to transcribe"),
    user=Depends(get_current_user),
):
    ext = Path(audio.filename or "").suffix.lower()
    if ext not in VOXCPM_ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Unsupported format '{ext}'. Allowed: {sorted(VOXCPM_ALLOWED_EXT)}")

    audio_bytes = await audio.read()
    if len(audio_bytes) > VOXCPM_MAX_AUDIO_BYTES:
        raise HTTPException(status_code=400, detail="Audio file exceeds 25 MB.")

    from app.voice_cloning_client import transcribe
    try:
        text = transcribe(audio_bytes=audio_bytes, audio_format=ext.lstrip(".") or "wav")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Transcription failed: {exc}")

    return {"text": text}


@router.post("/diarize", summary="Transcribe audio with speaker labels via VoxCPM")
@limiter.limit("5/minute")
async def voice_diarize(
    request: Request,
    audio: UploadFile = File(..., description="Audio file (supports multiple speakers)"),
    num_speakers: int = Form(None, description="Expected number of speakers (optional hint)"),
    user=Depends(get_current_user),
):
    ext = Path(audio.filename or "").suffix.lower()
    if ext not in VOXCPM_ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Unsupported format '{ext}'. Allowed: {sorted(VOXCPM_ALLOWED_EXT)}")

    audio_bytes = await audio.read()
    if len(audio_bytes) > VOXCPM_MAX_AUDIO_BYTES:
        raise HTTPException(status_code=400, detail="Audio file exceeds 25 MB.")

    from app.voice_cloning_client import transcribe_with_diarization
    try:
        result = transcribe_with_diarization(
            audio_bytes=audio_bytes,
            audio_format=ext.lstrip(".") or "wav",
            num_speakers=num_speakers,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Diarization failed: {exc}")

    return result
