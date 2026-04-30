"""
app/routes/voice_clone.py
All voice cloning endpoints.
"""
import logging
import re
import secrets
import time
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import (
    VOICE_SAMPLES_DIR,
    CLONED_VOICES_DIR,
    CLONE_ALLOWED_EXT,
    CLONE_MAX_FILE_SIZE,
    CLONE_MIN_DURATION,
    CLONE_MAX_DURATION,
    CLONE_MAX_PER_USER,
    MAX_REFERENCE_AUDIO_SIZE,
    MAX_VOICE_PROFILE_NAME_LENGTH,
    MAX_VOICE_PROFILES_PREMIUM,
    VOICE_CLONE_ALLOWED_EXT,
    XTTS_LANGUAGES,
    XTTS_SUPPORTED_LANGUAGES,
    MAX_TEXT_LENGTH,
)
from app.security import get_current_user
from app.jobs import _create_job, _job_executor
from app.ai.workers import _job_voice_clone, _job_voice_clone_profile
from app.ai.loaders import _load_xtts
from db import get_db

logger = logging.getLogger(__name__)
limiter = Limiter(key_func=get_remote_address)

router = APIRouter()


# ---------------------------------------------------------------------------
# Duration check helper
# ---------------------------------------------------------------------------


def _check_audio_duration(file_bytes: bytes, ext: str) -> float:
    """Return duration in seconds; raises HTTPException if invalid."""
    try:
        import soundfile as sf
        import io
        with sf.SoundFile(io.BytesIO(file_bytes)) as f:
            duration = len(f) / f.samplerate
        return duration
    except Exception:
        try:
            import librosa
            import io
            y, sr = librosa.load(io.BytesIO(file_bytes), sr=None)
            return len(y) / sr
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Cannot read audio file: {e}")


# ---------------------------------------------------------------------------
# Upload a voice sample
# ---------------------------------------------------------------------------


@router.post("/api/voice_clone/upload", summary="Upload a voice sample to create a cloned voice profile")
@limiter.limit("10/minute")
async def upload_voice_sample(
    request: Request,
    file: UploadFile = File(..., description="Voice sample audio (3–30 sec)"),
    name: str = Form(..., description="Display name for this voice profile"),
    user=Depends(get_current_user),
):
    name = name.strip()
    if not name or len(name) > 60:
        raise HTTPException(status_code=400, detail="Name must be 1–60 characters.")
    if not re.match(r'^[\w\s\-]+$', name):
        raise HTTPException(status_code=400, detail="Name may only contain letters, numbers, spaces, hyphens, and underscores.")

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")
    ext = Path(file.filename).suffix.lower()
    if ext not in CLONE_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '{ext}'. Allowed: {CLONE_ALLOWED_EXT}",
        )

    content = await file.read()
    if len(content) > CLONE_MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds 10 MB limit.")

    duration = _check_audio_duration(content, ext)
    if duration < CLONE_MIN_DURATION:
        raise HTTPException(
            status_code=400,
            detail=f"Sample too short ({duration:.1f}s). Minimum is {CLONE_MIN_DURATION}s.",
        )
    if duration > CLONE_MAX_DURATION:
        raise HTTPException(
            status_code=400,
            detail=f"Sample too long ({duration:.1f}s). Maximum is {CLONE_MAX_DURATION}s.",
        )

    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) as cnt FROM cloned_voices WHERE user_id = ?", (user["id"],)
    ).fetchone()["cnt"]
    if count >= CLONE_MAX_PER_USER:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {CLONE_MAX_PER_USER} voice profiles reached. Delete one first.",
        )

    sample_filename = f"sample_{user['id']}_{int(time.time())}_{secrets.token_hex(4)}{ext}"
    sample_path = VOICE_SAMPLES_DIR / sample_filename
    sample_path.write_bytes(content)

    now = datetime.utcnow().isoformat()
    cursor = conn.execute(
        "INSERT INTO cloned_voices (user_id, name, reference_filename, created_at) VALUES (?,?,?,?)",
        (user["id"], name, sample_filename, now),
    )
    voice_id = cursor.lastrowid
    conn.commit()
    conn.close()

    logger.info(f"Voice sample uploaded for user {user['email']}: {sample_filename} ({duration:.1f}s)")

    return {
        "success": True,
        "voice_id": voice_id,
        "name": name,
        "duration_seconds": round(duration, 2),
        "filename": sample_filename,
        "created_at": now,
    }


