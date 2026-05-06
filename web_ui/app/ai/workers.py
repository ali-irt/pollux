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
        from scipy.ndimage import uniform_filter1d
        from math import gcd

        INTRO_SECS = 2.0   # music-only intro before vocals
        TAIL_SECS  = 1.5   # fade-out tail after vocals

        # ── Step 1: vocals via edge-tts ───────────────────────────────────
        edge_voice = SONG_VOICE_TO_EDGE.get(voice_preset, "en-US-JennyNeural")
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            tmp_vocal = tf.name

        cmd = [
            resolve_bin("edge-tts"),
            "--voice", edge_voice,
            "--rate=-15%",   # slower cadence → more song-like phrasing
            "--pitch=+3Hz",  # subtle lift for musicality
            "--text", lyrics.strip(),
            "--write-media", tmp_vocal,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"edge-tts failed: {proc.stderr.decode()}")

        vocals_np, vocal_sr = sf.read(io.BytesIO(Path(tmp_vocal).read_bytes()))
        if vocals_np.ndim > 1:
            vocals_np = vocals_np.mean(axis=1)
        vocals_np = vocals_np.astype(np.float32)

        # ── Step 2: vocal FX — chorus + hall reverb ───────────────────────
        try:
            import librosa

            # Chorus: blend three pitch-shifted layers + short delay double
            pu  = librosa.effects.pitch_shift(vocals_np, sr=vocal_sr, n_steps= 0.25)
            pd  = librosa.effects.pitch_shift(vocals_np, sr=vocal_sr, n_steps=-0.25)
            dly = int(0.022 * vocal_sr)
            doubled = np.zeros_like(vocals_np)
            doubled[dly:] = vocals_np[:-dly] * 0.22
            vocals_np = vocals_np + (pu + pd) * 0.10 + doubled

            # Hall reverb — multi-tap delay network
            wet = np.zeros_like(vocals_np)
            for ms, decay in [(28, 0.45), (65, 0.28), (120, 0.16), (210, 0.09)]:
                d = int(ms * vocal_sr / 1000)
                if d < len(vocals_np):
                    buf_ = np.zeros(len(vocals_np))
                    buf_[d:] = vocals_np[: len(vocals_np) - d] * decay
                    wet += buf_
            vocals_np = vocals_np + wet * 0.32
        except Exception:
            pass  # FX are optional — continue without

        # Normalize vocals to 82 % headroom
        peak = np.max(np.abs(vocals_np))
        if peak > 0:
            vocals_np = vocals_np / peak * 0.82

        # ── Step 3: MusicGen — generate for full song duration ────────────
        vocal_dur = len(vocals_np) / vocal_sr
        target_dur = vocal_dur + INTRO_SECS + TAIL_SECS
        max_tokens = min(int(target_dur * 25.6), 512)

        music_np = music_sr = None
        time_left = deadline - time.time()
        if time_left > 10:
            style_label = style.replace("_", " ").title()
            music_prompt = (
                f"{style_label} instrumental background music, "
                "steady consistent beat, melodic, no vocals, professional studio quality"
            )
            try:
                mg_proc, mg_model = _load_musicgen()
                mg_device = next(mg_model.parameters()).device.type

                def _mg_generate():
                    with torch.inference_mode():
                        inp = mg_proc(text=[music_prompt], padding=True, return_tensors="pt").to(mg_device)
                        return mg_model.generate(**inp, max_new_tokens=max_tokens)

                mg_out = _run_with_timeout(_mg_generate, int(time_left - 5))
                music_np = mg_out[0, 0].cpu().numpy().astype(np.float32)
                music_sr = mg_model.config.audio_encoder.sampling_rate
                logger.info(f"Song job {job_id}: MusicGen produced {len(music_np)/music_sr:.1f}s of music")
            except Exception as e:
                logger.warning(f"Background music skipped in song job {job_id}: {e}")
        else:
            logger.warning(f"Song job {job_id}: skipping MusicGen, only {time_left:.0f}s left")

        # ── Step 4: mix with intro + sidechain ducking + fade ─────────────
        if music_np is not None and music_sr is not None:
            # Resample music to vocal sample rate
            if int(music_sr) != int(vocal_sr):
                g = gcd(int(music_sr), int(vocal_sr))
                music_np = resample_poly(music_np, int(vocal_sr) // g, int(music_sr) // g)

            # Normalize music to 45 % level
            bg_peak = np.max(np.abs(music_np))
            if bg_peak > 0:
                music_np = music_np / bg_peak * 0.45

            # Loop/trim to cover intro + body + tail
            total_samples = int(target_dur * vocal_sr)
            if len(music_np) < total_samples:
                music_np = np.tile(music_np, -(-total_samples // len(music_np)))
            music_np = music_np[:total_samples]

            intro_n = int(INTRO_SECS * vocal_sr)
            tail_n  = int(TAIL_SECS  * vocal_sr)
            body_n  = len(vocals_np)

            intro_music = music_np[:intro_n].copy()
            body_music  = music_np[intro_n : intro_n + body_n].copy()
            tail_music  = music_np[intro_n + body_n : intro_n + body_n + tail_n].copy()

            # Sidechain ducking — lower music where vocals are loud
            frame = int(0.02 * vocal_sr)
            duck = np.ones(body_n)
            for i in range(0, body_n, frame):
                rms = float(np.sqrt(np.mean(vocals_np[i:i+frame] ** 2))) if i < body_n else 0.0
                duck[i:i+frame] = 1.0 - min(rms * 3.2, 0.52)
            duck = uniform_filter1d(duck, size=int(0.08 * vocal_sr))
            body_music *= duck

            mixed_body = np.clip(vocals_np + body_music, -1.0, 1.0)

            # Fade in intro (0.4 s)
            fi = min(int(0.4 * vocal_sr), len(intro_music))
            intro_music[:fi] *= np.linspace(0.0, 1.0, fi)

            # Fade out tail
            if len(tail_music) > 0:
                tail_music *= np.linspace(1.0, 0.0, len(tail_music))

            mixed = np.concatenate([intro_music, mixed_body, tail_music])
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
    """Split text into sentences, synthesise each chunk with quality settings,
    crossfade boundaries, then normalise the final output."""
    import re, numpy as np, scipy.io.wavfile, soundfile as sf

    # Split on sentence boundaries; keep chunks under 180 chars for XTTS quality
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    chunks, current = [], ""
    for s in sentences:
        if len(current) + len(s) <= 180:
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
                text=chunk,
                speaker_wav=speaker_wav,
                language=language,
                file_path=tmp,
                temperature=0.65,        # more consistent, less robotic variance
                repetition_penalty=10.0, # reduce repeated sounds/words
                top_p=0.85,              # tighter nucleus sampling
                speed=1.0,
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

    # Crossfade between chunks (20 ms) to eliminate click/pop at boundaries
    fade_samples = int(sr * 0.02) if sr else 0
    if len(all_audio) == 1 or fade_samples < 2:
        combined = np.concatenate(all_audio)
    else:
        combined = all_audio[0]
        for nxt in all_audio[1:]:
            fade_len = min(fade_samples, len(combined), len(nxt))
            fade_out = np.linspace(1.0, 0.0, fade_len)
            fade_in  = np.linspace(0.0, 1.0, fade_len)
            combined[-fade_len:] *= fade_out
            nxt_copy = nxt.copy()
            nxt_copy[:fade_len] *= fade_in
            combined = np.concatenate([combined, nxt_copy])

    # Normalise to -1 dBFS, preserving dynamics
    peak = np.max(np.abs(combined))
    if peak > 0:
        combined = combined / peak * 0.891  # -1 dBFS headroom

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
        tts = _run_with_timeout(_load_xtts, JOB_TIMEOUT_VOICE_CLONE)
        if time.time() > deadline:
            raise TimeoutError("Voice clone timed out during model load")
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
        tts = _run_with_timeout(_load_xtts, JOB_TIMEOUT_VOICE_CLONE)
        if time.time() > deadline:
            raise TimeoutError("Voice clone timed out during model load")
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
