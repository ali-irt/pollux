"""
app/ai/workers.py
Background job worker functions (_job_*).
Each worker is submitted to _job_executor from a route handler.
"""
import io
import logging
import subprocess
import tempfile
import time
from pathlib import Path

from app.config import (
    JOB_TIMEOUT_MUSIC,
    JOB_TIMEOUT_SONG,
    JOB_TIMEOUT_VOICE_CLONE,
    SONG_VOICE_TO_EDGE,
    BASE_DIR,
)
from app.jobs import _update_job, _save_generation, _run_with_timeout
from app.ai.loaders import _load_musicgen, _load_xtts

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Binary resolution helper (also used by TTS routes)
# ---------------------------------------------------------------------------


def resolve_bin(name: str) -> str:
    """Locate a binary inside the project venv, falling back to system PATH."""
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
# Music generation worker
# ---------------------------------------------------------------------------


def _job_generate_music(job_id: str, user_id: int, prompt: str, duration: int):
    _update_job(job_id, "processing")
    try:
        import torch, scipy.io.wavfile
        processor, model = _load_musicgen()
        device = next(model.parameters()).device.type
        max_tokens = min(int(duration * 25.6), 205)

        def _generate():
            with torch.inference_mode():
                inputs = processor(text=[prompt], padding=True, return_tensors="pt").to(device)
                return model.generate(**inputs, max_new_tokens=max_tokens)

        audio_values = _run_with_timeout(_generate, JOB_TIMEOUT_MUSIC)
        sampling_rate = model.config.audio_encoder.sampling_rate
        audio_np = audio_values[0, 0].cpu().numpy()
        buf = io.BytesIO()
        scipy.io.wavfile.write(buf, rate=sampling_rate, data=audio_np)
        gen_id = _save_generation(user_id, "musicgen-small", prompt, buf.getvalue(), "wav")
        _update_job(job_id, "done", generation_id=gen_id)
        logger.info(f"Music job {job_id} done for user {user_id}")
    except TimeoutError:
        logger.warning(f"Music job {job_id} timed out after {JOB_TIMEOUT_MUSIC}s")
        _update_job(job_id, "failed", error_message=f"Generation timed out after {JOB_TIMEOUT_MUSIC}s")
    except Exception as e:
        logger.error(f"Music job {job_id} failed: {e}")
        _update_job(job_id, "failed", error_message=str(e))


# ---------------------------------------------------------------------------
# Song generation worker
# ---------------------------------------------------------------------------


