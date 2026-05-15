"""
app/routes/tts.py
TTS generation endpoints: single generate and batch_generate.
"""
import logging
import subprocess
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import (
    MODELS_DIR,
    MAX_TEXT_LENGTH,
)
from app.security import get_current_user
from db import get_db

logger = logging.getLogger(__name__)
limiter = Limiter(key_func=get_remote_address)

router = APIRouter()


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------


def resolve_bin(name: str) -> str:
    """Locate binary in project venv, falling back to system PATH."""
    from app.config import BASE_DIR
    for subdir in ("bin", "Scripts"):
        for suffix in ("", ".exe"):
            candidate = BASE_DIR.parent / "env" / subdir / f"{name}{suffix}"
            if candidate.exists():
                return str(candidate)
    import shutil
    found = shutil.which(name)
    if found:
        return found
    return name


# ---------------------------------------------------------------------------
# Voice helpers (shared)
# ---------------------------------------------------------------------------


def load_voices_json() -> dict:
    voices_json_path = MODELS_DIR / "voices.json"
    if not voices_json_path.exists():
        url = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/voices.json"
        urllib.request.urlretrieve(url, voices_json_path)
    import json
    with open(voices_json_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Synthesis helper (shared by single + batch generation)
# ---------------------------------------------------------------------------


def _synthesize_audio(
    model_info: dict,
    model_id: str,
    text: str,
    out_path: Path,
    speed: float = 1.0,
    pitch_hz: int = 0,
):
    """Synthesize a single audio clip using edge-tts or piper."""
    is_edge = model_info.get("engine") == "edge-tts"

    if is_edge:
        rate_pct = round((speed - 1.0) * 100)
        rate_str = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"
        pitch_str = f"+{pitch_hz}Hz" if pitch_hz >= 0 else f"{pitch_hz}Hz"
        cmd = [
            resolve_bin("edge-tts"),
            "--voice", model_info.get("edge_voice", "ur-PK-UzmaNeural"),
            f"--pitch={pitch_str}",
            f"--rate={rate_str}",
            "--text", text,
            "--write-media", str(out_path),
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            _, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise HTTPException(status_code=500, detail="edge-tts timed out after 30s")
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail=f"edge-tts failed: {stderr}")
    else:
        # Download model files on demand
        for file_path, file_meta in model_info.get("files", {}).items():
            dest = MODELS_DIR / file_path.split("/")[-1]
            if not dest.exists():
                dl_url = (
                    file_meta.get("url")
                    or f"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/{file_path}"
                )
                urllib.request.urlretrieve(dl_url, dest)

        model_onnx = MODELS_DIR / f"{model_id}.onnx"
        length_scale = round(1.0 / max(speed, 0.1), 3)  # piper: higher = slower

        try:
            from piper import PiperVoice
            import wave
            voice = PiperVoice.load(str(model_onnx))
            with wave.open(str(out_path), "wb") as wav_file:
                voice.synthesize_wav(text, wav_file)
        except ImportError:
            cmd = [
                resolve_bin("piper"),
                "--model", str(model_onnx),
                "--output_file", str(out_path),
                "--length-scale", str(length_scale),
            ]
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            try:
                _, stderr = proc.communicate(input=text, timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise HTTPException(status_code=500, detail="piper timed out after 30s")
            if proc.returncode != 0:
                raise HTTPException(status_code=500, detail=f"piper CLI failed: {stderr}")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class GenerateRequest(BaseModel):
    text: str
    model: str
    speed: float = 1.0    # 0.5x to 2.0x playback rate
    pitch_hz: int = 0     # -10 to +10 Hz (edge-tts only)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/api/generate", summary="Generate audio from text")
@limiter.limit("20/minute")
def generate_audio(req: GenerateRequest, request: Request, user=Depends(get_current_user)):
    if not req.text or not req.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")

    if len(req.text) > MAX_TEXT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Text exceeds maximum length of {MAX_TEXT_LENGTH} characters."
        )

    voices_info = load_voices_json()
    if req.model not in voices_info:
        raise HTTPException(status_code=400, detail=f"Unknown model: {req.model}")

    model_info = voices_info[req.model]

    # All features free — no credit or premium gates
    is_edge = model_info.get("engine") == "edge-tts"
    ext = ".mp3" if is_edge else ".wav"
    media_type = "audio/mpeg" if is_edge else "audio/wav"

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        _synthesize_audio(
            model_info, req.model, req.text, tmp_path,
            speed=max(0.5, min(2.0, req.speed)),
            pitch_hz=max(-10, min(10, req.pitch_hz)),
        )
        audio_bytes = tmp_path.read_bytes()
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?",
        (user["id"],),
    )
    conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at)"
        " VALUES (?,?,?,?,?)",
        (user["id"], "", req.model, req.text[:100], now),
    )
    conn.commit()
    conn.close()

    logger.info(f"Audio generated for user {user['id']}: {req.model}")
    return Response(
        content=audio_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f"inline; filename=output{ext}"},
    )
