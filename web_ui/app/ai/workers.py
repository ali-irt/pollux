"""
app/ai/workers.py
Background job worker functions (_job_*).
Each worker is submitted to _job_executor from a route handler.
"""
import io
import logging
import tempfile
import time
from pathlib import Path

from app.config import (
    JOB_TIMEOUT_SONG,
    JOB_TIMEOUT_VOICE_CLONE,
    ACE_STEP_STYLE_TAGS,
    ACE_STEP_QUALITY_PRESETS,
    BASE_DIR,
    OUTPUTS_DIR,
)
from app.jobs import _update_job, _save_generation, _run_with_timeout
from app.ai.loaders import _load_ace_step, _load_xtts

logger = logging.getLogger(__name__)

# Extra pipeline kwargs per quality level.
# guidance_interval: fraction of steps that run the full CFG double-pass.
#   Keep at 0.5 — going lower causes the early structure steps to lose guidance
#   and lets noise accumulate before vocals form.
# use_erg_tag: must stay True; it steers style tags into the generation and
#   removing it noticeably reduces vocal clarity.
# use_erg_lyric / use_erg_diffusion: extra quality passes — off for speed on
#   turbo/fast, on for best.
_QUALITY_PIPELINE_KWARGS = {
    "turbo":    {"guidance_interval": 0.5,
                 "use_erg_tag": True,  "use_erg_lyric": False, "use_erg_diffusion": False},
    "fast":     {"guidance_interval": 0.5,
                 "use_erg_tag": True,  "use_erg_lyric": False, "use_erg_diffusion": False},
    "balanced": {"guidance_interval": 0.5,
                 "use_erg_tag": True,  "use_erg_lyric": False, "use_erg_diffusion": False},
    "best":     {"guidance_interval": 0.5,
                 "use_erg_tag": True,  "use_erg_lyric": True,  "use_erg_diffusion": True},
}


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
# Lyrics formatter — injects [verse]/[chorus] markers if absent
# ---------------------------------------------------------------------------


def _format_lyrics(raw: str) -> str:
    """Ensure lyrics have ACE-Step section markers for best vocal quality.

    If the lyrics already contain any bracket marker (e.g. [verse]), return
    them unchanged.  Otherwise, split on blank lines and label the first two
    paragraphs as [verse] and [chorus] alternately, matching the pattern
    ACE-Step was trained on.
    """
    import re
    if re.search(r'\[.+?\]', raw):
        return raw  # already tagged

    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', raw.strip()) if p.strip()]
    if not paragraphs:
        return raw

    labels = ["[verse]", "[chorus]", "[verse]", "[chorus]", "[bridge]", "[outro]"]
    tagged = []
    for i, para in enumerate(paragraphs):
        label = labels[i] if i < len(labels) else "[verse]"
        tagged.append(f"{label}\n{para}")
    return "\n\n".join(tagged)


# ---------------------------------------------------------------------------
# Song generation worker (ACE-Step 1.5)
# ---------------------------------------------------------------------------


def _job_generate_song(job_id: str, user_id: int, lyrics: str, style: str,
                       audio_duration: int, quality: str = "balanced"):
    _update_job(job_id, "processing")
    tmp_path = None
    try:
        pipeline = _load_ace_step()
        style_prompt = ACE_STEP_STYLE_TAGS.get(style, f"{style}, vocals, music")
        formatted_lyrics = _format_lyrics(lyrics)
        infer_step, scheduler_type, guidance_scale = ACE_STEP_QUALITY_PRESETS.get(
            quality, ACE_STEP_QUALITY_PRESETS["balanced"]
        )

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tmp_path = tf.name

        import torch
        extra = _QUALITY_PIPELINE_KWARGS.get(quality, {})
        with torch.inference_mode():
            pipeline(
                audio_duration=audio_duration,
                prompt=style_prompt,
                lyrics=formatted_lyrics,
                infer_step=infer_step,
                guidance_scale=guidance_scale,
                scheduler_type=scheduler_type,
                save_path=tmp_path,
                **extra,
            )

        out_path = OUTPUTS_DIR / f"{job_id}.wav"
        Path(tmp_path).rename(out_path)
        tmp_path = None  # renamed, no longer needs cleanup

        gen_id = _save_generation(user_id, f"ace-step-1.5:{style}", lyrics)
        _update_job(job_id, "done", generation_id=gen_id, result_path=str(out_path))
        logger.info("Song job %s done for user %s (style=%s, quality=%s, duration=%ds)",
                    job_id, user_id, style, quality, audio_duration)
    except Exception as e:
        logger.error(f"Song job {job_id} failed: {e}")
        _update_job(job_id, "failed", error_message=str(e))
    finally:
        if tmp_path:
            for p in [tmp_path, tmp_path.replace(".wav", "_input_params.json")]:
                try:
                    Path(p).unlink(missing_ok=True)
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
        out_path = OUTPUTS_DIR / f"{job_id}.wav"
        out_path.write_bytes(audio_bytes)
        gen_id = _save_generation(user_id, f"xtts-v2-clone:{voice_name}", text)
        _update_job(job_id, "done", generation_id=gen_id, result_path=str(out_path))
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
        out_path = OUTPUTS_DIR / f"{job_id}.wav"
        out_path.write_bytes(audio_bytes)
        gen_id = _save_generation(user_id, f"__voice_clone_profile_{profile_id}__", text)
        _update_job(job_id, "done", generation_id=gen_id, result_path=str(out_path))
        logger.info(f"Voice clone profile job {job_id} done for user {user_id}")
    except TimeoutError:
        logger.warning(f"Voice clone profile job {job_id} timed out after {JOB_TIMEOUT_VOICE_CLONE}s")
        _update_job(job_id, "failed", error_message=f"Generation timed out after {JOB_TIMEOUT_VOICE_CLONE}s")
    except Exception as e:
        logger.error(f"Voice clone profile job {job_id} failed: {e}")
        _update_job(job_id, "failed", error_message=str(e))