def _job_generate_song(job_id: str, user_id: int, lyrics: str, voice_preset: str, style: str, quality: str):
    _update_job(job_id, "processing")
    tmp_vocal = None
    deadline = time.time() + JOB_TIMEOUT_SONG
    try:
        import torch, numpy as np, scipy.io.wavfile, soundfile as sf
        from scipy.signal import resample_poly
        from math import gcd

        # ── Step 1: vocals via edge-tts (fast, 1-3s) ─────────────────────
        edge_voice = SONG_VOICE_TO_EDGE.get(voice_preset, "en-US-JennyNeural")
        clean_lyrics = lyrics.strip()

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            tmp_vocal = tf.name

        cmd = [
            resolve_bin("edge-tts"),
            "--voice", edge_voice,
            "--rate=-10%",          # slightly slower → more expressive
            "--pitch=+2Hz",         # slight lift → more melodic
            "--text", clean_lyrics,
            "--write-media", tmp_vocal,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"edge-tts failed: {proc.stderr.decode()}")

        vocals_np, vocal_sr = sf.read(io.BytesIO(Path(tmp_vocal).read_bytes()))
        if vocals_np.ndim > 1:
            vocals_np = vocals_np.mean(axis=1)
        vocals_np = vocals_np.astype(np.float32)

        # ── Step 2: singing effects via librosa (pitch + reverb) ─────────
        try:
            import librosa
            vocals_np = librosa.effects.pitch_shift(vocals_np, sr=vocal_sr, n_steps=2)
            # Simple reverb — short delay network
            wet = np.zeros_like(vocals_np)
            for delay_ms, decay in [(30, 0.3), (60, 0.2), (100, 0.1)]:
                d = int(delay_ms * vocal_sr / 1000)
                if d < len(vocals_np):
                    padded = np.zeros(len(vocals_np))
                    padded[d:] = vocals_np[: len(vocals_np) - d] * decay
                    wet += padded
            vocals_np = np.clip(vocals_np + wet * 0.4, -1.0, 1.0)
        except Exception:
            pass  # effects are optional

        peak = np.max(np.abs(vocals_np))
        if peak > 0:
            vocals_np = vocals_np / peak * 0.88

        # ── Step 3: background music via MusicGen (capped at 4s) ─────────
        music_np = music_sr = None
        time_left = deadline - time.time()
        if time_left <= 5:
            logger.warning(f"Song job {job_id}: skipping MusicGen, only {time_left:.0f}s left")
        else:
            style_label = style.replace("_", " ").title()
            music_prompt = f"{style_label} instrumental background music, no vocals"
            try:
                mg_proc, mg_model = _load_musicgen()
                mg_device = next(mg_model.parameters()).device.type

                def _mg_generate():
                    with torch.inference_mode():
                        inp = mg_proc(text=[music_prompt], padding=True, return_tensors="pt").to(mg_device)
                        return mg_model.generate(**inp, max_new_tokens=102)  # ~4s

                mg_out = _run_with_timeout(_mg_generate, int(time_left - 3))
                music_np = mg_out[0, 0].cpu().numpy().astype(np.float32)
                music_sr = mg_model.config.audio_encoder.sampling_rate
            except (TimeoutError, Exception) as e:
                logger.warning(f"Background music skipped in song job {job_id}: {e}")

        # ── Step 4: mix vocals + background ──────────────────────────────
        if music_np is not None and music_sr is not None:
            if music_sr != vocal_sr:
                g = gcd(int(music_sr), int(vocal_sr))
                music_np = resample_poly(music_np, int(vocal_sr) // g, int(music_sr) // g)
            bg_peak = np.max(np.abs(music_np))
            if bg_peak > 0:
                music_np = music_np / bg_peak * 0.35
            n = len(vocals_np)
            repeats = -(-n // len(music_np)) if len(music_np) < n else 1
            music_np = np.tile(music_np, repeats)[:n]
            mixed = np.clip(vocals_np + music_np, -1.0, 1.0)
        else:
            mixed = vocals_np

        audio_int16 = (mixed * 32767).astype(np.int16)
        buf = io.BytesIO()
        scipy.io.wavfile.write(buf, rate=vocal_sr, data=audio_int16)
        gen_id = _save_generation(user_id, f"edge-tts+musicgen:{style}", lyrics, buf.getvalue(), "wav")
        _update_job(job_id, "done", generation_id=gen_id)
        logger.info(f"Song job {job_id} done for user {user_id}")
    except TimeoutError:
        logger.warning(f"Song job {job_id} timed out after {JOB_TIMEOUT_SONG}s")
        _update_job(job_id, "failed", error_message=f"Generation timed out after {JOB_TIMEOUT_SONG}s")
    except Exception as e:
        logger.error(f"Song job {job_id} failed: {e}")
        _update_job(job_id, "failed", error_message=str(e))
    finally:
        if tmp_vocal:
            try:
                Path(tmp_vocal).unlink()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# XTTS chunked synthesis helper
# ---------------------------------------------------------------------------


def _xtts_synthesize_chunks(tts, text: str, speaker_wav: str, language: str,
                             deadline: float = None) -> bytes:
    """Split text into sentences and synthesise each chunk; concatenate results.
    Keeps each XTTS call short (~100 chars) which is significantly faster on CPU."""
    import re, numpy as np, scipy.io.wavfile, soundfile as sf

    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    chunks, current = [], ""
    for s in sentences:
        if len(current) + len(s) <= 200:
            current = (current + " " + s).strip()
        else:
            if current:
                chunks.append(current)
            current = s
    if current:
        chunks.append(current)

    all_audio = []
    sr = None
    for chunk in chunks:
        if deadline and time.time() > deadline:
            raise TimeoutError("Voice clone timed out during chunk synthesis")
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp = f.name
            time_left = max(5, int(deadline - time.time())) if deadline else 40
            _run_with_timeout(
                tts.tts_to_file, time_left,
                text=chunk, speaker_wav=speaker_wav, language=language, file_path=tmp,
            )
            data, sr = sf.read(tmp)
            if data.ndim > 1:
                data = data.mean(axis=1)
            all_audio.append(data.astype(np.float32))
        finally:
            if tmp:
                try:
                    Path(tmp).unlink()
                except Exception:
                    pass

    if not all_audio:
        raise RuntimeError("No audio chunks generated")

    combined = np.concatenate(all_audio)
    buf = io.BytesIO()
    scipy.io.wavfile.write(buf, rate=int(sr), data=(combined * 32767).astype(np.int16))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Voice clone workers
# ---------------------------------------------------------------------------


def _job_voice_clone(job_id: str, user_id: int, voice_name: str, sample_path: str, text: str, language: str):
    _update_job(job_id, "processing")
    deadline = time.time() + JOB_TIMEOUT_VOICE_CLONE
    try:
        tts = _load_xtts()
        audio_bytes = _xtts_synthesize_chunks(tts, text, sample_path, language, deadline=deadline)
        gen_id = _save_generation(user_id, f"xtts-v2-clone:{voice_name}", text, audio_bytes, "wav")
        _update_job(job_id, "done", generation_id=gen_id)
        logger.info(f"Voice clone job {job_id} done for user {user_id}")
    except TimeoutError:
        logger.warning(f"Voice clone job {job_id} timed out after {JOB_TIMEOUT_VOICE_CLONE}s")
        _update_job(job_id, "failed", error_message=f"Generation timed out after {JOB_TIMEOUT_VOICE_CLONE}s")
    except Exception as e:
        logger.error(f"Voice clone job {job_id} failed: {e}")
        _update_job(job_id, "failed", error_message=str(e))


def _job_voice_clone_profile(job_id: str, user_id: int, profile_id: int, ref_path: str, text: str, language: str):
    _update_job(job_id, "processing")
    deadline = time.time() + JOB_TIMEOUT_VOICE_CLONE
    try:
        tts = _load_xtts()
        audio_bytes = _xtts_synthesize_chunks(tts, text, ref_path, language, deadline=deadline)
        gen_id = _save_generation(user_id, f"__voice_clone_profile_{profile_id}__", text, audio_bytes, "wav")
        _update_job(job_id, "done", generation_id=gen_id)
        logger.info(f"Voice clone profile job {job_id} done for user {user_id}")
    except TimeoutError:
        logger.warning(f"Voice clone profile job {job_id} timed out after {JOB_TIMEOUT_VOICE_CLONE}s")
        _update_job(job_id, "failed", error_message=f"Generation timed out after {JOB_TIMEOUT_VOICE_CLONE}s")
    except Exception as e:
        logger.error(f"Voice clone profile job {job_id} failed: {e}")
        _update_job(job_id, "failed", error_message=str(e))
