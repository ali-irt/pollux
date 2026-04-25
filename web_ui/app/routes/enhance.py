"""
app/routes/enhance.py
Local audio enhancement endpoint (normalize, fade, reverb, pitch, speed).
Uses librosa + scipy — no external API calls.
"""
import io
import logging
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.config import ENHANCE_ALLOWED_EXT, ENHANCE_MAX_SIZE
from app.security import get_current_user
from db import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/enhance_audio", summary="Apply local audio effects (normalize, fade, reverb, pitch, speed)")
async def enhance_audio(
    file: UploadFile = File(..., description="Audio file to enhance"),
    normalize: bool = Form(default=True),
    fade_in: float = Form(default=0.0),
    fade_out: float = Form(default=0.0),
    reverb_amount: float = Form(default=0.0),
    pitch_steps: int = Form(default=0),
    speed_factor: float = Form(default=1.0),
    user=Depends(get_current_user),
):
    try:
        import librosa
        import librosa.effects
        import numpy as np
        import soundfile as sf
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Audio processing libraries missing: {e}")

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")
    ext = Path(file.filename).suffix.lower()
    if ext not in ENHANCE_ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Unsupported format '{ext}'. Allowed: {ENHANCE_ALLOWED_EXT}")

    content = await file.read()
    if len(content) > ENHANCE_MAX_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds 50 MB limit.")

    # Clamp params
    fade_in = max(0.0, min(5.0, fade_in))
    fade_out = max(0.0, min(5.0, fade_out))
    reverb_amount = max(0.0, min(1.0, reverb_amount))
    pitch_steps = max(-6, min(6, pitch_steps))
    speed_factor = max(0.5, min(2.0, speed_factor))

    tmp_in_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(content)
            tmp_in_path = tmp.name
        try:
            y, sr = librosa.load(tmp_in_path, sr=None, mono=False)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to load audio: {e}")
    finally:
        if tmp_in_path:
            try:
                Path(tmp_in_path).unlink()
            except Exception:
                pass

    # Ensure shape is (channels, samples) for multi-channel support
    if y.ndim == 1:
        y = y[np.newaxis, :]

    try:
        processed_channels = []
        for ch in range(y.shape[0]):
            audio = y[ch].copy()

            # 1. Time-stretch (pitch-preserving speed change)
            if abs(speed_factor - 1.0) > 0.01:
                audio = librosa.effects.time_stretch(audio, rate=speed_factor)

            # 2. Pitch shift
            if pitch_steps != 0:
                audio = librosa.effects.pitch_shift(audio, sr=sr, n_steps=pitch_steps)

            # 3. Fade in
            if fade_in > 0.0:
                fade_samples = int(fade_in * sr)
                fade_samples = min(fade_samples, len(audio))
                ramp = np.linspace(0.0, 1.0, fade_samples)
                audio[:fade_samples] *= ramp

            # 4. Fade out
            if fade_out > 0.0:
                fade_samples = int(fade_out * sr)
                fade_samples = min(fade_samples, len(audio))
                ramp = np.linspace(1.0, 0.0, fade_samples)
                audio[-fade_samples:] *= ramp

            # 5. Simple algorithmic reverb (comb-filter delay network)
            if reverb_amount > 0.0:
                delays_ms = [29, 37, 43, 53]  # prime delay lengths (ms)
                wet = np.zeros_like(audio)
                for d_ms in delays_ms:
                    delay_samples = int(d_ms * sr / 1000)
                    if delay_samples < len(audio):
                        decay = reverb_amount * 0.5
                        padded = np.zeros(len(audio))
                        padded[delay_samples:] = audio[: len(audio) - delay_samples] * decay
                        wet += padded
                audio = audio + wet * reverb_amount

            processed_channels.append(audio)

        result = np.stack(processed_channels, axis=0)

        # 6. Normalize
        if normalize:
            peak = np.max(np.abs(result))
            if peak > 0:
                result = result / peak * 0.95

        # Squeeze mono back to 1D
        if result.shape[0] == 1:
            result = result[0]
        else:
            result = result.T  # (N, channels) for soundfile

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Enhancement processing failed: {e}")

    buf = io.BytesIO()
    try:
        sf.write(buf, result, sr, format="WAV")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to encode enhanced audio: {e}")

    audio_bytes = buf.getvalue()
    now = datetime.utcnow().isoformat()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO generations (user_id, filename, model, text_snippet, created_at, audio_data, audio_format)"
        " VALUES (?,?,?,?,?,?,?)",
        (user["id"], "", "__enhanced__", f"Enhanced: {file.filename}", now, audio_bytes, "wav"),
    )
    gen_id = cur.lastrowid
    conn.execute(
        "UPDATE users SET generation_count = generation_count + 1 WHERE id = ?", (user["id"],)
    )
    conn.commit()
    conn.close()

    logger.info(f"Audio enhanced for user {user['email']}")
    return Response(
        content=audio_bytes,
        media_type="audio/wav",
        headers={
            "Content-Disposition": "inline; filename=enhanced.wav",
            "X-Generation-Id": str(gen_id),
        },
    )