# ---------------------------------------------------------------------------
# List saved voice profiles (both endpoints)
# ---------------------------------------------------------------------------


@router.get("/api/voice_clone/list", summary="List all cloned voice profiles for the current user")
def list_cloned_voices(user=Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM cloned_voices WHERE user_id = ? ORDER BY created_at DESC",
        (user["id"],),
    ).fetchall()
    conn.close()
    return {
        "voices": [dict(r) for r in rows],
        "count": len(rows),
        "limit": CLONE_MAX_PER_USER,
    }


@router.get("/api/voice_clone/profiles", summary="List saved voice profiles for the current user")
def list_voice_profiles(user=Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, reference_filename, created_at FROM cloned_voices "
        "WHERE user_id = ? ORDER BY created_at DESC",
        (user["id"],),
    ).fetchall()
    conn.close()
    max_profiles = MAX_VOICE_PROFILES_PREMIUM
    return {
        "profiles": [dict(r) for r in rows],
        "count": len(rows),
        "max_profiles": max_profiles,
    }


# ---------------------------------------------------------------------------
# Delete a cloned voice profile
# ---------------------------------------------------------------------------


@router.delete("/api/voice_clone/{voice_id}", summary="Delete a cloned voice profile")
def delete_cloned_voice(voice_id: int, user=Depends(get_current_user)):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM cloned_voices WHERE id = ? AND user_id = ?",
        (voice_id, user["id"]),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Voice profile not found.")

    sample_path = VOICE_SAMPLES_DIR / row["reference_filename"]
    if sample_path.exists():
        sample_path.unlink()

    conn.execute("DELETE FROM cloned_voices WHERE id = ?", (voice_id,))
    conn.commit()
    conn.close()

    logger.info(f"Cloned voice deleted for user {user['email']}: voice_id={voice_id}")
    return {"success": True, "deleted_id": voice_id}


@router.delete("/api/voice_clone/profiles/{profile_id}", summary="Delete a saved voice profile")
def delete_voice_profile(profile_id: int, user=Depends(get_current_user)):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM cloned_voices WHERE id = ? AND user_id = ?",
        (profile_id, user["id"]),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Voice profile not found.")

    for _dir in (CLONED_VOICES_DIR, VOICE_SAMPLES_DIR):
        _p = _dir / row["reference_filename"]
        if _p.exists():
            _p.unlink()

    conn.execute("DELETE FROM cloned_voices WHERE id = ?", (profile_id,))
    conn.commit()
    conn.close()

    logger.info(f"Voice profile {profile_id} deleted for {user['email']}")

    return {"success": True, "deleted_id": profile_id}


# ---------------------------------------------------------------------------
# Generate with a saved cloned voice (job-based)
# ---------------------------------------------------------------------------


class VoiceCloneGenerateRequest(BaseModel):
    voice_id: int
    text: str
    language: str = "en"


