"""
app/routes/voice_clone.py
Voice cloning endpoints using XTTS v2 via the async job system.
"""
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import (
    CLONE_ALLOWED_EXT, CLONE_MAX_FILE_SIZE,
    CLONE_MIN_DURATION, CLONE_MAX_DURATION, CLONE_MAX_PER_USER,
    CLONED_VOICES_DIR, VOICE_SAMPLES_DIR,
)
from app.security import get_current_user
from app.jobs import _create_job, _job_executor, _count_active_jobs
from app.ai.workers import _job_voice_clone, _job_voice_clone_profile
from db import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

_XTTS_LANGUAGES = [
    "en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl",
    "cs", "ar", "zh", "hu", "ko", "ja", "hi", "ur",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_audio_upload(file: UploadFile, content: bytes) -> str:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in CLONE_ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Unsupported format '{ext}'. Allowed: {sorted(CLONE_ALLOWED_EXT)}")
    if len(content) > CLONE_MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="Audio file exceeds 10 MB.")
    return ext


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/api/voice_clone/upload", summary="Upload a voice sample and generate cloned TTS (async)")
async def voice_clone_upload(
    file: UploadFile = File(..., description="Reference audio (WAV/MP3/OGG/FLAC/M4A, 3–30 sec)"),
    text: str = Form(..., description="Text to synthesise in the cloned voice"),
    language: str = Form(default="en", description="Language code e.g. en, ur, hi"),
    name: str = Form(default="", description="Optional label for this voice"),
    user=Depends(get_current_user),
):
    content = await file.read()
    ext = _validate_audio_upload(file, content)

    if _count_active_jobs(user["id"], "voice_clone") >= 1:
        raise HTTPException(status_code=429, detail="A voice clone job is already in progress.")

    sample_path = VOICE_SAMPLES_DIR / f"{user['id']}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}{ext}"
    sample_path.write_bytes(content)

    voice_name = name.strip() or Path(file.filename or "voice").stem
    job_id = _create_job(user["id"], "voice_clone")
    _job_executor.submit(_job_voice_clone, job_id, user["id"], voice_name, str(sample_path), text, language)
    return {"job_id": job_id, "status": "pending", "poll_url": f"/api/jobs/{job_id}"}


@router.get("/api/voice_clone/list", summary="List saved voice samples")
def voice_clone_list(user=Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, reference_filename, created_at FROM cloned_voices WHERE user_id = ? ORDER BY created_at DESC",
        (user["id"],),
    ).fetchall()
    conn.close()
    return {"voices": [dict(r) for r in rows]}


@router.delete("/api/voice_clone/{voice_id}", summary="Delete a saved voice sample")
def voice_clone_delete(voice_id: int, user=Depends(get_current_user)):
    conn = get_db()
    row = conn.execute(
        "SELECT reference_filename FROM cloned_voices WHERE id = ? AND user_id = ?",
        (voice_id, user["id"]),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Voice not found.")
    conn.execute("DELETE FROM cloned_voices WHERE id = ?", (voice_id,))
    conn.commit()
    conn.close()
    try:
        Path(row["reference_filename"]).unlink(missing_ok=True)
    except Exception:
        pass
    return {"success": True, "deleted_id": voice_id}


@router.post("/api/voice_clone/save_profile", summary="Save a reusable voice profile from uploaded audio")
async def save_voice_profile(
    file: UploadFile = File(..., description="Reference audio"),
    name: str = Form(..., description="Name for this voice profile"),
    user=Depends(get_current_user),
):
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) as cnt FROM cloned_voices WHERE user_id = ?", (user["id"],)
    ).fetchone()["cnt"]
    if count >= CLONE_MAX_PER_USER:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Maximum {CLONE_MAX_PER_USER} saved profiles reached.")

    content = await file.read()
    ext = _validate_audio_upload(file, content)

    ref_path = CLONED_VOICES_DIR / f"{user['id']}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}{ext}"
    ref_path.write_bytes(content)

    now = datetime.utcnow().isoformat()
    cursor = conn.execute(
        "INSERT INTO cloned_voices (user_id, name, reference_filename, created_at) VALUES (?, ?, ?, ?)",
        (user["id"], name.strip(), str(ref_path), now),
    )
    conn.commit()
    profile_id = cursor.lastrowid
    conn.close()
    return {"success": True, "profile_id": profile_id, "name": name.strip()}


