"""
app/routes/stats.py
User stats, job polling, and endpoint discovery.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException

from app.config import BASE_URL
from app.security import get_current_user, user_remaining
from db import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/stats", summary="Get usage stats for the current user")
def get_stats(user=Depends(get_current_user)):
    conn = get_db()
    total = conn.execute(
        "SELECT COUNT(*) as cnt FROM generations WHERE user_id = ?", (user["id"],)
    ).fetchone()["cnt"]
    last = conn.execute(
        "SELECT created_at FROM generations WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
        (user["id"],),
    ).fetchone()
    models_used = conn.execute(
        "SELECT model, COUNT(*) as cnt FROM generations WHERE user_id = ? GROUP BY model ORDER BY cnt DESC LIMIT 5",
        (user["id"],),
    ).fetchall()
    conn.close()
    return {
        "plan": user["plan"],
        "generation_count": user["generation_count"],
        "generations_remaining": user_remaining(user),
        "credits": user["credits"],
        "is_premium": user["is_premium"],
        "total_in_history": total,
        "last_generation": last["created_at"] if last else None,
        "top_voices": [{"model": r["model"], "count": r["cnt"]} for r in models_used],
        "member_since": user["created_at"],
    }


@router.get("/api/jobs/{job_id}", summary="Poll status of a background generation job")
def get_job(job_id: str, user=Depends(get_current_user)):
    conn = get_db()
    job = conn.execute(
        "SELECT * FROM jobs WHERE id = ? AND user_id = ?",
        (job_id, user["id"]),
    ).fetchone()
    conn.close()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    result = dict(job)
    if job["status"] == "done" and job["generation_id"]:
        result["audio_url"] = f"/api/audio/{job['generation_id']}"
        result["download_url"] = f"/api/audio/{job['generation_id']}/download"
    return result


@router.get("/api/endpoints", summary="List all API endpoints (mobile dev reference)", tags=["Info"])
def list_endpoints():
    """Returns base URL and a structured map of every endpoint — use this to configure your mobile API client."""
    endpoints = [
        # ── Auth ──────────────────────────────────────────────────────────────
        {"group": "Auth", "method": "POST", "path": "/api/auth/register",          "auth": False, "description": "Register a new account"},
        {"group": "Auth", "method": "POST", "path": "/api/auth/login",             "auth": False, "description": "Login and receive JWT tokens"},
        {"group": "Auth", "method": "POST", "path": "/api/auth/refresh",           "auth": False, "description": "Refresh access token using refresh_token"},
        {"group": "Auth", "method": "GET",  "path": "/api/auth/me",                "auth": True,  "description": "Get current user info"},
        {"group": "Auth", "method": "POST", "path": "/api/auth/change-password",   "auth": True,  "description": "Change account password"},
        {"group": "Auth", "method": "POST", "path": "/api/auth/upgrade-to-premium","auth": True,  "description": "Upgrade user to premium plan"},
        # ── TTS ───────────────────────────────────────────────────────────────
        {"group": "TTS",  "method": "POST", "path": "/api/generate",               "auth": True,  "description": "Generate audio from text (rate: 20/min)"},
        {"group": "TTS",  "method": "POST", "path": "/api/batch_generate",         "auth": True,  "description": "Batch TTS → ZIP download (rate: 5/min)"},
        # ── Voices ────────────────────────────────────────────────────────────
        {"group": "Voices", "method": "GET", "path": "/api/voices",                "auth": True,  "description": "All voices"},
        {"group": "Voices", "method": "GET", "path": "/api/voices/free",           "auth": True,  "description": "Free-tier voices only"},
        {"group": "Voices", "method": "GET", "path": "/api/voices/premium",        "auth": True,  "description": "Premium voices only"},
        # ── History ───────────────────────────────────────────────────────────
        {"group": "History", "method": "GET",    "path": "/api/history",           "auth": True,  "description": "Paginated generation history (?page=1&per_page=20)"},
        {"group": "History", "method": "DELETE", "path": "/api/history/{entry_id}","auth": True,  "description": "Delete a single history entry"},
        {"group": "History", "method": "DELETE", "path": "/api/history",           "auth": True,  "description": "Clear all history"},
        # ── Audio files ───────────────────────────────────────────────────────
        {"group": "Audio", "method": "GET",    "path": "/api/audio/{generation_id}",          "auth": True, "description": "Stream audio from DB"},
        {"group": "Audio", "method": "GET",    "path": "/api/audio/{generation_id}/download", "auth": True, "description": "Download audio from DB (removes from storage after export)"},
        {"group": "Audio", "method": "DELETE", "path": "/api/audio/{generation_id}",          "auth": True, "description": "Delete audio from DB"},
        # ── Voice Cloning ─────────────────────────────────────────────────────
        {"group": "VoiceClone", "method": "POST",   "path": "/api/voice_clone/upload",                    "auth": True, "description": "Upload a voice sample (rate: 10/min)"},
        {"group": "VoiceClone", "method": "GET",    "path": "/api/voice_clone/list",                      "auth": True, "description": "List cloned voice profiles"},
        {"group": "VoiceClone", "method": "DELETE", "path": "/api/voice_clone/{voice_id}",                "auth": True, "description": "Delete a cloned voice profile"},
        {"group": "VoiceClone", "method": "POST",   "path": "/api/voice_clone/generate",                  "auth": True, "description": "Generate TTS using a saved cloned voice profile (XTTS v2)"},
        {"group": "VoiceClone", "method": "POST",   "path": "/api/voice_clone/generate_oneshot",           "auth": True, "description": "One-shot: clone from uploaded reference audio + synthesise speech"},
        {"group": "VoiceClone", "method": "POST",   "path": "/api/voice_clone/save_profile",              "auth": True, "description": "Save a reusable voice profile"},
        {"group": "VoiceClone", "method": "GET",    "path": "/api/voice_clone/profiles",                  "auth": True, "description": "List saved voice profiles"},
        {"group": "VoiceClone", "method": "DELETE", "path": "/api/voice_clone/profiles/{profile_id}",     "auth": True, "description": "Delete a saved voice profile"},
        {"group": "VoiceClone", "method": "POST",   "path": "/api/voice_clone/from_profile/{profile_id}", "auth": True, "description": "Synthesise speech from a saved profile"},
        {"group": "VoiceClone", "method": "GET",    "path": "/api/voice_clone/languages",                 "auth": True, "description": "Languages supported by the cloning engine"},
        # ── Audio enhancement ─────────────────────────────────────────────────
        {"group": "Audio",  "method": "POST", "path": "/api/enhance_audio",        "auth": True, "description": "Apply effects (normalize, fade, reverb, pitch, speed)"},
        {"group": "Audio",  "method": "POST", "path": "/api/transcribe",           "auth": True, "description": "Speech-to-text via local Whisper"},
        # ── Music ─────────────────────────────────────────────────────────────
        {"group": "Music",  "method": "POST", "path": "/api/generate_music",       "auth": True, "description": "Generate music locally (MusicGen)"},
        {"group": "Music",  "method": "POST", "path": "/api/generate_music_fal",   "auth": True, "description": "Generate music via FAL.AI stable-audio"},
        # ── Song generation ───────────────────────────────────────────────────
        {"group": "Song",   "method": "POST", "path": "/api/generate_song",        "auth": True, "description": "Generate AI song with vocals (Bark, fully local)"},
        {"group": "Song",   "method": "GET",  "path": "/api/song/voices",          "auth": True, "description": "List Bark voice presets"},
        # ── Translation ───────────────────────────────────────────────────────
        {"group": "Translation", "method": "POST", "path": "/api/translate",       "auth": True, "description": "Translate text to target language"},
        {"group": "Translation", "method": "GET",  "path": "/api/languages",       "auth": False, "description": "List all supported translation languages"},
        # ── Stats ─────────────────────────────────────────────────────────────
        {"group": "Stats",  "method": "GET",  "path": "/api/stats",                "auth": True, "description": "User usage statistics"},
    ]

    for ep in endpoints:
        ep["url"] = BASE_URL.rstrip("/") + ep["path"]

    return {
        "base_url": BASE_URL,
        "docs_url": BASE_URL.rstrip("/") + "/docs",
        "openapi_url": BASE_URL.rstrip("/") + "/openapi.json",
        "auth_header": "Authorization: Bearer <access_token>",
        "total": len(endpoints),
        "endpoints": endpoints,
    }