@router.post("/api/voice_clone/generate", summary="Generate TTS using a cloned voice (XTTS v2)")
def generate_with_cloned_voice(
    request: VoiceCloneGenerateRequest,
    user=Depends(get_current_user),
):
    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")
    if len(request.text) > MAX_TEXT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Text exceeds {MAX_TEXT_LENGTH} character limit.",
        )

    lang = request.language.strip().lower()
    if lang not in XTTS_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language '{lang}'. Supported: {sorted(XTTS_LANGUAGES)}",
        )

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM cloned_voices WHERE id = ? AND user_id = ?",
        (request.voice_id, user["id"]),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Voice profile not found.")

    sample_path = VOICE_SAMPLES_DIR / row["reference_filename"]
    if not sample_path.exists():
        sample_path = CLONED_VOICES_DIR / row["reference_filename"]
    if not sample_path.exists():
        raise HTTPException(status_code=404, detail="Voice sample file missing.")

    job_id = _create_job(user["id"], "voice_clone")
    _job_executor.submit(
        _job_voice_clone, job_id, user["id"],
        row["name"], str(sample_path), request.text.strip(), lang,
    )
    logger.info(f"Voice clone job {job_id} queued for user {user['email']}: voice='{row['name']}'")
    return {
        "job_id": job_id,
        "status": "pending",
        "poll_url": f"/api/jobs/{job_id}",
        "voice_name": row["name"],
        "language": lang,
    }


# ---------------------------------------------------------------------------
# One-shot clone (upload reference + text → synthesised speech, synchronous)
# ---------------------------------------------------------------------------


@router.post("/api/voice_clone/generate_oneshot", summary="Clone a voice from an uploaded reference clip and synthesise speech")
async def voice_clone_generate(
    text: str = Form(..., description="Text to synthesise"),
    language: str = Form(default="en", description="BCP-47 language code"),
    reference_audio: UploadFile = File(..., description="Reference audio (6–30 s recommended)"),
    user=Depends(get_current_user),
):
    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")
    if len(text) > MAX_TEXT_LENGTH:
        raise HTTPException(status_code=400, detail=f"Text exceeds {MAX_TEXT_LENGTH} characters.")

    language = language.strip().lower()
    if language not in XTTS_SUPPORTED_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language '{language}'. Supported: {sorted(XTTS_SUPPORTED_LANGUAGES)}",
        )

    if not reference_audio.filename:
        raise HTTPException(status_code=400, detail="No reference audio provided.")
    ext = Path(reference_audio.filename).suffix.lower()
    if ext not in VOICE_CLONE_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '{ext}'. Allowed: {VOICE_CLONE_ALLOWED_EXT}",
        )
    content = await reference_audio.read()
    if len(content) > MAX_REFERENCE_AUDIO_SIZE:
        raise HTTPException(status_code=400, detail="Reference audio exceeds 25 MB limit.")

    tmp_ref_path = None
    tmp_out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(content)
            tmp_ref_path = tmp.name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_out:
            tmp_out_path = tmp_out.name

        tts = _load_xtts()
        tts.tts_to_file(
            text=text.strip(),
            speaker_wav=tmp_ref_path,
            language=language,
            file_path=tmp_out_path,
        )
        audio_bytes = Path(tmp_out_path).read_bytes()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Voice clone generate error for {user['email']}: {e}")
        raise HTTPException(status_code=500, detail=f"Voice cloning failed: {e}")
    finally:
        for p in (tmp_ref_path, tmp_out_path):
            if p:
                try:
                    Path(p).unlink()
                except Exception:
                    pass

    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?", (user["id"],)
    )
    cur = conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at, audio_data, audio_format)"
        " VALUES (?,?,?,?,?,?,?)",
        (user["id"], "", "__voice_clone__", text[:100], now, audio_bytes, "wav"),
    )
    gen_id = cur.lastrowid
    conn.commit()
    conn.close()

    logger.info(f"Voice clone generated for {user['email']}")
    return Response(
        content=audio_bytes,
        media_type="audio/wav",
        headers={
            "Content-Disposition": "inline; filename=clone.wav",
            "X-Generation-Id": str(gen_id),
        },
    )


# ---------------------------------------------------------------------------
# Save a reusable voice profile
# ---------------------------------------------------------------------------


