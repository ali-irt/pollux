"""
app/routes/transcribe.py
Speech-to-text endpoint using local Whisper (HuggingFace transformers).
"""
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.config import (
    MAX_TRANSCRIBE_SIZE,
    WHISPER_ALLOWED_EXT,
    WHISPER_ALLOWED_SIZES,
)
from app.security import get_current_user
from app.ai.loaders import _load_whisper

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/transcribe", summary="Transcribe audio to text using local Whisper (no external API)")
async def transcribe_audio(
    file: UploadFile = File(..., description="Audio file to transcribe"),
    model_size: str = Form(default="base", description="Whisper model: tiny | base | small"),
    language: str = Form(default="auto", description="Language code (e.g. 'en') or 'auto' for detection"),
    user=Depends(get_current_user),
):
    if model_size not in WHISPER_ALLOWED_SIZES:
        raise HTTPException(status_code=400, detail=f"model_size must be one of: {WHISPER_ALLOWED_SIZES}")

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")
    ext = Path(file.filename).suffix.lower()
    if ext not in WHISPER_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio format '{ext}'. Allowed: {WHISPER_ALLOWED_EXT}",
        )

    content = await file.read()
    if len(content) > MAX_TRANSCRIBE_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds 50 MB limit.")

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        pipe = _load_whisper(model_size)

        generate_kwargs: dict = {"task": "transcribe"}
        if language != "auto":
            generate_kwargs["language"] = language

        result = pipe(tmp_path, generate_kwargs=generate_kwargs, return_timestamps=True)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Transcription error for user {user['email']}: {e}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink()
            except Exception:
                pass

    full_text: str = (result.get("text") or "").strip()
    chunks: list = result.get("chunks") or []

    segments = []
    for chunk in chunks:
        ts = chunk.get("timestamp") or (0, 0)
        segments.append({
            "start": ts[0] if ts[0] is not None else 0,
            "end": ts[1] if ts[1] is not None else 0,
            "text": (chunk.get("text") or "").strip(),
        })

    logger.info(f"Transcription done for user {user['email']}: {len(full_text)} chars, {len(segments)} segments")

    return {
        "success": True,
        "text": full_text,
        "segments": segments,
        "model": f"openai/whisper-{model_size}",
        "language_hint": language,
        "word_count": len(full_text.split()) if full_text else 0,
        "character_count": len(full_text),
        "segment_count": len(segments),
    }