@router.get("/api/voice_clone/profiles", summary="List saved voice profiles")
def list_profiles(user=Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, created_at FROM cloned_voices WHERE user_id = ? ORDER BY created_at DESC",
        (user["id"],),
    ).fetchall()
    conn.close()
    return {"profiles": [dict(r) for r in rows]}


@router.delete("/api/voice_clone/profiles/{profile_id}", summary="Delete a saved voice profile")
def delete_profile(profile_id: int, user=Depends(get_current_user)):
    conn = get_db()
    row = conn.execute(
        "SELECT reference_filename FROM cloned_voices WHERE id = ? AND user_id = ?",
        (profile_id, user["id"]),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Profile not found.")
    conn.execute("DELETE FROM cloned_voices WHERE id = ?", (profile_id,))
    conn.commit()
    conn.close()
    try:
        Path(row["reference_filename"]).unlink(missing_ok=True)
    except Exception:
        pass
    return {"success": True, "deleted_id": profile_id}


class GenerateFromProfileRequest(BaseModel):
    text: str
    language: str = "en"


@router.post("/api/voice_clone/from_profile/{profile_id}", summary="Synthesise speech from a saved profile (async)")
def generate_from_profile(profile_id: int, req: GenerateFromProfileRequest, user=Depends(get_current_user)):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")

    conn = get_db()
    profile = conn.execute(
        "SELECT reference_filename FROM cloned_voices WHERE id = ? AND user_id = ?",
        (profile_id, user["id"]),
    ).fetchone()
    conn.close()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found.")

    if _count_active_jobs(user["id"], "voice_clone") >= 1:
        raise HTTPException(status_code=429, detail="A voice clone job is already in progress.")

    job_id = _create_job(user["id"], "voice_clone")
    _job_executor.submit(
        _job_voice_clone_profile,
        job_id, user["id"], profile_id, profile["reference_filename"], req.text, req.language,
    )
    return {"job_id": job_id, "status": "pending", "poll_url": f"/api/jobs/{job_id}"}


@router.post("/api/voice_clone/generate_oneshot", summary="One-shot: upload reference audio + synthesise (async)")
async def generate_oneshot(
    file: UploadFile = File(..., description="Reference audio (3–30 sec)"),
    text: str = Form(..., description="Text to synthesise"),
    language: str = Form(default="en"),
    user=Depends(get_current_user),
):
    if not text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")

    content = await file.read()
    ext = _validate_audio_upload(file, content)

    if _count_active_jobs(user["id"], "voice_clone") >= 1:
        raise HTTPException(status_code=429, detail="A voice clone job is already in progress.")

    ref_path = VOICE_SAMPLES_DIR / f"oneshot_{user['id']}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}{ext}"
    ref_path.write_bytes(content)

    job_id = _create_job(user["id"], "voice_clone")
    _job_executor.submit(_job_voice_clone, job_id, user["id"], "oneshot", str(ref_path), text, language)
    return {"job_id": job_id, "status": "pending", "poll_url": f"/api/jobs/{job_id}"}


@router.post("/api/voice_clone/generate", summary="Generate TTS using a saved cloned voice (async)")
def generate_from_voice(
    voice_id: int = Form(...),
    text: str = Form(...),
    language: str = Form(default="en"),
    user=Depends(get_current_user),
):
    if not text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")

    conn = get_db()
    voice = conn.execute(
        "SELECT reference_filename, name FROM cloned_voices WHERE id = ? AND user_id = ?",
        (voice_id, user["id"]),
    ).fetchone()
    conn.close()
    if not voice:
        raise HTTPException(status_code=404, detail="Voice not found.")

    if _count_active_jobs(user["id"], "voice_clone") >= 1:
        raise HTTPException(status_code=429, detail="A voice clone job is already in progress.")

    job_id = _create_job(user["id"], "voice_clone")
    _job_executor.submit(
        _job_voice_clone, job_id, user["id"],
        voice["name"], voice["reference_filename"], text, language,
    )
    return {"job_id": job_id, "status": "pending", "poll_url": f"/api/jobs/{job_id}"}


@router.get("/api/voice_clone/languages", summary="Languages supported by the voice cloning engine")
def clone_languages(user=Depends(get_current_user)):
    return {"languages": _XTTS_LANGUAGES}