@router.post("/api/voice_clone/save_profile", summary="Save a voice profile from reference audio for repeated use")
async def save_voice_profile(
    name: str = Form(..., description="Display name for this voice profile"),
    reference_audio: UploadFile = File(..., description="Reference audio file"),
    user=Depends(get_current_user),
):
    max_profiles = MAX_VOICE_PROFILES_PREMIUM
    conn = get_db()
    existing_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM cloned_voices WHERE user_id = ?", (user["id"],)
    ).fetchone()["cnt"]
    conn.close()

    if existing_count >= max_profiles:
        raise HTTPException(
            status_code=403,
            detail=f"Voice profile limit reached ({max_profiles}). Delete an existing profile to add a new one.",
        )

    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Profile name cannot be empty.")
    if len(name) > MAX_VOICE_PROFILE_NAME_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Profile name exceeds {MAX_VOICE_PROFILE_NAME_LENGTH} characters.",
        )

    if not reference_audio.filename:
        raise HTTPException(status_code=400, detail="No reference audio provided.")
    ext = Path(reference_audio.filename).suffix.lower()
    if ext not in VOICE_CLONE_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '{ext}'. Allowed: {VOICE_CLONE_ALLOWED_EXT}",
        )
    content = await reference_audio.read()
    if len(content) > MAX_REFERENCE_AUDIO_SIZE:
        raise HTTPException(status_code=400, detail="Reference audio exceeds 25 MB limit.")

    ref_filename = f"ref_{user['id']}_{int(time.time())}_{secrets.token_hex(4)}{ext}"
    ref_path = CLONED_VOICES_DIR / ref_filename
    ref_path.write_bytes(content)

    now = datetime.utcnow().isoformat()
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO cloned_voices (user_id, name, reference_filename, created_at) VALUES (?,?,?,?)",
        (user["id"], name, ref_filename, now),
    )
    profile_id = cursor.lastrowid
    conn.commit()
    conn.close()

    logger.info(f"Voice profile saved for {user['email']}: '{name}' (id={profile_id})")

    return {
        "success": True,
        "profile_id": profile_id,
        "name": name,
        "created_at": now,
    }


# ---------------------------------------------------------------------------
# Generate from a saved profile (job-based)
# ---------------------------------------------------------------------------


class VoiceCloneFromProfileRequest(BaseModel):
    text: str
    language: str = "en"


@router.post("/api/voice_clone/from_profile/{profile_id}", summary="Synthesise speech using a saved voice profile")
def voice_clone_from_profile(
    profile_id: int,
    request: VoiceCloneFromProfileRequest,
    user=Depends(get_current_user),
):
    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")
    if len(request.text) > MAX_TEXT_LENGTH:
        raise HTTPException(status_code=400, detail=f"Text exceeds {MAX_TEXT_LENGTH} characters.")

    language = request.language.strip().lower()
    if language not in XTTS_SUPPORTED_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language '{language}'. Supported: {sorted(XTTS_SUPPORTED_LANGUAGES)}",
        )

    conn = get_db()
    profile = conn.execute(
        "SELECT * FROM cloned_voices WHERE id = ? AND user_id = ?",
        (profile_id, user["id"]),
    ).fetchone()
    conn.close()
    if not profile:
        raise HTTPException(status_code=404, detail="Voice profile not found.")

    ref_path = CLONED_VOICES_DIR / profile["reference_filename"]
    if not ref_path.exists():
        ref_path = VOICE_SAMPLES_DIR / profile["reference_filename"]
    if not ref_path.exists():
        raise HTTPException(status_code=404, detail="Reference audio file missing from server.")

    job_id = _create_job(user["id"], "voice_clone_profile")
    _job_executor.submit(
        _job_voice_clone_profile, job_id, user["id"],
        profile_id, str(ref_path), request.text.strip(), language,
    )
    logger.info(f"Voice clone profile job {job_id} queued for user {user['email']}: profile={profile_id}")
    return {"job_id": job_id, "status": "pending", "poll_url": f"/api/jobs/{job_id}"}


# ---------------------------------------------------------------------------
# List supported languages
# ---------------------------------------------------------------------------


@router.get("/api/voice_clone/languages", summary="List languages supported by the voice cloning engine")
def voice_clone_languages(user=Depends(get_current_user)):
    return {"languages": sorted(XTTS_SUPPORTED_LANGUAGES)}
